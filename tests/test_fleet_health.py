"""Fleet Server health state machine (no real vLLM)."""
from llmao.fleet import Fleet, Server


def _server(**kwargs):
    defaults = dict(
        model_name="gemma4-26b",
        name="gemma4-26b",
        host="10.0.0.1",
        port=8001,
        hf_model="google/gemma",
        api_key="sk-x",
        args=[],
    )
    defaults.update(kwargs)
    return Server(**defaults)


def test_probe_healthy():
    s = _server()
    s.record_probe(True, now=100.0, grace_s=1800, fail_threshold=3)
    assert s.state == Server.HEALTHY
    assert s.last_ok == 100.0


def test_starting_inside_grace():
    s = _server()
    s.seen_at = 0.0
    s.record_probe(False, now=10.0, grace_s=1800, fail_threshold=3, err="HTTP 503")
    assert s.state == Server.STARTING


def test_down_after_grace():
    s = _server()
    s.seen_at = 0.0
    s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="timeout")
    assert s.state == Server.DOWN


def test_healthy_needs_consecutive_fails():
    s = _server()
    s.record_probe(True, now=1.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=2.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=3.0, grace_s=1800, fail_threshold=3)
    assert s.state == Server.HEALTHY
    s.record_probe(False, now=4.0, grace_s=1800, fail_threshold=3)
    assert s.state == Server.DOWN


def test_model_health_aggregate():
    a = _server(name="a", port=1)
    b = _server(name="b", port=2, model_name="qwen3-8b")
    a.state = Server.HEALTHY
    b.state = Server.STARTING
    fleet = Fleet(cfg=None, servers=[a, b])
    assert fleet.model_health("gemma4-26b") == Fleet.BADGE_UP
    assert fleet.model_health("qwen3-8b") == Fleet.BADGE_STARTING
    assert fleet.model_health("nope") == ""


def test_note_config_fetch():
    fleet = Fleet(cfg=None, servers=[])
    fleet.note_config_fetch("primary", now=42.0)
    assert fleet.config_fetch_at["primary"] == 42.0


def test_box_json_method():
    s = _server(gpu_memory_utilization=0.5)
    body = s.box_json()
    assert body["model"] == "google/gemma"
    assert body["port"] == 8001
    assert body["gpu_memory_utilization"] == 0.5
