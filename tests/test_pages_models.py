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

"""model_catalog_rows + models.ezt stay in lockstep.

The user catalog reads the roll-up (status, honest context, privacy,
concurrency) rather than the models.yaml catalog figures. EZT raises
UnknownReference on a name the row does not carry, so this renders the real
template.
"""

import io
import pathlib

import asfquart
import ezt
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import Fleet, FleetDeployment, VllmServer
from llmao.model_status import (
    AVAILABLE,
    DEGRADED,
    SKEW_VLLM_UP_LITELLM_DOWN,
    UNAVAILABLE,
)

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent
NOW = 10_000.0


def _app():
    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


def _server(name: str, *, host: str = "10.0.0.1", port: int = 8001, ok: bool = True) -> VllmServer:
    srv = VllmServer(
        model_name=name,
        name=name,
        host=host,
        listen_port=port,
        hf_model="org/model",
        api_key="sk-x",
        args=[],
        public_port=port,
    )
    srv.max_model_len = 40960
    srv.observed_max_model_len = 40960
    srv.kv_cache_tokens = 40960 * 4
    srv.config_served_at = NOW - 100
    # One success is Healthy. Three failures after that success is Unhealthy;
    # in_litellm must be set first or a failure with no config history is Awaiting.
    if ok:
        srv.record_probe(True, now=NOW - 50, grace_s=1800, fail_threshold=3)
    else:
        srv.record_probe(True, now=NOW - 50, grace_s=1800, fail_threshold=3, in_litellm=True)
        for _ in range(3):
            srv.record_probe(False, now=NOW, grace_s=1800, fail_threshold=3, err="refused", in_litellm=True)
    return srv


def _fleet(*servers) -> Fleet:
    cfg = edict(
        {
            "fleet": {
                "health_interval_s": 45,
                "health_grace_s": 1800,
                "health_fail_threshold": 3,
                "litellm_health_interval_s": 14400,
            }
        }
    )
    deployments = []
    for srv in servers:
        dep = FleetDeployment.from_vllm(srv)
        dep.in_litellm = True
        dep.litellm_healthy = True
        dep.litellm_health_at = NOW
        deployments.append(dep)
    return Fleet(cfg=cfg, servers=list(servers), deployments=deployments)


def _catalog(name: str, *, self_hosted: bool = True, modality: str = "text+vision") -> dict:
    return {
        "model_name": name,
        "display_name": name.upper(),
        "self_hosted": ezt.boolean(self_hosted),
        "hosting_label": "Self-hosted" if self_hosted else "External",
        "context_window": "999,999",
        "license": "Apache-2.0",
        "modality": modality,
        "supports_thinking": ezt.boolean(True),
        "thinks_by_default": ezt.boolean(False),
        "openness": "open",
        "notes": "",
        "reveal_supply": False,
        "weights_distribution": "",
        "training_data_provenance": "",
        "provenance_record": "",
    }


def test_catalog_shows_rollup_not_the_yaml_context():
    pages = _app()
    healthy = _server("gemma")
    down = _server("gemma", host="10.0.0.2", port=8002, ok=False)
    fleet = _fleet(healthy, down)
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], now=NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row.rollup == DEGRADED
    assert row.rollup_label == "Degraded"
    assert row.available
    assert row.degraded
    # The catalog said 999,999. The roll-up reports the served window.
    assert row.context_window == "40,960"
    # The unhealthy replica is not added. One healthy box, four slots.
    assert row.concurrency == "Handles up to 4 full-context requests at once"
    assert row.private
    assert "infrastructure we control" in row.privacy
    assert [c.label for c in row.chips] == ["Text", "Vision"]


def test_unavailable_self_hosted_has_no_concurrency():
    pages = _app()
    fleet = _fleet()
    rows = pages.model_catalog_rows(fleet, [_catalog("missing")], now=NOW)
    assert rows[0].rollup == UNAVAILABLE
    assert rows[0].unavailable
    assert not rows[0].available
    assert rows[0].concurrency == ""
    assert rows[0].context_window == "—"


def test_commercial_is_external_and_has_no_concurrency():
    pages = _app()
    dep = FleetDeployment.from_commercial(
        edict(
            model_name="claude",
            model_info=edict(self_hosted=False),
            litellm_params=edict(api_base="https://api.example"),
        )
    )
    dep.in_litellm = True
    dep.litellm_healthy = True
    dep.litellm_health_at = NOW
    cfg = edict(
        {
            "fleet": {
                "health_interval_s": 45,
                "health_grace_s": 1800,
                "health_fail_threshold": 3,
                "litellm_health_interval_s": 14400,
            }
        }
    )
    fleet = Fleet(cfg=cfg, servers=[], deployments=[dep])
    rows = pages.model_catalog_rows(fleet, [_catalog("claude", self_hosted=False, modality="text")], now=NOW)
    assert rows[0].rollup == AVAILABLE
    assert not rows[0].private
    assert "external provider" in rows[0].privacy
    assert rows[0].concurrency == ""
    assert [c.label for c in rows[0].chips] == ["Text"]


def test_attention_items_lists_only_actionable_items():
    pages = _app()
    healthy = _server("gemma")  # not actionable on its own
    down = _server("qwen", host="10.0.0.2", port=8002, ok=False)
    fleet = _fleet(healthy, down)
    next(d for d in fleet.deployments if d.vllm is down).skew.append(SKEW_VLLM_UP_LITELLM_DOWN)
    fleet.unknown_config_fetches["192.0.2.7"] = {"first_seen": NOW - 100, "last_seen": NOW - 5, "count": 3}

    kinds = [i.kind for i in pages.attention_items(fleet, now=NOW)]
    # Only the actionable kinds appear; the healthy box does not.
    assert "unknown" in kinds
    assert "unhealthy" in kinds
    assert "skew" in kinds
    assert "healthy" not in kinds

    items = pages.attention_items(fleet, now=NOW)
    unknown = next(i for i in items if i.kind == "unknown")
    assert "192.0.2.7" in unknown.what
    # The strip renders in the template.
    out = _render([pages.model_catalog_rows(fleet, [_catalog("gemma")], now=NOW, admin=True)[0]], attention=items)
    assert "Needs attention" in out
    assert "192.0.2.7" in out


def test_models_template_renders_the_catalog_row():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], now=NOW)
    out = _render(rows)
    assert "Available" in out
    assert "40,960 context" in out
    assert "Prompts stay on infrastructure we control." in out
    assert "Handles up to 4 full-context requests at once" in out
    assert 'title="Accepts images"' in out
    assert "Request a key" in out


def test_admin_rows_carry_their_models_deployments_and_summary():
    pages = _app()
    healthy = _server("gemma")
    down = _server("gemma", host="10.0.0.2", port=8002, ok=False)
    # A fresh box that has fetched config but not been probed: Loading.
    loading = VllmServer(
        model_name="qwen",
        name="qwen",
        host="10.0.0.3",
        listen_port=8003,
        hf_model="org/model",
        api_key="sk-x",
        args=[],
        public_port=8003,
    )
    loading.max_model_len = 8192
    loading.config_served_at = NOW - 10
    fleet = _fleet(healthy, down, loading)
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma"), _catalog("qwen")], now=NOW, admin=True)
    by_name = {r.model_name: r for r in rows}
    assert len(by_name["gemma"].deployments) == 2
    assert len(by_name["qwen"].deployments) == 1
    assert by_name["gemma"].replica_summary == "1 healthy / 1 unhealthy"
    assert by_name["qwen"].replica_summary == "1 loading"
    # The rows are the fleet rows: same shape fleet.ezt renders.
    assert pages.fleet_rows(fleet, admin=True, now=NOW)[0].model_name


def test_non_admin_rows_have_no_admin_fields():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], now=NOW, admin=False)
    assert not rows[0].keys() & {"deployments", "replica_summary"}


def _render(rows, attention=None) -> str:
    data = edict(
        title="Models",
        is_site_admin=ezt.boolean(False),
        reveal_supply=False,
        uid="u@example.apache.org",
        name="N",
        can_create_automation=ezt.boolean(False),
        projects=[],
        projects_label=None,
        admin_projects=[],
        flashes=[],
        repo="https://github.com/apache/tooling-llmao",
        commit="deadbeef",
        models=rows,
        attention=attention or [],
    )
    t = ezt.Template(str(THIS_DIR / "templates" / "models.ezt"))
    buf = io.StringIO()
    t.generate(buf, data)
    return buf.getvalue()
