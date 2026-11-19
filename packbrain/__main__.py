"""python -m packbrain  ->  start the HTTP service with host/port from config.json."""
import uvicorn

from . import config

if __name__ == "__main__":
    cfg = config.get()
    print(f"Pack-Brain on http://{cfg['host']}:{cfg['port']}  (model {cfg['model']}, LLM {cfg['llm_base_url']})")
    print(f"API token is stored in {config.CONFIG_PATH}")
    uvicorn.run("packbrain.service:app", host=cfg["host"], port=cfg["port"], log_level="info")
