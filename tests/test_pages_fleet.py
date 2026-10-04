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

"""fleet_rows + fleet.ezt stay in lockstep.

EZT raises UnknownReference when a template reads a name the data does not
carry, and Quart returns that as a 500 with the traceback (and, on the /fleet
page, the session) -- so a row field added in pages.py but forgotten in
fleet.ezt (or vice versa) is a real outage, not a lint nit. This builds the
rows through the same fleet_rows the page uses and renders the real template.
"""

import io
import pathlib

import asfquart
import ezt
from easydict import EasyDict

from llmao.fleet import Fleet, FleetDeployment, VllmServer
from llmao.model_status import AWAITING, HEALTHY, LOADING, STALLED

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent
NOW = 10_000.0


def _app():
    # pages.py registers its routes on asfquart.APP at import time, so the
    # app must exist before `import pages`. The real config.yaml is
    # git-ignored, so point at the committed example; nothing here reads
    # fleet state off APP.
    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


def _server(name: str, port: int) -> VllmServer:
    return VllmServer(
        model_name="gemma4-26b",
        name=name,
        host="10.0.0.1",
        listen_port=port,
        hf_model="google/gemma",
        api_key="sk-x",
        args=[],
        public_port=port,
    )


def _fleet():
    """One server per lifecycle, driven through the same calls the app uses."""
    pages = _app()
    healthy = _server("healthy", 8001)
    healthy.record_probe(True, now=NOW, grace_s=1800, fail_threshold=3)
    assert healthy.state == HEALTHY

    loading_reached = _server("loading-reached", 8002)
    loading_reached.config_served_at = NOW - 10
    loading_reached.record_probe(False, now=NOW, grace_s=1800, fail_threshold=3, err="HTTP 503", reached=True)
    assert loading_reached.state == LOADING and loading_reached.reached is True

    loading_not = _server("loading-not", 8003)
    loading_not.config_served_at = NOW - 10
    loading_not.record_probe(False, now=NOW, grace_s=1800, fail_threshold=3, err="refused", reached=False)
    assert loading_not.state == LOADING and loading_not.reached is False

    awaiting = _server("awaiting", 8004)
    assert awaiting.state == AWAITING and awaiting.config_served_at is None

    stalled = _server("stalled", 8005)
    stalled.config_served_at = NOW - 9000
    stalled.record_probe(False, now=NOW, grace_s=1800, fail_threshold=3, err="timeout")
    assert stalled.state == STALLED

    servers = [healthy, loading_reached, loading_not, awaiting, stalled]
    deployments = [FleetDeployment.from_vllm(s) for s in servers]
    # The healthy box is already routed, so it reads "serving" (green).
    deployments[0].in_litellm = True
    fleet = Fleet(cfg=None, servers=servers, deployments=deployments)
    return pages, fleet


def test_fleet_rows_render_every_state():
    pages, fleet = _fleet()
    rows = pages.fleet_rows(fleet, admin=True, now=NOW)
    assert [r.name for r in rows] == ["healthy", "loading-reached", "loading-not", "awaiting", "stalled"]

    out = _render(rows)
    # Each state carries its badge text, and the two loading rows carry their
    # distinct reach details; awaiting carries its own detail line.
    assert ">healthy<" in out
    assert "vLLM up, model loading" in out
    assert "vLLM not listening yet" in out
    assert "has not requested its config" in out
    assert ">stalled<" in out
    # The loading badge text appears for both loading rows, not elsewhere.
    assert out.count(">loading<") == 2


def test_fleet_rows_catches_a_missing_field():
    """Drop one field a row carries and the template must fail loudly."""
    pages, fleet = _fleet()
    rows = pages.fleet_rows(fleet, admin=True, now=NOW)
    broken = [EasyDict({k: v for k, v in r.items() if k != "detail"}) for r in rows]
    try:
        _render(broken)
    except Exception as e:
        assert "detail" in str(e) or type(e).__name__ == "UnknownReference"
    else:
        raise AssertionError("rendering a row missing 'detail' unexpectedly succeeded")


def _render(rows) -> str:
    data = EasyDict(
        title="Fleet",
        is_site_admin=ezt.boolean(True),
        litellm_ui="",
        uid="u@example.apache.org",
        name="N",
        can_create_automation=ezt.boolean(False),
        projects=[],
        projects_label=None,
        admin_projects=[],
        flashes=[],
        repo="https://github.com/apache/tooling-llmao",
        commit="deadbeef",
        servers=rows,
    )
    t = ezt.Template(str(THIS_DIR / "templates" / "fleet.ezt"))
    buf = io.StringIO()
    t.generate(buf, data)
    return buf.getvalue()
