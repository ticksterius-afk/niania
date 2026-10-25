import json
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    cfg_path = Path(__file__).parent / "config.json"
    port = json.loads(cfg_path.read_text(encoding="utf-8")).get("port", 8080) if cfg_path.exists() else 8080
    uvicorn.run("niania.server:app", host="0.0.0.0", port=port, log_level="warning")
