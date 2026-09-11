"""Fleet VllmServer health state machine (no real vLLM)."""
import asyncio

from easydict import EasyDict as edict

from llmao.fleet import parse_kv_cache_tokens, parse_max_model_len, Fleet, VllmServer


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
    assert s.state == VllmServer.SERVING
    assert s.last_ok == 100.0


def test_starting_inside_grace():
    s = _server()
    s.seen_at = 0.0
    s.record_probe(False, now=10.0, grace_s=1800, fail_threshold=3, err="HTTP 503")
    assert s.state == VllmServer.STARTING


def test_down_after_grace():
    s = _server()
    s.seen_at = 0.0
    s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="timeout")
    assert s.state == VllmServer.DOWN


def test_serving_needs_consecutive_fails():
    s = _server()
    s.record_probe(True, now=1.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=2.0, grace_s=1800, fail_threshold=3)
    s.record_probe(False, now=3.0, grace_s=1800, fail_threshold=3)
    assert s.state == VllmServer.SERVING
    s.record_probe(False, now=4.0, grace_s=1800, fail_threshold=3)
    assert s.state == VllmServer.DOWN


def test_model_health_aggregate():
    a = _server(name="a", listen_port=1)
    b = _server(name="b", listen_port=2, model_name="qwen3-8b")
    a.state = VllmServer.SERVING
    b.state = VllmServer.STARTING
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
    assert fleet.servers[0].state == VllmServer.PENDING


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
    assert s.state == VllmServer.PENDING


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
    before vLLM listens. The comparison is `(now - seen_at) < grace_s`, so
    1799 is still STARTING and 1800 is DOWN. Pinned because moving to <= would
    shave a second off a window that was chosen deliberately, and the failure
    would look like a flaky box rather than an off-by-one.
    """
    s = _server()
    s.seen_at = 0.0
    s.record_probe(False, now=1799.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == VllmServer.STARTING

    s.record_probe(False, now=1800.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == VllmServer.DOWN


def test_recovers_from_down_on_a_single_probe():
    """DOWN -> SERVING takes one success, and clears the fail counter.

    Asymmetric on purpose: three failures to go down, one success to come
    back. That is the right bias for health-gated registration -- withholding
    a working server is worse than briefly trusting a flaky one -- but it is
    worth stating rather than leaving as an accident of the code.
    """
    s = _server()
    s.seen_at = 0.0
    for _ in range(3):
        s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == VllmServer.DOWN
    assert s.fails == 3

    s.record_probe(True, now=2100.0, grace_s=1800, fail_threshold=3)
    assert s.state == VllmServer.SERVING
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
    s.seen_at = 0.0
    now = 100.0
    for _ in range(10):
        s.record_probe(False, now=now, grace_s=1800, fail_threshold=3, err="refused")
        now += 45.0
        s.record_probe(True, now=now, grace_s=1800, fail_threshold=3)
        now += 45.0

    assert s.state == VllmServer.SERVING
    assert s.fails == 0


def test_down_stays_down_while_failing():
    """Once DOWN, further failures do not reset the grace window.

    The grace branch is only reached when state is not SERVING, and by then
    now - seen_at is far past grace_s -- so a long-dead box cannot slip back
    into STARTING and look like it is merely booting.
    """
    s = _server()
    s.seen_at = 0.0
    for _ in range(3):
        s.record_probe(False, now=2000.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == VllmServer.DOWN

    s.record_probe(False, now=9999.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == VllmServer.DOWN
    assert s.fails == 4


def test_never_healthy_box_goes_starting_then_down():
    """The provisioning failure case: wrong port, or vLLM crash-looping.

    Stays STARTING for the whole grace window so a genuinely slow start is
    not misreported, then goes DOWN once. Registration should therefore never
    have fired for it.
    """
    s = _server()
    s.seen_at = 0.0
    now = 45.0
    while now < 1800.0:
        s.record_probe(False, now=now, grace_s=1800, fail_threshold=3, err="refused")
        assert s.state == VllmServer.STARTING, f"flipped early at {now}s"
        now += 45.0

    s.record_probe(False, now=1800.0, grace_s=1800, fail_threshold=3, err="refused")
    assert s.state == VllmServer.DOWN


_METRICS = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="google/gemma-4-26B-A4B-it"} 0.0
# HELP vllm:cache_config_info Information of the LLMEngine CacheConfig
# TYPE vllm:cache_config_info gauge
vllm:cache_config_info{block_size="16",cache_dtype="auto",enable_prefix_caching="True",gpu_memory_utilization="0.9",kv_cache_max_concurrency="6.52951355508556",kv_cache_memory_bytes="None",kv_cache_size_tokens="855836",num_cpu_blocks="None",num_gpu_blocks="33960"} 1.0
"""


def test_parse_kv_cache_tokens_prefers_the_reported_count():
    """kv_cache_size_tokens is authoritative; the block product is not.

    Real output from the A100 serving Gemma reports 855,836 tokens while
    num_gpu_blocks * block_size gives 543,360 -- blocks do not map uniformly
    to tokens for a model with heterogeneous head dimensions. Taking the
    product would understate the cache by a third and falsely flag a
    correctly-sized server as oversized.
    """
    assert parse_kv_cache_tokens(_METRICS) == 855836
    assert 33960 * 16 == 543360  # what the wrong answer would have been


def test_parse_kv_cache_tokens_falls_back_to_block_product():
    """Builds that do not emit the token count still get an approximation."""
    text = 'vllm:cache_config_info{block_size="16",num_gpu_blocks="1000"} 1.0'
    assert parse_kv_cache_tokens(text) == 16000

    text = 'vllm:cache_config_info{block_size="16",num_gpu_blocks="1000",kv_cache_size_tokens="None"} 1.0'
    assert parse_kv_cache_tokens(text) == 16000


def test_parse_kv_cache_tokens_absent():
    """vLLM's metric names are not a stable API.

    A missing or renamed metric degrades the display rather than breaking the
    probe loop, so this returns None instead of raising.
    """
    assert parse_kv_cache_tokens("vllm:num_requests_running{} 0.0\n") is None
    assert parse_kv_cache_tokens("") is None


def test_parse_kv_cache_tokens_malformed():
    for text in (
        'vllm:cache_config_info{block_size="16"} 1.0',          # no block count
        'vllm:cache_config_info{block_size="x",num_gpu_blocks="1"} 1.0',
        'vllm:cache_config_info block_size=16 1.0',              # no braces
        'vllm:cache_config_info{block_size="0",num_gpu_blocks="0"} 1.0',
    ):
        assert parse_kv_cache_tokens(text) is None, text


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
