"""Regress live recall refresh and separately inject a health response to check warnings."""

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/screenshots/recall-fix")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    credentials = json.loads((ROOT / ".local/research/initial-credentials.json").read_text())
    checks, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.base_url)
        page.locator("#credential").fill(credentials["admin"])
        page.locator("#login-form button").click()
        expect(page.locator("#workspace")).to_be_visible()
        page.wait_for_function("state.frame && state.health && !state.eventsLoading")
        assert not page.evaluate("archiveStopReason()"), "Live lab must accept archives"
        before = page.evaluate("state.events.map(r => (r.payload || r).id)")
        page.locator('[data-scenario="thermal_step"]').click()
        page.wait_for_function(
            "ids => state.events.some(r => !ids.includes((r.payload || r).id))", arg=before, timeout=30000
        )
        expect(page.locator("#events button").first).to_be_visible()
        checks.append("Real Modbus/OPC UA thermal trigger appears without manual event refresh")
        page.locator("#events button").first.click()
        expect(page.locator("#event-detail")).to_be_visible()
        page.wait_for_function("state.history.length > 0", timeout=15000)
        checks.append("Newly listed event opens persisted recall history")
        page.screenshot(path=str(args.output / "recall-recorded.png"), full_page=True)

        def exhausted(route):
            response = route.fetch()
            health = response.json()
            health["history_archive"].update(
                suspended=True, reason="DEVELOPMENT_WRITE_BUDGET_EXHAUSTED"
            )
            route.fulfill(response=response, json=health)

        page.route("**/api/health", exhausted)
        expect(page.locator("#archive-warning")).to_be_visible(timeout=10000)
        expect(page.locator("#archive-warning")).to_contain_text("开发库写入预算已用尽")
        page.evaluate('notice("")')
        expect(page.locator("#archive-warning")).to_be_visible()
        page.screenshot(path=str(args.output / "archive-stopped-ui-simulation.png"), full_page=True)
        page.locator('[data-page="14"]').click()
        expect(page.locator("#archive-warning")).to_be_visible()
        checks.append("UI-only injected stopped-archive warning survives cleared notice and page navigation")
        page.unroute("**/api/health", exhausted)
        expect(page.locator("#archive-warning")).to_be_hidden(timeout=10000)
        checks.append("Warning clears after real healthy archive status returns")
        browser.close()
    assert not errors, errors
    result = json.dumps({"passed": checks, "page_errors": errors}, ensure_ascii=False, indent=2)
    (args.output / "result.json").write_text(result + "\n", encoding="utf-8")
    print(result)


if __name__ == "__main__":
    main()
