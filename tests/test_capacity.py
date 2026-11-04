# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""The hourly capacity rollup and where its rows are kept (llmao/capacity.py)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import (
    DEMAND_DAY_PREFIX,
    DEMAND_PREFIX,
    FOLD_VERSION,
    HOUR_S,
    NO_PROJECT,
    CapacityStore,
    CapacityWriter,
    DemandRollup,
    HourlyRollup,
    fold_demand,
    hour_label,
    retention_days,
)
from llmao.proxy.client import MemoryStore
from llmao.rental import Rental
from llmao.vllm_client import Sample
from tests.fake_transport import fake_memory

# 2026-10-18T09:00:00Z, a clock hour's start.
T0 = 1792314000.0
SERVER = "203.0.113.10:8001"


def _sample(ts, *, running=0, waiting=0, kv=0.0, **counters):
    buckets = counters.pop("buckets", {})
    return Sample(ts=ts, running=running, waiting=waiting, kv=kv, counters=counters, buckets=buckets)


def _store():
    transport, rows = fake_memory()
    return CapacityStore(MemoryStore(transport)), rows


def test_t0_is_on_the_hour():
    assert T0 % HOUR_S == 0
    assert hour_label(int(T0)) == "2026-10-18T09"


def test_an_hour_of_readings_becomes_one_row():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "qwen3-8b", _sample(T0, running=2, kv=0.2, prompt_tokens=100, generation_tokens=10))
    rollup.record(
        SERVER, "qwen3-8b", _sample(T0 + 15, running=1, waiting=3, kv=0.6, prompt_tokens=400, generation_tokens=60)
    )
    rollup.record(SERVER, "qwen3-8b", _sample(T0 + 30, kv=0.1, prompt_tokens=450, generation_tokens=70))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    row = hour.row(15)
    assert row.hour == "2026-10-18T09"
    assert row.server == SERVER
    assert row.model_name == "qwen3-8b"
    assert row.covered_s == 45
    assert row.busy_frac == round(2 / 3, 3)
    assert row.waiting_frac == round(1 / 3, 3)
    assert row.waiting_peak == 3
    assert row.kv_avg == 0.3
    assert row.kv_peak == 0.6
    # Growth since the first reading; what came before it is not this hour's.
    assert row.prompt_tokens == 350
    assert row.generation_tokens == 60
    assert row.usd_per_hour is None
    # No provider has placed the server: no rental on the row.
    assert row.rental == "" and row.rental_label == ""


def test_an_hour_is_stamped_with_the_rental_its_provider_names():
    """The server is an address and port, which changes when the provider moves the
    machine and means nothing once the rental ends. The stamp keeps its hours together."""
    rental = Rental(
        provider="Vast",
        id="53865410",
        label="RTX 3060 x2",
        url="",
        usd_per_hour=0.14,
        host="203.0.113.10",
        name="53865410",
        status="running",
    )
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0), usd_per_hour=0.07, rental=rental)
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    row = hour.row(15)
    assert row.rental == "Vast 53865410"
    assert row.rental_label == "RTX 3060 x2"
    assert row.usd_per_hour == 0.07


def test_a_restarted_server_counts_from_zero():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0, requests=50))
    rollup.record(SERVER, "m", _sample(T0 + 15, requests=60))
    rollup.record(SERVER, "m", _sample(T0 + 30, requests=4))  # vLLM restarted
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    assert hour.row(15).requests == 14


def test_a_missed_reading_does_not_credit_the_gap():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0, requests=10))
    rollup.missed(SERVER)
    rollup.record(SERVER, "m", _sample(T0 + 600, requests=500))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    assert hour.row(15).requests == 0
    assert hour.row(15).covered_s == 30


def test_the_hour_is_finished_by_the_next_reading_or_by_the_clock():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0))
    rollup.record("198.51.100.8:8001", "m", _sample(T0))
    assert rollup.take_finished(T0 + 60) == []
    # The first server moves into the next hour; the second goes quiet.
    rollup.record(SERVER, "m", _sample(T0 + HOUR_S))
    assert [h.server for h in rollup.take_finished(T0 + HOUR_S + 1)] == [SERVER, "198.51.100.8:8001"]
    # The new hour is still open.
    assert rollup.take_finished(T0 + HOUR_S + 2) == []


def test_queue_time_p95_comes_from_the_histogram():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0, queued=0, queue_s=0, buckets={0.3: 0, 1.0: 0, 5.0: 0}))
    rollup.record(SERVER, "m", _sample(T0 + 15, queued=100, queue_s=50, buckets={0.3: 80, 1.0: 96, 5.0: 100}))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    row = hour.row(15)
    assert row.queue_p95_s == 1.0
    assert row.queue_avg_s == 0.5


def test_a_quiet_hour_has_no_queue_figures():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    assert hour.row(15).queue_p95_s is None
    assert hour.row(15).queue_avg_s is None


def test_rows_are_kept_by_hour_then_server_and_read_by_day():
    store, rows = _store()
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    asyncio.run(store.write(hour.row(15)))
    assert list(rows) == [f"llmao:capacity:2026-10-18T09:{SERVER}"]
    today = dt.date(2026, 10, 18)
    assert [r.server for r in asyncio.run(store.read(1, today=today))] == [SERVER]
    assert asyncio.run(store.read(1, today=today + dt.timedelta(days=1))) == []
    assert len(asyncio.run(store.read(2, today=today + dt.timedelta(days=1)))) == 1


def test_pruning_drops_only_the_days_past_retention():
    store, rows = _store()
    for day in ("2026-07-19", "2026-07-20", "2026-07-21"):
        rows[f"llmao:capacity:{day}T00:{SERVER}"] = json.dumps({"hour": f"{day}T00"})
    # Keeping 90 days on 2026-10-18 keeps 2026-07-21 onwards.
    gone = asyncio.run(store.prune(90, today=dt.date(2026, 10, 18)))
    assert gone == 2
    assert list(rows) == [f"llmao:capacity:2026-07-21T00:{SERVER}"]


def test_an_hour_that_cannot_be_stored_waits_for_the_next_pass():
    transport, rows = fake_memory(down=True)
    rollup = HourlyRollup(interval_s=15)
    writer = CapacityWriter(rollup, CapacityStore(MemoryStore(transport)))
    rollup.record(SERVER, "m", _sample(T0))
    asyncio.run(writer.write_finished(T0 + HOUR_S))
    assert rows == {}
    transport.down = False
    asyncio.run(writer.write_finished(T0 + HOUR_S + 60))
    assert list(rows) == [f"llmao:capacity:2026-10-18T09:{SERVER}"]


def test_retention_is_required():
    assert retention_days(edict(capacity_retention_days=90)) == 90
    with pytest.raises(ValueError, match="capacity_retention_days is required"):
        retention_days(edict())
    for bad in (0, -1, "ninety"):
        with pytest.raises(ValueError, match="capacity_retention_days must be"):
            retention_days(edict(capacity_retention_days=bad))


# ---- provider spend ----


# ---------------------------------------------------------------------------
# Demand: a day of LiteLLM's spend log, folded into one row per model per
# hour. The rows look like what /spend/logs/v2 returned on 2026-11-01, cut
# down to the fields the fold reads.
# ---------------------------------------------------------------------------

DAY = dt.date(2026, 11, 1)


def _spend(
    hour,
    model="gemma4-26b",
    *,
    project="spark",
    agent="OpenAI/Python 1.109.1",
    prompt=580,
    completion=46,
    ms=600,
    failure=None,
    minute=10,
):
    """One spend-log row, as spend_log() hands them over. `failure` is LiteLLM's normalized_error."""
    meta = edict(user_api_key_team_alias=project, user_agent=agent, error_information=None)
    if failure:
        meta.error_information = edict(normalized_error=failure, error_code=failure[:3])
    return edict(
        startTime=f"{DAY}T{hour:02d}:{minute:02d}:00.000+00:00",
        model_group=model,
        status="failure" if failure else "success",
        prompt_tokens=0 if failure else prompt,
        completion_tokens=0 if failure else completion,
        request_duration_ms=ms,
        metadata=meta,
    )


def test_a_day_folds_into_one_row_per_model_per_hour():
    rows = fold_demand(
        DAY,
        [
            _spend(9),
            _spend(9, prompt=92_000, completion=100, ms=34_000, agent="opencode/latest", project="netbeans"),
            _spend(9, model="qwen3.8-27b"),
            _spend(10),
        ],
    )
    assert [(r.hour, r.model) for r in rows] == [
        ("2026-11-01T09", "gemma4-26b"),
        ("2026-11-01T09", "qwen3.8-27b"),
        ("2026-11-01T10", "gemma4-26b"),
    ]
    nine = rows[0]
    assert nine.requests == 2
    assert nine.prompt_tokens == 580 + 92_000
    assert nine.completion_tokens == 146
    assert nine.by_project == {
        "spark": dict(requests=1, prompt_tokens=580, completion_tokens=46),
        "netbeans": dict(requests=1, prompt_tokens=92_000, completion_tokens=100),
    }
    assert {k: v["requests"] for k, v in nine.by_tool.items()} == {"OpenAI": 1, "opencode": 1}
    assert nine.prompt_len == {"<1k": 1, "32k-128k": 1}
    assert nine.duration_p50_s == 0.6
    assert nine.duration_p95_s == 34.0
    assert nine.sources == ["litellm"]
    assert nine.refused == {}


def test_what_is_not_demand_is_left_out():
    """A bad key at the door, a /v1/models listing, LiteLLM's own health checks, another day."""
    rows = fold_demand(
        DAY,
        [
            _spend(9),
            edict(_spend(9), model_group="", call_type="/v1/models"),
            _spend(9, project="litellm-internal-health-check"),
            edict(_spend(9), startTime="2026-11-02T00:00:00.000+00:00"),
        ],
    )
    assert [(r.hour, r.requests) for r in rows] == [("2026-11-01T09", 1)]


def test_a_refused_request_counts_as_a_request_with_a_reason_and_no_tokens():
    (row,) = fold_demand(
        DAY,
        [_spend(9), _spend(9, failure="429_RATE_LIMIT", ms=2), _spend(9, failure="429_RATE_LIMIT", project=None)],
    )
    assert row.requests == 3
    assert row.refused == {"429_RATE_LIMIT": 2}
    assert row.prompt_tokens == 580
    assert row.by_project[NO_PROJECT]["requests"] == 1
    # the 2ms refusals do not drag the latency down
    assert row.duration_p50_s == 0.6
    assert row.prompt_len == {"<1k": 1}


def test_demand_rows_are_kept_by_hour_then_model_and_pruned_with_the_rest():
    store, rows = _store()
    (row,) = fold_demand(DAY, [_spend(9)])
    asyncio.run(store.write_demand(row))
    asyncio.run(store.mark_demand_day("2026-11-01", sources=["litellm"], rows=1))
    assert sorted(rows) == [f"{DEMAND_DAY_PREFIX}2026-11-01", f"{DEMAND_PREFIX}2026-11-01T09:gemma4-26b"]
    assert [r.model for r in asyncio.run(store.read_demand(1, today=DAY))] == ["gemma4-26b"]
    assert asyncio.run(store.demand_days_done()) == {
        "2026-11-01": edict(version=FOLD_VERSION, sources=["litellm"], rows=1, log="off")
    }
    # 90 days later the day, its rows and its marker go together.
    rows[f"llmao:capacity:2026-11-01T09:{SERVER}"] = json.dumps({"hour": "2026-11-01T09"})
    gone = asyncio.run(store.prune(90, today=DAY + dt.timedelta(days=91)))
    assert gone == 2
    assert rows == {}


class _Metering:
    """Stands in for UsageManager: hands back the spend log of each day it is asked for."""

    def __init__(self, days: dict[str, list], *, down: bool = False):
        self.days = days
        self.down = down
        self.asked: list[dt.date] = []

    async def spend_log(self, day):
        self.asked.append(day)
        if self.down:
            raise RuntimeError("LiteLLM away")
        return self.days.get(day.isoformat(), [])


def test_the_rollup_does_every_closed_day_the_store_lacks_oldest_first():
    store, rows = _store()
    halloween = [edict(_spend(h), startTime=f"2026-10-31T{h:02d}:10:00.000+00:00") for h in (9, 10)]
    metering = _Metering({"2026-11-01": [_spend(9)], "2026-10-31": halloween})
    rollup = DemandRollup(metering, store, keep_days=3)
    # 01:30 on the 2nd: the 1st closed at 01:00, the 2nd is still open.
    done = asyncio.run(rollup.roll_up_missing(dt.datetime(2026, 11, 2, 1, 30, tzinfo=dt.UTC)))
    assert done == [dt.date(2026, 10, 31), dt.date(2026, 11, 1)]
    assert metering.asked == done
    markers = asyncio.run(store.demand_days_done())
    assert markers["2026-10-31"].rows == 2
    assert markers["2026-11-01"].rows == 1
    assert len([k for k in rows if k.startswith(DEMAND_PREFIX)]) == 3
    # The next run finds nothing to do, and asks LiteLLM for nothing.
    assert asyncio.run(rollup.roll_up_missing(dt.datetime(2026, 11, 2, 2, 0, tzinfo=dt.UTC))) == []
    assert metering.asked == done


def test_a_day_is_not_closed_until_an_hour_after_midnight():
    rollup = DemandRollup(_Metering({}), _store()[0], keep_days=90)
    assert rollup.missing_days({}, dt.datetime(2026, 11, 2, 0, 30, tzinfo=dt.UTC))[-1] == dt.date(2026, 10, 31)
    assert rollup.missing_days({}, dt.datetime(2026, 11, 2, 1, 0, tzinfo=dt.UTC))[-1] == dt.date(2026, 11, 1)


def test_a_request_to_a_deployment_id_is_a_probe_not_demand():
    """The fleet tools and health probes name a deployment by its LiteLLM id."""
    rows = fold_demand(DAY, [_spend(9), _spend(9, model="dae4931b-4b1d-4342-b680-9ba79e4cd375", prompt=8)])
    assert [(r.model, r.requests) for r in rows] == [("gemma4-26b", 1)]


def test_a_day_rolled_up_by_an_older_fold_is_done_again_without_its_old_rows():
    store, rows = _store()
    # What an earlier fold left: a marker without the current version, and a
    # row for a model the current fold no longer counts.
    rows[f"{DEMAND_DAY_PREFIX}2026-11-01"] = json.dumps(dict(version=1, sources=["litellm"], rows=1))
    rows[f"{DEMAND_PREFIX}2026-11-01T09:dae4931b-4b1d-4342-b680-9ba79e4cd375"] = json.dumps(dict(hour="2026-11-01T09"))
    rollup = DemandRollup(_Metering({"2026-11-01": [_spend(9)]}), store, keep_days=2)
    done = asyncio.run(rollup.roll_up_missing(dt.datetime(2026, 11, 2, 1, 30, tzinfo=dt.UTC)))
    assert done == [dt.date(2026, 11, 1)]
    assert sorted(rows) == [f"{DEMAND_DAY_PREFIX}2026-11-01", f"{DEMAND_PREFIX}2026-11-01T09:gemma4-26b"]
    assert asyncio.run(store.demand_days_done())["2026-11-01"].version == FOLD_VERSION
    # And a day marked by the current fold is left alone.
    assert asyncio.run(rollup.roll_up_missing(dt.datetime(2026, 11, 2, 2, 0, tzinfo=dt.UTC))) == []


def test_a_quiet_day_is_marked_so_it_is_not_asked_for_again():
    store, _rows = _store()
    rollup = DemandRollup(_Metering({}), store, keep_days=2)
    asyncio.run(rollup.roll_up_missing(dt.datetime(2026, 11, 2, 1, 30, tzinfo=dt.UTC)))
    assert asyncio.run(store.demand_days_done())["2026-11-01"].rows == 0


def test_a_day_that_cannot_be_read_waits_for_the_next_run_with_the_rest():
    store, rows = _store()
    rollup = DemandRollup(_Metering({}, down=True), store, keep_days=3)
    assert asyncio.run(rollup.roll_up_missing(dt.datetime(2026, 11, 2, 1, 30, tzinfo=dt.UTC))) == []
    assert rows == {}


# ---------------------------------------------------------------------------
# The prompt log's side of a day: the runner spawns the tool for the day and
# folds what only the log can say into the same rows.
# ---------------------------------------------------------------------------


def _log_record(hour, model="gemma4-26b", *, stream=False, tools=False, finish="stop", ttft=None, project="spark"):
    return dict(
        ts=f"2026-11-01T{hour:02d}:15:00.000Z",
        model=model,
        stream=stream,
        ttft_s=ttft,
        finish_reason=finish,
        params={"tools": [{"type": "function"}]} if tools else {},
        key={"project": project},
    )


def _log_day(tmp_path, records):
    (tmp_path / "prompts-2026-11-01.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return tmp_path


class _Notices:
    def __init__(self):
        self.noted = []

    async def note(self, what, *, subject=None, **detail):
        self.noted.append((subject, what))


def test_a_day_is_rolled_up_from_both_sides_and_the_log_only_fields_join_litellms_rows(tmp_path):
    store, _rows = _store()
    log_dir = _log_day(
        tmp_path,
        [
            _log_record(9, stream=True, ttft=0.4, tools=True),
            _log_record(9, stream=True, ttft=1.2, finish="length"),
            # An hour LiteLLM has no row for: the log's side alone, for the reconciliation.
            _log_record(11),
        ],
    )
    metering = _Metering({"2026-11-01": [_spend(9), _spend(9)]})
    rollup = DemandRollup(metering, store, keep_days=3, log_dir=log_dir)
    asyncio.run(rollup.roll_up(DAY))
    nine, eleven = sorted(asyncio.run(store.read_demand(1, today=DAY)), key=lambda r: r.hour)
    assert nine.sources == ["litellm", "log"] and nine.requests == 2 and nine.log_requests == 2
    assert (nine.streamed, nine.with_tools, nine.finish_length) == (2, 1, 1)
    assert (nine.ttft_p50_s, nine.ttft_p95_s) == (0.4, 1.2)
    assert eleven.sources == ["log"] and eleven.requests == 0 and eleven.log_requests == 1
    marker = asyncio.run(store.demand_days_done())["2026-11-01"]
    assert (marker.sources, marker.log, marker.rows) == (["litellm", "log"], "ok", 2)


def test_a_day_without_a_log_file_is_final_and_one_whose_tool_failed_is_tried_again(tmp_path):
    store, _rows = _store()
    notices = _Notices()
    metering = _Metering({"2026-11-01": [_spend(9)]})
    # No file for the day: LiteLLM's side alone, and nothing to retry.
    rollup = DemandRollup(metering, store, keep_days=2, log_dir=tmp_path, notices=notices)
    asyncio.run(rollup.roll_up(DAY))
    marker = asyncio.run(store.demand_days_done())["2026-11-01"]
    assert (marker.sources, marker.log) == (["litellm"], "absent")
    assert notices.noted == []
    assert rollup.missing_days(asyncio.run(store.demand_days_done()), dt.datetime(2026, 11, 2, 2, tzinfo=dt.UTC)) == []
    # The tool cannot run: the day is written from LiteLLM, the admins are
    # told once, and the day is missing again next run.
    broken = DemandRollup(
        metering, store, keep_days=2, log_dir=tmp_path, notices=notices, tool=tmp_path / "no-such-tool"
    )
    asyncio.run(broken.roll_up(DAY))
    marker = asyncio.run(store.demand_days_done())["2026-11-01"]
    assert (marker.sources, marker.log) == (["litellm"], "failed")
    ((subject, what),) = notices.noted
    assert subject == "prompt-log-rollup" and "2026-11-01" in what and "tries the log again" in what
    assert broken.missing_days(asyncio.run(store.demand_days_done()), dt.datetime(2026, 11, 2, 2, tzinfo=dt.UTC)) == [
        DAY
    ]
    # Without a log directory a failed marker is not the runner's concern.
    plain = DemandRollup(metering, store, keep_days=2)
    assert (
        plain.missing_days(
            {"2026-11-01": edict(version=FOLD_VERSION, log="failed")}, dt.datetime(2026, 11, 2, 2, tzinfo=dt.UTC)
        )
        == []
    )
