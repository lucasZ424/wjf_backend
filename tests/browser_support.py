"""Helpers for tests that drive a real browser; the fixtures (`browser`, `rig`, `page`) are in conftest.py."""

import json
import os
import re
from pathlib import Path

# Set to a folder for the one run whose screenshots and browser logs are kept as engineering evidence (方案第 11 节);
# without it `keep` does nothing, so ordinary runs leave no files behind.
EVIDENCE = os.environ.get("HTSG_EVIDENCE_DIR")


def sign_in(page, rig, who="viewer"):
    page.goto(rig.url)
    page.locator("#credential").fill(rig.credentials[who])
    page.locator("#login-form button").click()
    page.locator("#workspace").wait_for(state="visible")


def watch_socket(page):
    """Every message the page receives on its realtime sockets, as (socket number, message); call before `goto`."""
    log, sockets = [], []

    def opened(ws):
        number = len(sockets)
        sockets.append(ws)
        ws.on(
            "framereceived",
            lambda payload: log.append((number, json.loads(payload if isinstance(payload, str) else payload.decode()))),
        )

    page.on("websocket", opened)
    return log


def number(text):
    """The number in a cell such as '+45.9 MPa' or '0.9 mV/V'; None for the dash that stands for 'no value'."""
    text = text.split("：")[-1]
    found = re.search(r"([+-]?)\s*(\d[\d,]*(?:\.\d+)?|\.\d+)", text)
    if found is None:
        return None
    value = float(found.group(2).replace(",", ""))
    return -value if found.group(1) == "-" else value


def keep(page, name):
    """Save a full-page screenshot and everything the browser logged so far (only when HTSG_EVIDENCE_DIR is set)."""
    if not EVIDENCE:
        return
    folder = Path(EVIDENCE)
    folder.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(folder / f"{name}.png"), full_page=True)
    (folder / f"{name}.log").write_text("\n".join(page.console_log) + "\n", encoding="utf-8")
