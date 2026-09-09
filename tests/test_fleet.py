"""config_for_host: JSON for a client IP from fleet.hosts + catalog."""
from pathlib import Path

import pytest
import yaml
from easydict import EasyDict as edict

from llmao.fleet import (
    UnknownHost,
    client_ip,
    config_for_host,
    normalize_peer_ip,
    validate_fleet,
)
from llmao.models import load_model_list
from llmao.litellm_client import _norm_base

EXAMPLE = Path(__file__).resolve().parent.parent / "model_list.yaml.example"
EXAMPLE_CFG = Path(__file__).resolve().parent.parent / "config.yaml.example"

FLEET_KNOBS = {
    "health_interval_s": 45,
    "health_timeout_s": 3,
    "health_grace_s": 1800,
    "health_fail_threshold": 3,
    "skew_interval_s": 180,
    "litellm_health_interval_s": 14400,
}


def _cfg(hosts, models_path=EXAMPLE):
    return edict({
        "fleet": {"hosts": hosts, **FLEET_KNOBS},
        "models_path": str(models_path),
    })


def test_example_primary_host():
    cfg = edict(yaml.safe_load(EXAMPLE_CFG.read_text(encoding="utf-8")))
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("127.0.0.1", models=models, cfg=cfg)
    assert payload["host"] == "127.0.0.1"
    assert "set_id" not in payload
    names = [s["name"] for s in payload["servers"]]
    assert "gemma4-26b" in names
    assert "qwen3-8b" in names
    gemma = next(s for s in payload["servers"] if s["name"] == "gemma4-26b")
    assert gemma["model"] == "google/gemma-4-26B-A4B-it"
    assert gemma["host"] == "127.0.0.1"
    assert gemma["port"] == 8001


def test_unknown_host():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    with pytest.raises(UnknownHost):
        config_for_host("10.0.0.9", models=models, cfg=cfg)


def test_optional_name_two_copies():
    cfg = _cfg({
        "10.0.0.1": [
            ["qwen3-8b", 8003],
            ["qwen3-8b", 8004, "qwen3-8b-b"],
        ]
    })
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert [s["name"] for s in payload["servers"]] == ["qwen3-8b", "qwen3-8b-b"]
    assert [s["port"] for s in payload["servers"]] == [8003, 8004]


def test_duplicate_name():
    cfg = _cfg({
        "10.0.0.1": [
            ["qwen3-8b", 8003],
            ["qwen3-8b", 8004],
        ]
    })
    with pytest.raises(ValueError, match="duplicate name"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_duplicate_port():
    cfg = _cfg({
        "10.0.0.1": [
            ["gemma4-26b", 8001],
            ["qwen3-8b", 8001],
        ]
    })
    with pytest.raises(ValueError, match="duplicate port"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_unknown_model():
    cfg = _cfg({"10.0.0.1": [["nope", 9]]})
    with pytest.raises(ValueError, match="unknown model"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_normalize_peer_ip():
    assert normalize_peer_ip("::ffff:203.0.113.10") == "203.0.113.10"
    assert normalize_peer_ip("203.0.113.10") == "203.0.113.10"


def test_client_ip_xff_leftmost():
    assert client_ip(remote_addr="10.0.0.1", forwarded_for="203.0.113.10, 10.0.0.1") == "203.0.113.10"


def test_client_ip_no_xff():
    assert client_ip(remote_addr="203.0.113.10", forwarded_for=None) == "203.0.113.10"
    assert client_ip(remote_addr="::ffff:203.0.113.10", forwarded_for="") == "203.0.113.10"


def test_norm_base_strips_trailing_v1():
    """LiteLLM appends /v1/chat/completions to api_base.

    A base that already ends in /v1 resolves to /v1/v1/..., which 404s at the
    origin. LiteLLM cannot parse that and reports an OpenAI authentication
    error naming platform.openai.com -- so the symptom points at the caller's
    key rather than at the route. This happened in production against the
    Fastly-fronted Gemma endpoint.
    """
    assert _norm_base("https://llm.tooling.apache.org/v1") == "https://llm.tooling.apache.org"
    assert _norm_base("https://llm.tooling.apache.org/v1/") == "https://llm.tooling.apache.org"
    assert _norm_base("http://127.0.0.1:8003/v1") == "http://127.0.0.1:8003"


def test_norm_base_leaves_other_paths_alone():
    """Only a trailing /v1 is stripped -- not a path that merely contains it."""
    assert _norm_base("http://127.0.0.1:8003") == "http://127.0.0.1:8003"
    assert _norm_base("http://127.0.0.1:8003/") == "http://127.0.0.1:8003"
    assert _norm_base("https://example.com/v1/proxy") == "https://example.com/v1/proxy"
    assert _norm_base("") == ""
    assert _norm_base(None) == ""


def test_norm_base_makes_skew_comparison_match():
    """The comparison this exists for.

    check_config_skew compares Server.api_base against what LiteLLM reports.
    Server.api_base is host:port with no suffix; a catalog entry may carry
    /v1. Without normalisation on both sides they never match, and the skew
    check reports a missing route that is actually present.
    """
    assert _norm_base("http://100.105.28.100:8003/v1") == _norm_base("http://100.105.28.100:8003")


def test_from_row_falls_back_to_fleet_api_key():
    """Empty means vLLM starts unauthenticated on a public port."""
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003]]})
    cfg.fleet.selfhost_api_key = "sk-fleet"
    models = load_model_list(EXAMPLE)          # catalog carries no api_key
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert payload["servers"][0]["api_key"] == "sk-fleet"