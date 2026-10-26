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

"""Fleet VllmServer health state machine (no real vLLM)."""

import asyncio
import time

from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import (
    Fleet,
    FleetDeployment,
    VllmServer,
    add_refusal,
    fetch_served_models,
    parse_max_model_len,
)
from llmao.model_status import (
    AVAILABLE,
    AWAITING,
    FOREIGN,
    HEALTHY,
    LOADING,
    REJECTED,
    SKEW_CONFIG_MISMATCH,
    SKEW_IN_LITELLM_NOT_IN_CONFIG,
    SKEW_INTENDED_NOT_IN_LITELLM,
    SKEW_VLLM_DOWN_LITELLM_UP,
    SKEW_VLLM_UP_LITELLM_DOWN,
    STALLED,
    UNAVAILABLE,
    UNHEALTHY,
)
from llmao.provisioning import UNKNOWN_FETCH_CAP, ConfigFetch, ProvisioningRecord, UnknownFetch
from llmao.proxy.deployments import DeploymentManager
from tests.fake_transport import FakeTransport
from tests.test_fleet import FLEET_KNOBS


class _Resp:
    def __init__(self, status_code=200, text="", body=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode() if isinstance(text, str) else b""
        self._body = body

    def json(self):
        return self._body


def _server(**kwargs):
    defaults = dict(
        model_name="gemma4-26b",
        name="gemma4-26b",
        host="10.0.0.1",
        listen_port=8001,
        hf_model="google/gemma",
        api_key="sk-x",
        args=[],
        public_port=8001,
    )
    defaults.update(kwargs)
    return VllmServer(**defaults)


def test_probe_serving():
    s = _server()
    s.record_probe(True, now=100.0, grace_s=1800, fail_threshold=3)
    assert s.state == HEALTHY
    assert s.last_ok == 100.0


def test_starting_inside_grace():
    s = _server()
    s.config_served_at = 0.0
    s.record_probe(False, now=10.0, grace_s=1800, fail_threshold=3, err="HTTP 503")
    assert s.state == LOADING


def test_down_after_grace():
    s = _server()
    s.config_served_at = 0.0
    s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="timeout")
    assert s.state == STALLED


def test_serving_needs_consecutive_fails():
    s = _server()
    s.config_served_at = 0.0  # box has already fetched its config
    s.record_probe(True, now=1.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=2.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=3.0, grace_s=1800, fail_threshold=3)
    assert s.state == HEALTHY
    s.record_probe(False, now=4.0, grace_s=1800, fail_threshold=3)
    assert s.state == UNHEALTHY


def test_model_rollup_aggregate():
    a = _server(name="a", listen_port=1)
    b = _server(name="b", listen_port=2, model_name="qwen3-8b")
    # The roll-up recomputes the lifecycle from the stored observation, so the
    # state is set the way record_probe would.
    # Probed "now" so the roll-up's staleness check (3 x 45s) keeps the
    # signals fresh.
    a.record_probe(True, now=10_000.0, grace_s=1800, fail_threshold=3)
    b.config_served_at = 9_990.0
    b.record_probe(False, now=10_000.0, grace_s=1800, fail_threshold=3, err="refused")
    cfg = edict({"fleet": dict(FLEET_KNOBS)})
    fleet = Fleet(cfg=cfg, servers=[a, b])
    assert fleet.model_rollup("gemma4-26b", now=10_000.0).rollup == AVAILABLE
    # Loading is not counted, so a model with only that replica is unavailable.
    assert fleet.model_rollup("qwen3-8b").rollup == UNAVAILABLE
    assert fleet.model_rollup("nope").rollup == UNAVAILABLE


def test_note_config_fetch():
    fleet = Fleet(cfg=None, servers=[])
    fleet.note_config_fetch("primary", now=42.0)
    assert fleet.provisioning.fetches["primary"] == ConfigFetch("primary", 42.0)
    assert fleet.provisioning.fetched_at("primary") == 42.0
    assert fleet.provisioning.fetched_at("never") is None
    assert fleet.config_fetch_display("never") == "—"


def test_unknown_config_fetch_drops_once_in_hosts():
    cfg = edict({"fleet": {"hosts": {"203.0.113.10": []}}})
    fleet = Fleet(cfg=cfg, servers=[])
    fleet.note_unknown_config_fetch("198.51.100.8", now=10)
    fleet.note_unknown_config_fetch("198.51.100.8", now=20)
    assert fleet.provisioning.unknown["198.51.100.8"] == UnknownFetch("198.51.100.8", 10, 20, 2)
    fleet.note_unknown_config_fetch("203.0.113.10", now=30)
    assert "203.0.113.10" not in fleet.provisioning.unknown
    cfg.fleet.hosts["198.51.100.8"] = []
    fleet.note_unknown_config_fetch("203.0.113.9", now=40)
    assert "198.51.100.8" not in fleet.provisioning.unknown
    assert "203.0.113.9" in fleet.provisioning.unknown
    # A known host's fetch forgets it as a stranger.
    fleet.note_config_fetch("203.0.113.9", now=50)
    assert "203.0.113.9" not in fleet.provisioning.unknown


def test_the_unknown_record_is_capped_at_the_oldest_last_seen():
    record = ProvisioningRecord()
    for i in range(UNKNOWN_FETCH_CAP):
        record.note_unknown(f"10.0.{i // 256}.{i % 256}", now=100 + i)
    # The first host seen again is the newest; the second-seen is now the oldest.
    record.note_unknown("10.0.0.0", now=1000)
    assert record.note_unknown("10.9.9.9", now=2000) == ["10.0.0.1"]
    assert len(record.unknown) == UNKNOWN_FETCH_CAP
    assert record.unknown["10.0.0.0"].count == 2


def test_add_refusal_until_serving():
    dep = FleetDeployment.from_vllm(_server())
    assert add_refusal(dep) == "vLLM is not serving yet"
    dep.vllm.state = HEALTHY
    assert add_refusal(dep) is None
    dep.in_litellm = True
    assert add_refusal(dep) == "already in LiteLLM"


def test_add_refusal_without_a_public_address():
    dep = FleetDeployment.from_vllm(_server(public_port=None))
    dep.vllm.state = HEALTHY
    assert add_refusal(dep) == "its public address is not known yet"


class _LiteLLM:
    """What the stand-in LiteLLM says. A test changes these as it goes."""

    def __init__(self, info, health):
        self.info = info
        self.health = health

    def __call__(self, call):
        return self.info if call.path == "model/info" else self.health


def _skew_manager(fleet, info, health=None):
    """A DeploymentManager, and the stand-in LiteLLM it is talking to."""
    litellm = _LiteLLM(info, health or {})
    return DeploymentManager(fleet.cfg, fleet, FakeTransport(litellm)), litellm


def test_skew_sentences_replace_old_phrases():
    srv = _server()
    # Loading: config served, first probe still failing. The skew runner
    # reads the stored observation, so the state is set via probes.
    # The skew runners compute signals at real time.time(), so the probe
    # observation must be fresh for it to count.
    srv.config_served_at = time.time() - 1.0
    srv.record_probe(False, now=time.time(), grace_s=1800, fail_threshold=3, err="refused")
    assert srv.state == LOADING
    dep = FleetDeployment.from_vllm(srv)
    dep.skew = ["missing from LiteLLM", "LiteLLM health disagrees"]
    fleet = Fleet(cfg=edict({"fleet": {"hosts": {}, **FLEET_KNOBS}}), servers=[srv], deployments=[dep])
    backend, litellm = _skew_manager(fleet, [])
    asyncio.run(backend.check_config_skew())
    # Loading is not a missing-route badge. The old sentence is dropped.
    assert dep.skew == []
    assert "missing from LiteLLM" not in dep.skew
    srv.record_probe(True, now=time.time(), grace_s=1800, fail_threshold=3)
    assert srv.state == HEALTHY
    asyncio.run(backend.check_config_skew())
    assert dep.skew == [SKEW_INTENDED_NOT_IN_LITELLM]
    base = srv.api_base
    litellm.health = {"unhealthy_endpoints": [{"api_base": base}], "healthy_endpoints": []}
    asyncio.run(backend.check_health_skew())
    assert SKEW_VLLM_UP_LITELLM_DOWN in dep.skew
    for _ in range(3):
        srv.record_probe(False, now=time.time(), grace_s=1800, fail_threshold=3, err="refused")
    assert srv.state == UNHEALTHY
    litellm.health = {"healthy_endpoints": [{"api_base": base}], "unhealthy_endpoints": []}
    asyncio.run(backend.check_health_skew())
    assert SKEW_VLLM_DOWN_LITELLM_UP in dep.skew
    assert "LiteLLM health disagrees" not in dep.skew


def test_extra_routes_are_per_port_and_model():
    srv = _server()
    dep = FleetDeployment.from_vllm(srv)
    fleet = Fleet(cfg=edict({"fleet": {"hosts": {}, **FLEET_KNOBS}}), servers=[srv], deployments=[dep])
    info = [
        {"model_name": "other-a", "litellm_params": {"api_base": "http://10.0.0.1:9001/v1"}},
        {"model_name": "other-b", "litellm_params": {"api_base": "http://10.0.0.1:9002/v1"}},
        {"model_name": "other-c", "litellm_params": {"api_base": "http://10.0.0.1:9002/v1"}},
    ]
    asyncio.run(_skew_manager(fleet, info)[0].check_config_skew())
    found = {(row.host, row.port, row.model_name) for row in fleet.extra_litellm}
    assert found == {("10.0.0.1", 9001, "other-a"), ("10.0.0.1", 9002, "other-b"), ("10.0.0.1", 9002, "other-c")}
    assert all(SKEW_IN_LITELLM_NOT_IN_CONFIG in row.skew for row in fleet.extra_litellm)
    assert SKEW_IN_LITELLM_NOT_IN_CONFIG not in dep.skew


def test_model_name_mismatch_survives_health_check():
    srv = _server()
    srv.state = HEALTHY
    dep = FleetDeployment.from_vllm(srv)
    fleet = Fleet(cfg=edict({"fleet": {"hosts": {}, **FLEET_KNOBS}}), servers=[srv], deployments=[dep])
    backend, litellm = _skew_manager(fleet, [{"model_name": "not-gemma", "litellm_params": {"api_base": srv.api_base}}])
    asyncio.run(backend.check_config_skew())
    assert SKEW_CONFIG_MISMATCH in dep.skew
    litellm.health = {"healthy_endpoints": [{"api_base": srv.api_base}], "unhealthy_endpoints": []}
    asyncio.run(backend.check_health_skew())
    assert SKEW_CONFIG_MISMATCH in dep.skew
    litellm.info = [{"model_name": srv.model_name, "litellm_params": {"api_base": srv.api_base}}]
    asyncio.run(backend.check_config_skew())
    assert SKEW_CONFIG_MISMATCH not in dep.skew
    assert fleet.extra_litellm == []


def test_add_calls_deployment_only_when_serving():
    srv = _server()
    dep = FleetDeployment.from_vllm(srv)
    fleet = Fleet(cfg=edict({"fleet": {"hosts": {}}}), servers=[srv], deployments=[dep])
    added = []

    def attempt():
        """What the Add button does: ask add_refusal, and add only if it does not object."""
        if add_refusal(dep):
            return
        added.append(dep)

    attempt()
    assert added == []
    srv.state = HEALTHY
    attempt()
    assert added == [dep]


def test_box_json_listen_not_public():
    s = _server(gpu_memory_utilization=0.5, public_port=41234)
    body = s.box_json()
    assert body["model"] == "google/gemma"
    assert body["port"] == 8001
    assert body["gpu_memory_utilization"] == 0.5
    assert s.api_base == "http://10.0.0.1:41234/v1"


def test_local_from_cfg_public_equals_listen():
    from pathlib import Path

    from llmao.models import load_models

    example = Path(__file__).resolve().parent.parent / "models.yaml"
    cfg = edict(
        {
            "fleet": {
                "hosts": {"127.0.0.1": [["gemma4-26b", 8001]]},
                "vllm_api_salt": "test-vllm-salt",
                "health_interval_s": 45,
                "health_timeout_s": 3,
                "health_grace_s": 1800,
                "health_fail_threshold": 3,
                "skew_interval_s": 180,
                "litellm_health_interval_s": 14400,
            },
            "models_path": str(example),
        }
    )
    fleet = Fleet.from_cfg(cfg, models=load_models(example))
    assert fleet.servers[0].listen_port == 8001
    assert fleet.servers[0].public_port == 8001
    # A fresh server awaits contact; serving its config starts the load.
    assert fleet.servers[0].state == AWAITING
    fleet.note_config_fetch("127.0.0.1", now=1.0)
    assert fleet.servers[0].config_served_at == 1.0
    fleet.servers[0].record_probe(False, now=2.0, grace_s=1800, fail_threshold=3, err="HTTP 503")
    assert fleet.servers[0].state == LOADING


def test_probe_skips_without_public_port():
    s = _server(public_port=None)
    assert s.health_url is None
    cfg = edict(
        {
            "fleet": edict(
                {
                    "health_timeout_s": 1,
                    "health_grace_s": 1800,
                    "health_fail_threshold": 3,
                }
            )
        }
    )

    class _Boom:
        async def get(self, url):
            raise AssertionError(f"must not probe {url}")

        async def aclose(self):
            return None

    fleet = Fleet(cfg=cfg, servers=[s])
    asyncio.run(fleet.probe_all(client=_Boom(), now=1.0))
    assert s.state == AWAITING


def test_model_in_litellm():
    s = _server()
    fleet = Fleet(cfg=None, servers=[s])
    assert fleet.model_in_litellm("gemma4-26b") is False
    fleet.deployments[0].in_litellm = True
    assert fleet.model_in_litellm("gemma4-26b") is True
    assert fleet.model_in_litellm("other") is False


def test_grace_boundary_is_exclusive():
    """A slow start must not be marked down one second early.

    health_grace_s is 1800, sized for a cold Gemma pull -- ~50GB of weights
    before vLLM listens. The comparison is `(now - config_served_at) < grace_s`,
    so 1799 is still Loading and 1800 is Stalled. Pinned because moving to <= would
    shave a second off a window that was chosen deliberately, and the failure
    would look like a flaky box rather than an off-by-one.
    """
    s = _server()
    s.config_served_at = 0.0
    s.record_probe(False, now=1799.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == LOADING

    s.record_probe(False, now=1800.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == STALLED


def test_recovers_from_down_on_a_single_probe():
    """DOWN -> SERVING takes one success, and clears the fail counter.

    Asymmetric on purpose: three failures to go down, one success to come
    back. That is the right bias for health-gated registration -- withholding
    a working server is worse than briefly trusting a flaky one -- but it is
    worth stating rather than leaving as an accident of the code.
    """
    s = _server()
    s.config_served_at = 0.0
    for _ in range(3):
        s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == STALLED
    assert s.fails == 3

    s.record_probe(True, now=2100.0, grace_s=1800, fail_threshold=3)
    assert s.state == HEALTHY
    assert s.fails == 0
    assert s.last_error is None


def test_flapping_settles_rather_than_oscillating_per_probe():
    """A box that alternates ok/fail stays SERVING.

    Each success resets fails to 0, so an alternating box never accumulates
    the three consecutive failures needed to go down. It reports SERVING
    throughout.

    That is the intended behaviour of a consecutive-fail threshold, but it
    means a box failing half its probes looks entirely healthy -- worth
    knowing before trusting this signal to gate route registration.
    """
    s = _server()
    s.config_served_at = 0.0
    now = 100.0
    for _ in range(10):
        s.record_probe(False, now=now, grace_s=1800, fail_threshold=3, err="refused")
        now += 45.0
        s.record_probe(True, now=now, grace_s=1800, fail_threshold=3)
        now += 45.0

    assert s.state == HEALTHY
    assert s.fails == 0


def test_down_stays_down_while_failing():
    """Once DOWN, further failures do not reset the grace window.

    The grace branch is only reached when state is not SERVING, and by then
    now - config_served_at is far past grace_s -- so a long-dead box cannot
    slip back into Loading and look like it is merely booting.
    """
    s = _server()
    s.config_served_at = 0.0
    for _ in range(3):
        s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == STALLED

    s.record_probe(False, now=9999.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == STALLED
    assert s.fails == 4


def test_never_healthy_box_goes_starting_then_down():
    """The provisioning failure case: wrong port, or vLLM crash-looping.

    Stays STARTING for the whole grace window so a genuinely slow start is
    not misreported, then goes DOWN once. Registration should therefore never
    have fired for it.
    """
    s = _server()
    s.config_served_at = 0.0
    now = 45.0
    while now < 1800.0:
        s.record_probe(False, now=now, grace_s=1800, fail_threshold=3, err="refused")
        assert s.state == LOADING, f"flipped early at {now}s"
        now += 45.0

    s.record_probe(False, now=1800.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == STALLED


def test_parse_max_model_len():
    body = {"data": [{"id": "gemma4-26b", "max_model_len": 131072}]}
    assert parse_max_model_len(body) == 131072
    assert parse_max_model_len({"data": []}) is None
    assert parse_max_model_len({}) is None
    assert parse_max_model_len(None) is None


def test_oversized_flags_the_hang_condition():
    """A served window above the measured cache makes vLLM hang, not error.

    The A100 measures ~856k tokens of KV cache, so 131072 is safe. A box whose
    cache came out smaller -- a co-resident model, a different card than
    ordered -- would serve the same config and stall on a long request.
    """
    s = _server()
    s.kv_cache_tokens = 855952
    s.observed_max_model_len = 131072
    assert s.oversized is False

    s.kv_cache_tokens = 98304
    assert s.oversized is True


def test_oversized_false_when_unmeasured():
    """An unscraped server is not a broken one."""
    s = _server()
    s.observed_max_model_len = 131072
    assert s.kv_cache_tokens is None
    assert s.oversized is False

    s.kv_cache_tokens = 1000
    s.observed_max_model_len = None
    s.max_model_len = None
    assert s.oversized is False


def test_oversized_falls_back_to_configured_length():
    """Before a scrape lands, the configured value is what we have."""
    s = _server()
    s.kv_cache_tokens = 40960
    s.observed_max_model_len = None
    s.max_model_len = 131072
    assert s.oversized is True


def test_the_served_window_is_read_from_the_root():
    """fetch_served_models takes the server root, not a /v1-suffixed api_base.

    A /v1-suffixed base would ask for root/v1/v1/models, 404, and leave the
    window unknown -- oversized detection quietly dead while the rest of the
    suite stays green.
    """
    calls = []
    root = "http://10.0.0.1:8001"

    class _Client:
        async def get(self, url, headers=None):
            calls.append(url)
            if url == f"{root}/v1/models":
                return _Resp(200, body={"data": [{"max_model_len": 131072}]})
            return _Resp(404)

    served = asyncio.run(fetch_served_models(_Client(), root, "sk-x"))
    assert (served.status, served.max_model_len) == (200, 131072)
    assert calls == [f"{root}/v1/models"]


def test_probe_all_scrapes_from_root_not_api_base():
    """The SERVING-transition read must be handed root_url, not api_base.

    Catches the regression one layer up from fetch_served_models: if
    probe_all ever passes the /v1-suffixed base, the read 404s on every
    transition and oversized stays unknown without any other test failing.
    /metrics is the sampler's, so the fleet no longer asks for it.
    """
    s = _server()
    calls = []
    root = f"http://{s.host}:{s.public_port}"

    class _Client:
        async def get(self, url, headers=None):
            calls.append(url)
            if url.endswith("/health"):
                return _Resp(200, text="OK")
            if url.endswith("/v1/models"):
                return _Resp(200, body={"data": [{"max_model_len": 131072}]})
            return _Resp(404)

    cfg = edict({"fleet": edict({"health_timeout_s": 1, "health_grace_s": 1800, "health_fail_threshold": 3})})
    fleet = Fleet(cfg=cfg, servers=[s])
    asyncio.run(fleet.probe_all(client=_Client(), now=1.0))

    assert s.state == HEALTHY
    assert s.observed_max_model_len == 131072
    assert calls == [f"{root}/health", f"{root}/v1/models"]


def test_the_fleet_is_ready_after_its_first_pass_even_a_failed_one():
    """A comparison that waited on a fleet whose first pass failed would never run."""
    fleet = Fleet(edict(fleet=edict(FLEET_KNOBS)), [])

    async def fail(*_args, **_kwargs):
        raise RuntimeError("Vast API unreachable")

    fleet.refresh_public_ports = fail
    fleet.probe_all = fail

    async def scenario():
        task = asyncio.create_task(fleet.run_lifecycle())
        try:
            await asyncio.wait_for(fleet.ready.wait(), timeout=5)
        finally:
            task.cancel()

    assert fleet.ready.is_set() is False
    asyncio.run(scenario())
    assert fleet.ready.is_set()


def test_litellm_is_not_compared_with_the_fleet_before_its_first_pass():
    from llmao.proxy.client import LiteLLMBackend

    fleet = Fleet(edict(fleet=edict(FLEET_KNOBS)), [])
    fake = FakeTransport(lambda call: {"data": []} if call.path == "model/info" else {})
    backend = LiteLLMBackend(edict(fleet=edict(FLEET_KNOBS)), fleet, transport=fake)

    async def scenario():
        task = asyncio.create_task(backend.run_skew())
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            assert fake.calls == []
            fleet.ready.set()
            for _ in range(20):
                await asyncio.sleep(0)
            assert [c.path for c in fake.calls][:2] == ["model/info", "health"]
        finally:
            task.cancel()

    asyncio.run(scenario())


def test_the_first_probe_starts_the_not_reached_clock():
    s = _server()
    assert s.first_probed_at is None
    s.record_probe(False, now=10.0, grace_s=1800, fail_threshold=3, err="no connection", reached=False)
    assert (s.state, s.first_probed_at) == (AWAITING, 10.0)
    s.record_probe(False, now=1000.0, grace_s=1800, fail_threshold=3, err="no connection", reached=False)
    assert (s.state, s.first_probed_at) == (AWAITING, 10.0)
    s.record_probe(False, now=1810.0, grace_s=1800, fail_threshold=3, err="no connection", reached=False)
    assert s.state == STALLED


def test_an_unlisted_host_with_no_address_is_still_an_attempt():
    """Nothing to dial counts as not reached: the clock runs and Stalled follows."""
    s = _server(public_port=None)
    s.unlisted = True
    cfg = edict({"fleet": edict({"health_timeout_s": 1, "health_grace_s": 1800, "health_fail_threshold": 3})})

    class _Boom:
        async def get(self, url):
            raise AssertionError(f"must not probe {url}")

        async def aclose(self):
            return None

    fleet = Fleet(cfg=cfg, servers=[s])
    asyncio.run(fleet.probe_all(client=_Boom(), now=1.0))
    assert (s.state, s.first_probed_at, s.last_error) == (AWAITING, 1.0, "no address to dial")
    asyncio.run(fleet.probe_all(client=_Boom(), now=1801.0))
    assert s.state == STALLED


class _KeyClient:
    """/health always 200; /v1/models answers `status`, settable between passes."""

    def __init__(self, status=401):
        self.status = status
        self.models_calls = 0

    async def get(self, url, headers=None):
        if url.endswith("/health"):
            return _Resp(200, text="OK")
        if url.endswith("/v1/models"):
            self.models_calls += 1
            if self.status == 200:
                return _Resp(200, body={"data": [{"max_model_len": 131072}]})
            return _Resp(self.status)
        return _Resp(404)

    async def aclose(self):
        return None


def _key_fleet(s):
    cfg = edict({"fleet": edict({"health_timeout_s": 1, "health_grace_s": 1800, "health_fail_threshold": 3})})
    return Fleet(cfg=cfg, servers=[s])


def test_a_refused_key_is_rejected_and_asked_again_every_pass():
    s = _server()
    fleet, client = _key_fleet(s), _KeyClient(401)
    for n in (1, 2, 3, 4, 5):
        asyncio.run(fleet.probe_all(client=client, now=float(n)))
        assert (s.state, s.key_rejected, s.rejected_passes) == (REJECTED, True, n)
    # Listed, so never Foreign, however long it goes on.
    assert client.models_calls == 5
    assert s.observed_max_model_len is None
    # The admin fixes the key: Healthy on that pass, the window read, the count gone.
    client.status = 200
    asyncio.run(fleet.probe_all(client=client, now=6.0))
    assert (s.state, s.key_rejected, s.rejected_passes, s.observed_max_model_len) == (HEALTHY, False, 0, 131072)
    # Healthy with the window known: /v1/models is not asked again.
    asyncio.run(fleet.probe_all(client=client, now=7.0))
    assert client.models_calls == 6


def test_rejected_at_an_unlisted_host_becomes_foreign_after_the_passes():
    s = _server()
    s.unlisted = True
    fleet, client = _key_fleet(s), _KeyClient(401)
    asyncio.run(fleet.probe_all(client=client, now=1.0))
    asyncio.run(fleet.probe_all(client=client, now=2.0))
    assert s.state == REJECTED
    asyncio.run(fleet.probe_all(client=client, now=3.0))
    assert s.state == FOREIGN
    # Listed again (the provider's gap closed): ours after all, back to Rejected.
    s.unlisted = False
    asyncio.run(fleet.probe_all(client=client, now=4.0))
    assert s.state == REJECTED


def test_a_failed_probe_interrupts_the_rejected_run():
    s = _server()
    s.unlisted = True
    s.record_probe(True, now=1.0, grace_s=1800, fail_threshold=3, key_rejected=True)
    s.record_probe(True, now=2.0, grace_s=1800, fail_threshold=3, key_rejected=True)
    assert (s.state, s.rejected_passes) == (REJECTED, 2)
    s.record_probe(False, now=3.0, grace_s=1800, fail_threshold=3, err="no connection", reached=False)
    assert s.rejected_passes == 0
    assert s.state != FOREIGN


def test_rejected_and_foreign_may_be_removed_but_not_added():
    from llmao.fleet import remove_refusal

    s = _server()
    dep = FleetDeployment.from_vllm(s)
    for state in (REJECTED, FOREIGN):
        s.state = state
        dep.in_litellm = False
        assert add_refusal(dep) == "vLLM rejects our key"
        dep.in_litellm = True
        assert remove_refusal(dep) is None
