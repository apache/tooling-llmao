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

"""What the analysis scripts share (analysis/_core.py), and prompt-report end to end."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys

import _core
import pytest
from conftest import ANALYSIS

REPORT = ANALYSIS / "prompt-report"


def test_a_script_finds_the_log_directory_in_config_or_stops(tmp_path):
    cfg = tmp_path / "config.yaml"
    with pytest.raises(SystemExit, match="not found"):
        _core.log_dir(cfg)
    cfg.write_text("prompt_log_dir: /x1/llmao/prompt-log\n")
    assert _core.log_dir(cfg).as_posix() == "/x1/llmao/prompt-log"
    cfg.write_text("other: 1\n")
    with pytest.raises(SystemExit, match="prompt_log_dir is required"):
        _core.log_dir(cfg)


def test_a_span_is_the_last_closed_days_or_from_to():
    today = dt.datetime.now(dt.UTC).date()
    last = _core.span(days=3)
    assert last == [today - dt.timedelta(days=3), today - dt.timedelta(days=2), today - dt.timedelta(days=1)]
    assert _core.span(start=dt.date(2026, 11, 1), end=dt.date(2026, 11, 2)) == [
        dt.date(2026, 11, 1),
        dt.date(2026, 11, 2),
    ]
    assert _core.span(days=2, end=dt.date(2026, 11, 3)) == [dt.date(2026, 11, 2), dt.date(2026, 11, 3)]
    try:
        _core.span(start=dt.date(2026, 11, 3), end=dt.date(2026, 11, 2))
    except ValueError as e:
        assert "after it ends" in str(e)
    else:
        raise AssertionError("a start after its end must be an error")


def _record(day: str, model: str = "qwen3-8b") -> dict:
    return dict(
        ts=f"{day}T09:30:00.000Z",
        model=model,
        status="success",
        error=None,
        stream=False,
        finish_reason="stop",
        params={},
        tokens={"prompt": 500, "completion": 20, "total": 520},
        key={"project": "tooling", "user": "someone"},
        user_agent="OpenAI/Python 1.109.1",
        messages=[{"role": "user", "content": "x"}],
    )


def test_prompt_report_prints_the_reports_as_text_and_as_csv(tmp_path):
    (tmp_path / "prompts-2026-11-01.jsonl").write_text(json.dumps(_record("2026-11-01")) + "\n")
    (tmp_path / "prompts-2026-11-02.jsonl").write_text(json.dumps(_record("2026-11-02", "gemma4-26b")) + "\n")
    base = [sys.executable, str(REPORT), "--log-dir", str(tmp_path), "--from", "2026-11-01", "--to", "2026-11-02"]
    text = subprocess.run(base, capture_output=True, text=True, check=True).stdout
    assert "2 requests over 2026-11-01 to 2026-11-02, 2 days with records" in text
    assert "By project" in text and "tooling" in text and "Messages per request" in text
    as_csv = subprocess.run([*base, "--csv"], capture_output=True, text=True, check=True).stdout
    assert "# By model\nname,requests,per day,prompt tokens\n" in as_csv
    assert "gemma4-26b,1,0.5,500\n" in as_csv
