"""Fleet Server health state machine (no real vLLM)."""
import asyncio

from easydict import EasyDict as edict

from llmao.fleet import Fleet, Server


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
    return Server(**defaults)


def test_probe_serving():
    s = _server()
    s.record_probe(True, now=100.0, grace_s=1800, fail_threshold=3)
    assert s.state == Server.SERVING
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


def test_serving_needs_consecutive_fails():
    s = _server()
    s.record_probe(True, now=1.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=2.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=3.0, grace_s=1800, fail_threshold=3)
    assert s.state == Server.SERVING
    s.record_probe(False, now=4.0, grace_s=1800, fail_threshold=3)
    assert s.state == Server.DOWN


def test_model_health_aggregate():
    a = _server(name="a", listen_port=1)
    b = _server(name="b", listen_port=2, model_name="qwen3-8b")
    a.state = Server.SERVING
    b.state = Server.STARTING
    fleet = Fleet(cfg=None, servers=[a, b])
    assert fleet.model_health("gemma4-26b") == Fleet.BADGE_UP
    assert fleet.model_health("qwen3-8b") == Fleet.BADGE_STARTING
    assert fleet.model_health("nope") == ""


def test_note_config_fetch():
    fleet = Fleet(cfg=None, servers=[])
    fleet.note_config_fetch("primary", now=42.0)
    assert fleet.config_fetch_at["primary"] == 42.0


def test_box_json_listen_not_public():
    s = _server(gpu_memory_utilization=0.5, public_port=41234)
    body = s.box_json()
    assert body["model"] == "google/gemma"
    assert body["port"] == 8001
    assert body["gpu_memory_utilization"] == 0.5
    assert s.api_base == "http://10.0.0.1:41234"


def test_local_from_cfg_public_equals_listen():
    from pathlib import Path
    from llmao.models import load_model_list

    example = Path(__file__).resolve().parent.parent / "model_list.yaml.example"
    cfg = edict({
        "fleet": {
            "hosts": {"127.0.0.1": [["gemma4-26b", 8001]]},
            "health_interval_s": 45,
            "health_timeout_s": 3,
            "health_grace_s": 1800,
            "health_fail_threshold": 3,
            "skew_interval_s": 180,
            "litellm_health_interval_s": 14400,
        },
        "models_path": str(example),
    })
    fleet = Fleet.from_cfg(cfg, models=load_model_list(example))
    assert fleet.servers[0].listen_port == 8001
    assert fleet.servers[0].public_port == 8001
    assert fleet.servers[0].state == Server.PENDING


def test_probe_skips_without_public_port():
    s = _server(public_port=None)
    assert s.health_url is None
    cfg = edict({
        "fleet": edict({
            "health_timeout_s": 1,
            "health_grace_s": 1800,
            "health_fail_threshold": 3,
        })
    })

    class _Boom:
        async def get(self, url):
            raise AssertionError(f"must not probe {url}")

        async def aclose(self):
            return None

    fleet = Fleet(cfg=cfg, servers=[s])
    asyncio.run(fleet.probe_all(client=_Boom(), now=1.0))
    assert s.state == Server.PENDING
