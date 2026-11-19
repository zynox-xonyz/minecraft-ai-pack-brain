"""Service configuration: config.json next to the project (created on first start), env overrides PACKBRAIN_<KEY>.

The API token is generated on first start and stored in config.json. Treat config.json like a password file.
"""
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("PACKBRAIN_CONFIG", ROOT / "config.json"))

DEFAULTS = {
    "llm_base_url": "http://127.0.0.1:8080/v1",      # any OpenAI-compatible server (llama-swap, llama.cpp server, ...)
    "model": "gemma4",
    "host": "127.0.0.1",          # loopback by default; set 0.0.0.0 consciously when the Minecraft server is on another machine
    "port": 8787,
    "api_token": "",              # generated on first start
    "rate_limit_per_minute": 8,   # per player
    "rate_limit_per_hour": 80,    # per player
    "max_question_chars": 500,
    "history_turns": 6,           # remembered question/answer pairs per player
    "history_ttl_seconds": 1800,
    "max_concurrent_llm": 1,      # llama-swap runs --parallel 1
    "max_queue": 4,               # requests allowed to wait for the model; more get HTTP 503
    "max_tool_rounds": 4,
    "searxng_url": "",            # optional local SearXNG, e.g. http://127.0.0.1:8888 (needs "json" in search.formats); empty = no web search
    "web_search_enabled": True,
    "agent": "v3",                # v3 = router + code-collected evidence + one grounded answer; v2 = step-wise cited agent; v1 = free tool calling
    "web_search_results": 5,
    "log_dir": "logs",
}

_cfg = None


def get() -> dict:
    global _cfg
    if _cfg is not None:
        return _cfg
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    changed = False
    if not cfg.get("api_token"):
        cfg["api_token"] = secrets.token_urlsafe(24)
        changed = True
    if changed or not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    for k, v in DEFAULTS.items():  # env overrides, e.g. PACKBRAIN_PORT=8790
        env = os.environ.get("PACKBRAIN_" + k.upper())
        if env is not None:
            cfg[k] = type(v)(env) if not isinstance(v, str) else env
    cfg["_log_path"] = Path(cfg["log_dir"]) if Path(cfg["log_dir"]).is_absolute() else ROOT / cfg["log_dir"]
    _cfg = cfg
    return cfg
