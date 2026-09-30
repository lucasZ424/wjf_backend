import json
from pathlib import Path

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.config import load_settings
from htsg.runtime import Runtime
from htsg.storage import Router, Store

config = load_settings("config/development.json")
runtime = Runtime(config, Router(config, Store(""), Store("")))
app = create_app(runtime, Accounts(Path(".local/accounts.json")))
Path("docs").mkdir(exist_ok=True)
Path("docs/openapi.json").write_text(
    json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8"
)
runtime.io.shutdown()
runtime.read_io.shutdown()
runtime.calc_io.shutdown()
print("Exported docs/openapi.json without opening a database connection.")
