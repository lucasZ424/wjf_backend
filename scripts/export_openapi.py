import json
import time
from pathlib import Path
from types import SimpleNamespace

from htsg.auth import Accounts
from htsg.config import load_settings
from htsg.owner import Owner
from htsg.web import create_web_app

config = load_settings("config/development.json")
owner = Owner(config, time.time(), time.monotonic(), run_id="openapi")
# a placeholder archive router only makes the history routes part of the description; nothing is read from it
app = create_web_app(
    config=config, accounts=Accounts(Path(".local/accounts.json")), owner=owner,
    archive_router=SimpleNamespace(),
)  # fmt: skip
Path("docs").mkdir(exist_ok=True)
Path("docs/openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8")
print("Exported docs/openapi.json without opening a database connection.")
