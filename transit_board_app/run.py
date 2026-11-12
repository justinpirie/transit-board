"""
App entrypoint: read the API key from the app's options, make sure a commute
config exists, then exec uvicorn.
"""
import json
import os
import shutil
import sys

OPTIONS = "/data/options.json"                       # written by the Supervisor
CONFIG = "/homeassistant/transit_board/config.yaml"  # editable with the File editor app

opts = json.load(open(OPTIONS)) if os.path.exists(OPTIONS) else {}
key = (opts.get("api_key") or os.environ.get("API_KEY_511", "")).strip()
if not key:
    sys.exit("Set your 511 token in the app's Configuration tab "
             "(free: https://511.org/open-data/token)")
os.environ["API_KEY_511"] = key
os.environ["TRANSIT_OVERRIDES"] = json.dumps(opts.get("overrides") or [])

if not os.path.exists(CONFIG):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    shutil.copy("/app/default_config.yaml", CONFIG)
    print(f"Created {CONFIG} from defaults", flush=True)
os.environ["TRANSIT_CONFIG"] = CONFIG

# One worker on purpose: cache + rate limiter live in this process.
os.execvp("uvicorn", ["uvicorn", "transit_board.app:app",
                      "--host", "0.0.0.0", "--port", "8099", "--workers", "1"])
