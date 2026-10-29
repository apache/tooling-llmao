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
import time

import asfquart
import ezt
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import ORPHAN_PASSES, ExtraLiteLLM, Fleet, FleetDeployment, VllmServer, remove_refusal
from llmao.intent import Approval, provider_intent
from llmao.model_status import (
    AVAILABLE,
    AWAITING,
    CONFIGURED,
    DEGRADED,
    HEALTHY,
    LOADING,
    SKEW_IN_LITELLM_NOT_IN_CONFIG,
    SKEW_VLLM_UP_LITELLM_DOWN,
    STALLED,
    UNAVAILABLE,
    UNHEALTHY,
    UNKNOWN,
)
from llmao.models import load_models
from llmao.provisioning import UnknownFetch
from llmao.rental import ProviderRentals, Rental
from tests.test_fleet import EXAMPLE

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


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
    srv.config_served_at = time.time() - 100
    # One success is Healthy. Three failures after that success is Unhealthy;
    # in_litellm must be set first or a failure with no config history is Awaiting.
    if ok:
        srv.record_probe(True, now=time.time(), grace_s=1800, fail_threshold=3)
    else:
        srv.record_probe(True, now=time.time() - 1, grace_s=1800, fail_threshold=3, in_litellm=True)
        for _ in range(3):
            srv.record_probe(False, now=time.time(), grace_s=1800, fail_threshold=3, err="refused", in_litellm=True)
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
        dep.litellm_health_at = time.time()
        deployments.append(dep)
    return Fleet(cfg=cfg, servers=list(servers), deployments=deployments)


def _catalog(name: str, *, modality: str = "text+vision") -> dict:
    return {
        "model_name": name,
        "display_name": name.upper(),
        "good_for": "",
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
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")])
    assert len(rows) == 1
    row = rows[0]
    assert row.rollup == DEGRADED
    assert row.rollup_label == "Degraded"
    # nothing in the catalog says so; its deployments are on our boxes
    assert row.hosting_label == "Self-hosted"
    assert row.available
    assert row.degraded
    # The catalog said 999,999. The roll-up reports the served window.
    assert row.context_window == "40,960"
    # The unhealthy replica is not added. One healthy box, four slots.
    assert row.concurrency == "4 long requests at once"
    assert row.private
    assert row.privacy_lead == "Private"
    assert "infrastructure the ASF controls" in row.privacy
    assert [c.label for c in row.chips] == ["Text", "Vision"]


def test_unavailable_self_hosted_has_no_concurrency():
    pages = _app()
    fleet = _fleet()
    rows = pages.model_catalog_rows(fleet, [_catalog("missing")])
    assert rows[0].rollup == UNAVAILABLE
    assert rows[0].unavailable
    assert not rows[0].available
    assert rows[0].concurrency == ""
    assert rows[0].context_window == "—"


def test_commercial_is_external_and_has_no_concurrency():
    pages = _app()
    dep = FleetDeployment.from_intended(provider_intent("example", "claude", "anthropic/claude", "https://api.example"))
    dep.in_litellm = True
    dep.litellm_healthy = True
    dep.litellm_health_at = time.time()
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
    rows = pages.model_catalog_rows(fleet, [_catalog("claude", modality="text")])
    assert rows[0].rollup == AVAILABLE
    assert not rows[0].private
    assert rows[0].privacy_lead == "External"
    assert rows[0].hosting_label == "External"
    assert "third-party provider" in rows[0].privacy
    assert rows[0].concurrency == ""
    assert [c.label for c in rows[0].chips] == ["Text"]


def test_attention_items_lists_only_actionable_items():
    pages = _app()
    healthy = _server("gemma")  # not actionable on its own
    down = _server("qwen", host="10.0.0.2", port=8002, ok=False)
    fleet = _fleet(healthy, down)
    next(d for d in fleet.deployments if d.vllm is down).skew.append(SKEW_VLLM_UP_LITELLM_DOWN)
    fleet.provisioning.unknown["192.0.2.7"] = UnknownFetch("192.0.2.7", time.time() - 100, time.time() - 5, 3)

    items = pages.attention_items(fleet)
    kinds = [i.kind for i in items]
    # One item per deployment. Skew text is on that item, not a second row.
    assert "unknown" in kinds
    assert "unhealthy" in kinds
    assert "healthy" not in kinds
    unhealthy = next(i for i in items if i.kind == "unhealthy")
    assert "vLLM up, LiteLLM down" in unhealthy.detail
    unknown = next(i for i in items if i.kind == "unknown")
    assert "192.0.2.7" in unknown.what
    # The strip renders in the template.
    out = _render([pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True)[0]], attention=items)
    assert "Work needed" in out
    assert "192.0.2.7" in out


def test_models_template_renders_the_catalog_row():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")])
    out = _render(rows)
    assert "Available" in out
    assert "40,960 context" in out
    assert "infrastructure the ASF controls" in out
    assert "<strong>Private.</strong>" in out
    assert "4 long requests at once" in out
    assert 'title="Accepts images"' in out
    assert "Create a key" in out
    assert "Request a key" not in out


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
    loading.config_served_at = time.time() - 10
    fleet = _fleet(healthy, down, loading)
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma"), _catalog("qwen")], admin=True)
    by_name = {r.model_name: r for r in rows}
    assert len(by_name["gemma"].deployments) == 2
    assert len(by_name["qwen"].deployments) == 1
    assert by_name["gemma"].replica_summary == "1 healthy / 1 unhealthy"
    assert by_name["qwen"].replica_summary == "1 loading"
    # The rows are the deployment rows: same shape the drill-down renders.
    assert pages.deployment_rows(fleet, admin=True)[0].model_name


def test_non_admin_rows_have_no_admin_fields():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=False)
    assert not rows[0].keys() & {"deployments", "replica_summary"}


def test_awaiting_row_shows_the_last_probe():
    pages = _app()

    def fresh(host: str, port: int) -> VllmServer:
        return VllmServer(
            model_name="qwen",
            name="qwen",
            host=host,
            listen_port=port,
            hf_model="org/model",
            api_key="sk-x",
            args=[],
            public_port=port,
        )

    refused = fresh("10.0.0.4", 8001)
    refused.record_probe(
        False, now=time.time() - 14, grace_s=1800, fail_threshold=3, err="no connection", reached=False
    )
    # A 503 reaches vLLM, so that probe is the loading start: the row is Loading, not Awaiting.
    loading = fresh("10.0.0.5", 8001)
    loading.record_probe(False, now=time.time() - 14, grace_s=1800, fail_threshold=3, err="HTTP 503", reached=True)
    quiet = fresh("10.0.0.6", 8001)
    fleet = _fleet(refused, loading, quiet)
    rows = {r.listen: r for r in pages.deployment_rows(fleet, admin=True)}
    assert rows["10.0.0.4:8001"].last_ok == "never"
    assert rows["10.0.0.4:8001"].detail.startswith("probed ")
    assert rows["10.0.0.4:8001"].detail.endswith(", no connection")
    assert rows["10.0.0.5:8001"].state == LOADING
    assert rows["10.0.0.5:8001"].detail == "vLLM up, model loading"
    assert rows["10.0.0.6:8001"].detail == "has not requested its config"
    assert rows["10.0.0.6:8001"].last_ok == "never"


def test_admin_rows_of_an_approved_host_say_so():
    """The one mark an approval leaves on a row: why it is here when fleet.hosts does not name it."""
    pages = _app()
    fleet = _fleet(_server("gemma"))
    fleet.approvals["10.0.0.1"] = Approval("10.0.0.1", ("gemma",), time.time() - 20)
    rows = pages.deployment_rows(fleet, admin=True)
    assert rows[0].flag == "Approved · 20s ago"
    # Non-admin rows never expose it.
    for r in pages.deployment_rows(fleet, admin=False):
        assert r.flag == ""


def test_apply_box_approval_reports_the_models_and_refuses_a_bad_form():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    fleet.cfg.fleet.hosts = {}
    fleet.cfg.fleet.vllm_api_salt = "test-vllm-salt"
    fleet.models = {m.model_name: m for m in load_models(EXAMPLE)}
    ok, msg = pages.apply_box_approval(fleet, "", ["gemma4-26b"])
    assert not ok and "No host" in msg
    ok, msg = pages.apply_box_approval(fleet, "192.0.2.7", [])
    assert not ok and "at least one" in msg
    ok, msg = pages.apply_box_approval(fleet, "192.0.2.7", ["gemma4-26b"])
    assert ok and msg == "Approved 192.0.2.7 to load gemma4-26b."
    assert [s.host for s in fleet.servers] == ["10.0.0.1", "192.0.2.7"]


def test_admin_drill_down_renders_deployments_without_label_controls():
    pages = _app()
    healthy = _server("gemma")
    down = _server("gemma", host="10.0.0.2", port=8002, ok=False)
    fleet = _fleet(healthy, down)
    fleet.approvals["10.0.0.1"] = Approval("10.0.0.1", ("gemma",), time.time() - 20)
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True)
    out = _render(rows, admin=True)

    # The per-model deployment table is present, with one row per replica.
    assert "Approvals reset when the control plane restarts." in out
    assert "1 healthy / 1 unhealthy" in out
    assert "Approved ·" in out
    # Reboot is manual until a provider API can do it; there is no label to set or clear.
    assert "reboot" not in out.lower()
    assert "Clear flag" not in out
    assert "/do-set-admin-label" not in out


def test_attention_strip_offers_approve_for_an_unknown_box_and_nothing_else():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    fleet.provisioning.unknown["192.0.2.7"] = UnknownFetch("192.0.2.7", time.time() - 100, time.time() - 5, 3)
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True)
    out = _render(rows, attention=pages.attention_items(fleet), admin=True)
    assert "Fleet key presented from 192.0.2.7" in out
    assert "seen 5s · 3 requests" in out
    assert 'data-host="192.0.2.7"' in out
    assert 'data-bs-target="#approveBoxModal"' in out
    assert [a.label for a in pages.attention_items(fleet)[0].actions] == ["Approve"]
    out = _render(
        rows,
        attention=pages.attention_items(fleet),
        admin=True,
        approve_choices=[edict(model_name="gemma", display_name="Gemma")],
    )
    assert 'action="/do-approve-box"' in out
    assert 'value="gemma"' in out
    assert ">Gemma<" in out


def _orphan(passes: int, *, stamp: str = "", route_id: str = "orphan-1") -> ExtraLiteLLM:
    return ExtraLiteLLM(
        host="198.51.100.9",
        port=18490,
        model_name="gemma",
        skew=(SKEW_IN_LITELLM_NOT_IN_CONFIG,),
        route_id=route_id,
        api_base="http://198.51.100.9:18490",
        passes=passes,
        llmao_key=stamp,
    )


def test_attention_strip_shows_an_orphan_once_it_has_persisted_with_stop_tracking():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    fleet.cfg.fleet.hosts = {}
    # A debounce: one read is not an item.
    fleet.extra_litellm = [_orphan(ORPHAN_PASSES - 1)]
    assert pages.attention_items(fleet) == []
    fleet.extra_litellm = [_orphan(ORPHAN_PASSES)]
    (item,) = pages.attention_items(fleet)
    assert item.what == "198.51.100.9:18490 · gemma — in LiteLLM, not in config"
    assert item.detail == "not added by llmao; someone added it in LiteLLM"
    assert [a.label for a in item.actions] == ["Stop tracking"]
    out = _render(pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True), attention=[item], admin=True)
    assert out.count("/do-remove-deployment") == 1
    assert 'name="host" value="198.51.100.9"' in out
    assert 'name="name" value="gemma"' in out
    assert 'name="port" value="18490"' in out
    assert 'data-confirm="Stop tracking 198.51.100.9:18490?' in out
    # The post finds the orphan the way the form named it.
    assert pages._match_extra(fleet, "gemma", "198.51.100.9", "18490") is fleet.extra_litellm[0]
    assert pages._match_extra(fleet, "gemma", "198.51.100.9", "8001") is None
    # No id to delete by: the button is there, disabled, with the reason as text.
    fleet.extra_litellm = [_orphan(ORPHAN_PASSES, route_id="")]
    out = _render([], attention=pages.attention_items(fleet), admin=True)
    assert "LiteLLM reported it without an id" in out
    assert "disabled" in out


def test_a_stamped_orphan_on_a_listed_host_offers_approve():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    fleet.cfg.fleet.hosts = {}
    fleet.extra_litellm = [_orphan(ORPHAN_PASSES, stamp="http://198.51.100.9:8001")]
    (item,) = pages.attention_items(fleet)
    assert item.detail.startswith("llmao added it")
    # No listing names the host: nothing to approve, the box is invisible.
    assert [a.label for a in item.actions] == ["Stop tracking"]
    rental = Rental(
        provider="vast",
        id="1",
        label="x",
        url="",
        usd_per_hour=None,
        host="198.51.100.9",
        name="198.51.100.9",
        status="running",
    )
    fleet.vast_rentals = ProviderRentals("vast", by_id={"1": rental})
    (item,) = pages.attention_items(fleet)
    assert [a.label for a in item.actions] == ["Approve", "Stop tracking"]
    # A host in fleet.hosts is intended already; this is a route for a model it does not run.
    fleet.cfg.fleet.hosts = {"198.51.100.9": 8001}
    assert [a.label for a in pages.attention_items(fleet)[0].actions] == ["Stop tracking"]


def test_the_strip_shows_a_rental_nobody_intends_with_approve_by_name():
    from llmao.fleet import Discovered

    pages = _app()
    fleet = _fleet(_server("gemma"))
    vast = Rental(
        provider="Vast",
        id="1",
        label="1x RTX 4090",
        url="",
        usd_per_hour=0.4,
        host="198.51.100.8",
        name="198.51.100.8",
        status="running",
    )
    pod = Rental(
        provider="RunPod",
        id="pod-1",
        label="llmao-q",
        url="",
        usd_per_hour=0.6,
        host="203.0.113.7",
        name="pod-1",
        status="running",
    )
    fleet.discovered = {
        "198.51.100.8": Discovered(vast, "8001: serving gemma with our key", time.time()),
        "pod-1": Discovered(pod, "nothing listening", time.time()),
    }
    items = pages.attention_items(fleet)
    assert [i.what for i in items] == [
        "198.51.100.8 · rented, nobody intends it",
        "pod-1 · rented, nobody intends it",
    ]
    assert items[0].detail == "Vast · 1x RTX 4090 (1) — 8001: serving gemma with our key"
    assert items[1].detail == "RunPod · llmao-q (pod-1) — nothing listening"
    # Approve is by the name the box sends: the address on Vast, the pod id on RunPod.
    assert [[a.label for a in i.actions] for i in items] == [["Approve"], ["Approve"]]
    out = _render(pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True), attention=items, admin=True)
    assert out.count('data-bs-target="#approveBoxModal"') == 2
    assert 'data-host="198.51.100.8"' in out
    assert 'data-host="pod-1"' in out


def test_remove_refusal_gate():
    # self-hosted, in LiteLLM, still healthy: too early to drop.
    healthy = _fleet(_server("gemma"))
    assert remove_refusal(healthy.deployments[0]) == "vLLM is still healthy"

    # self-hosted, in LiteLLM, unhealthy: the drop is allowed.
    down = _fleet(_server("gemma", ok=False))
    assert remove_refusal(down.deployments[0]) is None

    # self-hosted but not in LiteLLM: there is nothing to drop.
    absent = _fleet(_server("gemma"))
    absent.deployments[0].in_litellm = False
    assert remove_refusal(absent.deployments[0]) == "not in LiteLLM"

    # a commercial row is never manually removed.
    commercial = _fleet(_server("gemma"))
    commercial.deployments[0].self_hosted = False
    assert remove_refusal(commercial.deployments[0]) == "not a self-hosted deployment"


def test_drill_down_renders_add_and_remove_actions():
    pages = _app()
    a = _server("gemma", host="10.0.0.1")  # healthy, in LiteLLM -> disabled Remove
    b = _server("gemma", host="10.0.0.2")  # healthy, not in LiteLLM -> enabled Add
    c = _server("gemma", host="10.0.0.3", ok=False)  # unhealthy, in LiteLLM -> enabled Remove
    d = _server("gemma", host="10.0.0.4", ok=False)  # unhealthy, not in LiteLLM -> disabled Add
    fleet = _fleet(a, b, c, d)
    for dep in fleet.deployments:  # _fleet defaults every box into LiteLLM
        if dep.vllm.host in ("10.0.0.2", "10.0.0.4"):
            dep.in_litellm = False
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True)
    out = _render(rows, admin=True)

    # Exactly one enabled Add (the healthy box not yet in LiteLLM) posts the row.
    assert out.count("/do-add-deployment") == 1
    assert 'name="host" value="10.0.0.2"' in out
    assert 'aria-label="Add 10.0.0.2:8001"' in out
    # The yellow badge says why, and Add says what it does.
    assert "Not in LiteLLM yet, so no traffic. llmao adds it on its next pass; Add does it now." in out
    assert 'title="Register this server in LiteLLM now, so it gets traffic"' in out

    # Exactly one enabled Stop tracking (the unhealthy box still in LiteLLM) posts the row.
    assert out.count("/do-remove-deployment") == 1
    assert 'name="host" value="10.0.0.3"' in out
    assert "Stop tracking" in out
    assert 'data-confirm="Stop tracking 10.0.0.3:8001?' in out

    # Actions that do not apply are not rendered in the Deployments list.
    assert "disabled" not in out
    assert "vLLM is not serving yet" not in out
    assert "vLLM is still healthy" not in out

    # The attention strip keeps them, disabled, with the reason as text.
    out = _render(rows, attention=pages.attention_items(fleet), admin=True)
    assert "vLLM is not serving yet" in out


def test_serving_but_not_in_litellm_says_why():
    pages = _app()
    failed = _server("gemma", host="10.0.0.2")
    unplaced = _server("gemma", host="10.0.0.3")
    unplaced.public_port = None
    fleet = _fleet(failed, unplaced)
    for dep in fleet.deployments:
        dep.in_litellm = False
    fleet.deployments[0].litellm_error = "LiteLLM said no"
    fleet.deployments[0].litellm_error_at = time.time() - 120
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True)
    out = _render(rows, admin=True)
    assert "Adding it failed 2m ago: LiteLLM said no" in out
    assert "Added once its public address is known." in out
    # No address, no Add: there is nothing it could register.
    assert out.count("/do-add-deployment") == 1


def test_two_ports_on_one_host_are_two_deployments():
    pages = _app()
    up = _server("gemma", host="10.0.0.9", port=8001)
    down = _server("qwen", host="10.0.0.9", port=8003, ok=False)
    also_down = _server("gemma", host="10.0.0.9", port=8005, ok=False)
    fleet = _fleet(up, down, also_down)

    items = pages.attention_items(fleet)
    assert [i.ident for i in items] == ["10.0.0.9:8003", "10.0.0.9:8005"]
    assert items[0].what == "10.0.0.9:8003 · qwen — unhealthy"
    assert items[0].anchor == "dep-10-0-0-9-8003"

    rows = pages.deployment_rows(fleet, admin=True)
    assert len({r.anchor for r in rows}) == 3
    assert [bool(r.attention) for r in rows] == [False, True, True]

    out = _render(
        pages.model_catalog_rows(fleet, [_catalog("gemma"), _catalog("qwen")], admin=True),
        attention=items,
        admin=True,
    )
    assert 'href="#dep-10-0-0-9-8003"' in out
    assert 'id="dep-10-0-0-9-8003"' in out
    assert 'id="dep-10-0-0-9-8001"' in out

    # host + name alone still finds a deployment; the port tells two apart.
    assert len(pages._match_deployments(fleet, "qwen", "10.0.0.9")) == 1
    assert len(pages._match_deployments(fleet, "gemma", "10.0.0.9")) == 2
    assert [d.vllm for d in pages._match_deployments(fleet, "gemma", "10.0.0.9", "8005")] == [also_down]


def test_healthy_row_has_one_menu_entry_and_no_clutter():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    row = pages.deployment_rows(fleet, admin=True)[0]
    assert row.primary == []
    assert row.menu == []
    assert row.badge_class == "text-bg-success"
    # The diagnostics line leaves out anything it has no value for.
    assert all(d.value and d.value != "—" for d in row.diags)
    assert "Context" in [d.label for d in row.diags]
    facts = {d.label: d for d in row.diags}
    assert facts["vLLM"].value.startswith("last healthy ") and facts["vLLM"].value.endswith(" ago")
    assert not any(d.bad for d in row.diags)


def test_diagnostics_say_a_failed_litellm_check_plainly():
    pages = _app()
    fleet = _fleet(_server("gemma"))
    dep = fleet.deployments[0]
    dep.litellm_healthy = False
    dep.litellm_health_at = time.time() - 300
    row = pages.deployment_rows(fleet, admin=True)[0]
    facts = {d.label: d for d in row.diags}
    assert facts["LiteLLM"].value == "check failed 5m ago"
    assert facts["LiteLLM"].bad
    out = _render(pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True), admin=True)
    assert "health unhealthy" not in out
    assert "dep-fact-bad" in out
    out = _render(pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True), admin=True)
    assert "disabled" not in out
    assert "<table" not in out
    assert "—" not in out.split('<div class="list-group dep-list')[1].split("modelDetailModal")[0]


def test_awaiting_row_has_no_diagnostics_line():
    pages = _app()
    quiet = VllmServer(
        model_name="qwen",
        name="qwen",
        host="10.0.0.6",
        listen_port=8001,
        hf_model="org/model",
        api_key="sk-x",
        args=[],
        public_port=8001,
    )
    fleet = _fleet(quiet)
    fleet.deployments[0].in_litellm = False
    row = pages.deployment_rows(fleet, admin=True)[0]
    assert row.state == AWAITING
    assert row.diags == []
    assert row.detail == "has not requested its config"
    out = _render(pages.model_catalog_rows(fleet, [_catalog("qwen")], admin=True), admin=True)
    assert "dep-diag" not in out
    assert "KV cache" not in out


def test_every_state_has_a_badge_class():
    pages = _app()
    for state in (AWAITING, LOADING, HEALTHY, UNHEALTHY, STALLED, UNKNOWN, CONFIGURED, "pending", "no deployment"):
        assert pages._badge_class(state).startswith("text-bg-")
    # Serving but not routed is a warning, not a success.
    assert pages._badge_class(HEALTHY, routed=False) == "text-bg-warning"


def test_card_copies_the_model_id_from_an_icon():
    pages = _app()
    rows = pages.model_catalog_rows(_fleet(_server("gemma")), [_catalog("gemma")])
    out = _render(rows)
    button = out[out.index("js-copy-model") :]
    button = button[: button.index("</button>")]
    assert 'title="Copy gemma to the clipboard"' in button
    assert 'aria-label="Copy gemma to the clipboard"' in button
    assert 'data-copy="gemma"' in button
    assert "bi-clipboard" in button
    # The icon is the whole content; the name is in the attributes.
    assert button.rstrip().endswith("</i>")


def test_mixed_card_and_unavailable_card():
    pages = _app()
    box = _server("gemma")
    fleet = _fleet(box)
    commercial = FleetDeployment.from_intended(
        provider_intent("example", "gemma", "example/gemma", "https://api.example")
    )
    fleet.deployments.append(commercial)
    rows = pages.model_catalog_rows(fleet, [_catalog("gemma"), _catalog("missing")])
    out = _render(rows)
    assert "<strong>Mixed.</strong>" in out
    assert "you can't choose which" in out
    assert "model-privacy-mixed" in out
    # The unavailable card keeps its button, disabled, and says why once.
    assert "disabled>Create a key</button>" in out
    assert out.count(">Unavailable<") == 1
    assert "Unavailable right now" not in out


def test_thinking_chip_says_when_it_is_the_default():
    pages = _app()
    quiet = _catalog("gemma")
    loud = _catalog("qwen")
    loud["thinks_by_default"] = ezt.boolean(True)
    out = _render(pages.model_catalog_rows(_fleet(_server("gemma"), _server("qwen")), [quiet, loud]))
    assert out.count('unless you turn it off">Thinks by default<') == 1
    assert out.count('only when asked">Thinking<') == 1


def test_cards_put_degraded_after_available_and_unavailable_last():
    pages = _app()
    fleet = _fleet(_server("alpha"), _server("beta"), _server("beta", host="10.0.0.9", ok=False))
    rows = pages.model_catalog_rows(fleet, [_catalog("aaa-missing"), _catalog("beta"), _catalog("alpha")])
    assert [r.model_name for r in rows] == ["alpha", "beta", "aaa-missing"]
    assert rows[1].degraded


def _render(rows, attention=None, *, admin=False, approve_choices=None) -> str:
    data = edict(
        title="Models",
        is_site_admin=ezt.boolean(admin),
        nav="models",
        login_back="/models",
        restarted_at="10/08 14:30 (UTC)",
        reveal_supply=False,
        uid="u@example.apache.org",
        name="N",
        can_create_automation=ezt.boolean(False),
        projects=[],
        projects_label=None,
        admin_projects=[],
        flashes=[],
        work=None,
        notices=[],
        repo="https://github.com/apache/tooling-llmao",
        commit="deadbeef",
        models=rows,
        attention=attention or [],
        approve_choices=approve_choices or [],
    )
    t = ezt.Template(str(THIS_DIR / "templates" / "models.ezt"))
    buf = io.StringIO()
    t.generate(buf, data)
    return buf.getvalue()


def test_a_card_says_what_its_model_is_good_for():
    pages = _app()
    catalog = _catalog("qwen3-8b")
    catalog["good_for"] = "Simple questions, quick edits, classification and other routine calls. Fast and cheap."
    rows = pages.model_catalog_rows(_fleet(_server("qwen3-8b")), [catalog], admin=False)
    out = _render(rows)
    assert "Good for:</span> Simple questions, quick edits" in out
    # No line at all when a model says nothing.
    assert "Good for:" not in _render(
        pages.model_catalog_rows(_fleet(_server("qwen3-8b")), [_catalog("qwen3-8b")], admin=False)
    )


def test_each_deployment_says_where_to_find_it():
    pages = _app()
    vast, runpod, lost = (
        _server("gemma", host="10.0.0.1"),
        _server("gemma", host="10.0.0.2"),
        _server("gemma", host="10.0.0.3"),
    )
    vast.rental = Rental(
        provider="Vast",
        id="53865410",
        label="RTX 3060 x2",
        url="https://cloud.vast.ai/instances/",
        usd_per_hour=0.14,
        host="10.0.0.1",
        name="10.0.0.1",
        status="running",
        ports=edict({"8001": 41234}),
    )
    runpod.rental = Rental(
        provider="RunPod",
        id="6jqrsngxdor2mc",
        label="llmao-qwen3-8b",
        url="",
        usd_per_hour=0.89,
        host="",
        name="6jqrsngxdor2mc",
        status="exited",
    )
    rows = pages.model_catalog_rows(_fleet(vast, runpod, lost), [_catalog("gemma")], admin=True)
    out = _render(rows, admin=True)
    assert '<a href="https://cloud.vast.ai/instances/"' in out
    assert "Vast · RTX 3060 x2 (53865410)</a>" in out
    # No link for RunPod yet; the pod is named so it can be found. Listed,
    # not running: the provider's status follows the name.
    assert "RunPod · llmao-qwen3-8b (6jqrsngxdor2mc), exited" in out
    assert "RunPod · llmao-qwen3-8b (6jqrsngxdor2mc), exited</a>" not in out
    # A host no provider lists stands out.
    assert "location unknown: no provider lists this host" in out
    assert "text-warning-emphasis" in out


def test_where_a_deployment_lives_is_for_admins_only():
    pages = _app()
    srv = _server("gemma", host="10.0.0.1")
    srv.rental = Rental(
        provider="Vast",
        id="53865410",
        label="RTX 3060 x2",
        url="https://cloud.vast.ai/instances/",
        usd_per_hour=0.14,
        host="10.0.0.1",
        name="10.0.0.1",
        status="running",
        ports=edict({"8001": 41234}),
    )
    out = _render(pages.model_catalog_rows(_fleet(srv), [_catalog("gemma")], admin=False))
    assert "53865410" not in out


def test_an_unlisted_host_says_why_and_is_work_needed():
    pages = _app()
    gone = _server("gemma", host="10.0.0.9", ok=False)
    gone.unlisted = True
    fleet = _fleet(gone)
    reason = "No Vast or RunPod instance has this address; the rental may have ended."
    assert pages._deployment_reason(fleet.deployments[0]) == reason
    (item,) = [i for i in pages.attention_items(fleet) if i.ident == "10.0.0.9:8001"]
    assert item.kind == "unlisted"
    assert item.detail == reason + " If so, remove it from fleet.hosts."
    out = _render(pages.model_catalog_rows(fleet, [_catalog("gemma")], admin=True), admin=True)
    assert "has not requested its config" not in out


def test_a_gone_box_has_no_buttons():
    """Nothing to reboot or add; llmao drops its LiteLLM deployment by itself."""
    pages = _app()
    gone = _server("gemma", host="10.0.0.9")
    gone.unlisted = True
    fleet = _fleet(gone)
    dep = fleet.deployments[0]
    for in_litellm in (True, False):
        dep.in_litellm = in_litellm
        assert pages.admin_actions(fleet, dep) == []


def test_an_unlisted_host_still_serving_gets_no_remove_sentence():
    # A listing gap with the box answering: the listing is in doubt, not the
    # rental. The item shows, the deployment stays, nothing says "remove".
    pages = _app()
    srv = _server("gemma", host="10.0.0.9", ok=True)
    srv.unlisted = True
    fleet = _fleet(srv)
    (item,) = [i for i in pages.attention_items(fleet) if i.ident == "10.0.0.9:8001"]
    assert item.kind == "unlisted"
    assert item.detail == "No Vast or RunPod instance has this address, but it is serving; the listing may be behind."
    assert pages.admin_actions(fleet, fleet.deployments[0]) == []


def test_an_unlisted_host_never_reached_gets_the_sentence_once_the_grace_is_out():
    pages = _app()
    srv = _server("gemma", host="10.0.0.9")
    srv.config_served_at = None
    srv.last_ok = None
    srv.last_probe_ok = None
    srv.unlisted = True
    fleet = _fleet(srv)
    dep = fleet.deployments[0]
    dep.in_litellm = False  # the deployment is already gone
    reason = "No Vast or RunPod instance has this address; the rental may have ended."

    srv.record_probe(False, now=time.time() - 60, grace_s=1800, fail_threshold=3, err="no connection", reached=False)
    (item,) = [i for i in pages.attention_items(fleet) if i.ident == "10.0.0.9:8001"]
    assert "awaiting" in item.rest
    assert item.detail == reason

    srv.first_probed_at = time.time() - 1801
    (item,) = [i for i in pages.attention_items(fleet) if i.ident == "10.0.0.9:8001"]
    assert "stalled" in item.rest
    assert item.detail == reason + " If so, remove it from fleet.hosts."


def test_a_box_that_rejects_our_key_is_work_needed_with_the_fix_named():
    from llmao.model_status import FOREIGN, REJECTED

    pages = _app()
    srv = _server("gemma", host="10.0.0.9", ok=True)
    srv.record_probe(True, now=time.time(), grace_s=1800, fail_threshold=3, key_rejected=True)
    assert srv.state == REJECTED
    fleet = _fleet(srv)
    dep = fleet.deployments[0]
    dep.in_litellm = False  # the first 401 dropped it
    (item,) = [i for i in pages.attention_items(fleet) if i.ident == "10.0.0.9:8001"]
    assert item.kind == "rejected"
    assert "rejected" in item.rest
    assert (
        item.detail == "vLLM is up but rejects our key, so it was dropped from LiteLLM. Check the api key on the box."
    )
    (add,) = [a for a in pages.admin_actions(fleet, dep) if a.kind == "add"]
    assert (add.kind, add.enabled, add.reason) == ("add", ezt.boolean(False), "vLLM rejects our key")

    # Unlisted as well, for long enough: Foreign, and the sentence.
    srv.unlisted = True
    for n in (1, 2, 3):
        srv.record_probe(True, now=time.time() + n, grace_s=1800, fail_threshold=3, key_rejected=True)
    assert srv.state == FOREIGN
    (item,) = [i for i in pages.attention_items(fleet) if i.ident == "10.0.0.9:8001"]
    assert item.kind == "unlisted"
    assert item.detail == (
        "No Vast or RunPod instance has this address, and the vLLM there rejects our key; "
        "it may be someone else's now. If so, remove it from fleet.hosts."
    )
    assert pages.admin_actions(fleet, dep) == []
