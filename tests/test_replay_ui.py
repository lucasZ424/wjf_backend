"""The browser must keep the reference window while freely changing channels."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

from htsg.config import load_settings


def test_fault_reference_allows_other_channels_and_custom_window():
    config = load_settings("config/research-sim.json").model_dump()
    config["simulation"]["enabled"] = False
    replay_requests, errors = [], []
    fault = {
        "id": "f1",
        "point_id": "bridge_mvv_01",
        "time": 1700000000,
        "value": 3,
        "unit": "mV/V",
        "state": "HIGH",
    }

    def respond(route):
        url = urlparse(route.request.url)
        if url.path == "/" or url.path.startswith("/static/"):
            name = "index.html" if url.path == "/" else url.path.rsplit("/", 1)[-1]
            mime = {"html": "text/html", "js": "application/javascript", "css": "text/css"}
            route.fulfill(
                body=(Path("htsg/static") / name).read_bytes(), content_type=mime[name.rsplit(".", 1)[-1]]
            )
            return
        result = {}
        if url.path == "/api/me":
            result = {"username": "reader", "role": "viewer"}
        elif url.path == "/api/ui-config":
            result = config
        elif url.path == "/api/faults":
            result = {"items": [{"payload": fault}], "next_cursor": None}
        elif url.path == "/api/replay":
            replay_requests.append(parse_qs(url.query))
            result = {"items": [], "next_cursor": None, "has_more": False, "partial": False}
        route.fulfill(body=json.dumps(result), content_type="application/json")

    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("http://htsg.test/**", respond)
        try:
            page.goto("http://htsg.test/")
            page.locator("#credential").fill("reader:test")
            page.locator("#login-form button").click()
            page.locator(".page-heading h2").wait_for()
            assert page.locator("#navigation button").evaluate_all(
                "buttons => buttons.map(b => b.dataset.page)"
            ) == ["1", "2", "4", "5", "12", "14", "15"]
            assert page.locator("#point-select").count() == 0
            assert page.locator("#scenario-controls").count() == 1
            for number in (2, 4):
                page.locator(f'[data-page="{number}"]').click()
                assert page.locator("#point-select").count() == 0
            page.locator('[data-page="5"]').click()
            assert page.locator(".page-heading h2").inner_text() == "历史查询"
            page.locator('[data-event="f1"]').click()
            page.wait_for_function("document.getElementById('history-status').textContent.includes('0 条')")
            assert replay_requests[-1]["center"] == ["1700000000"]
            with page.expect_response("**/api/replay?*"):
                page.locator("#point-select").select_option("strain_08")
            assert replay_requests[-1]["point"] == ["strain_08"]
            assert replay_requests[-1]["center"] == ["1700000000"]
            with page.expect_response("**/api/replay?*"):
                page.locator("#window-select").fill("1800")
                page.locator("#window-select").press("Tab")
            assert replay_requests[-1]["seconds"] == ["1800"]
            assert replay_requests[-1]["center"] == ["1700000000"]
            assert all("event_id" not in query for query in replay_requests)
            page.locator('[data-page="12"]').click()
            page.locator('[data-page="5"]').click()
            assert page.locator("#point-select").input_value() == "strain_08"
            assert page.locator("#window-select").input_value() == "1800"
            assert page.locator("#event-detail").is_visible()
            assert not errors
        finally:
            browser.close()
