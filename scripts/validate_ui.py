"""Exercise the live local research HMI without mocking business data or exposing tokens."""

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--scenario-controls", action="store_true")
    parser.add_argument(
        "--event-history",
        action="store_true",
        help="Require a real closed event and verify its complete plotted history",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "docs/screenshots")
    args = parser.parse_args()
    credentials = json.loads((ROOT / ".local/research/initial-credentials.json").read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    failures, checks = [], []

    def passed(name):
        checks.append(name)

    def login(page, role):
        page.goto(args.base_url)
        page.locator("#credential").fill(credentials[role])
        page.locator("#login-form button").click()
        expect(page.locator("#workspace")).to_be_visible()
        page.wait_for_function(
            "state.frame && Object.values(state.frame.values).some(v => typeof v.value === 'number')"
        )
        expect(page.locator("#connection")).to_have_text("实时连接")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1080})
        page = context.new_page()
        page.on("pageerror", lambda error: failures.append(str(error)))
        login(page, "admin")
        passed("admin login and real protocol-fed numeric frame")

        for number in (1, 2, 4, 12, 14, 15):
            page.locator(f'[data-page="{number}"]').click()
            if number in (1, 2, 4):
                page.wait_for_function(
                    "state.history.length > 0 && document.querySelector('#history-status').textContent.includes('条')"
                )
            if number == 2:
                assert page.locator(".station").count() == page.evaluate("state.config.ui.stations.length")
                page.locator("#field-select").select_option("temperature")
                page.locator(".station").last.click()
                assert page.locator("#point-select").input_value().startswith("temp_")
                passed("configured station selection changes requested history point")
            if number == 4:
                page.wait_for_function(
                    "document.querySelector('#model-info').textContent.includes('参数敏感性范围')"
                )
                page.wait_for_function(
                    "document.querySelector('#thermal-status-0').textContent.includes('条')"
                )
                expect(page.locator("#model-info table")).to_contain_text("弹性模量 E")
                expect(page.locator("#model-info table")).to_contain_text("1/K")
                assert not page.locator("#model-info details").evaluate("element => element.open")
                passed("thermal estimate metadata and temperature/load archived trends")
            if number == 14:
                page.wait_for_function("document.querySelectorAll('#source-cards .card').length === 2")
                assert "CONNECTED" in page.locator("#source-cards").inner_text()
                passed("two real active-reading protocol source panels")
            if number == 15:
                expect(page.locator("#account-form")).to_be_visible()
            if number in (1, 2, 4):
                page.wait_for_timeout(250)
                page.wait_for_function(
                    "document.querySelector('#history-status').textContent.includes('条') && document.querySelector('#trend-title').textContent.includes(label(state.point))"
                )
            page.screenshot(path=str(args.output / f"page-{number:02}.png"), full_page=True)
        passed("six desktop pages rendered and captured")

        name = f"ui_test_{int(time.time())}"
        page.locator('[name="username"]').fill(name)
        page.locator("#account-form button").click()
        expect(page.locator("#new-credential")).to_be_visible()
        created = page.locator("#new-credential").inner_text()
        assert name in created
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator(f'[data-revoke="{name}"]').click()
        expect(page.locator("#audit-log")).to_contain_text(name)
        expect(page.locator("#account-area tr").filter(has_text=name)).to_contain_text("已停用")
        passed("admin creates and revokes actual test account with audit entry")

        page.locator('[data-page="1"]').click()
        page.wait_for_function("state.history.length > 0")
        history_requests = []
        page.on(
            "request",
            lambda request: history_requests.append(request.url) if "/api/history?" in request.url else None,
        )
        page.evaluate("state.ws.close()")
        expect(page.locator("#connection")).to_contain_text("重连中")
        expect(page.locator("#connection")).to_have_text("实时连接", timeout=15000)
        page.wait_for_function("document.querySelector('#history-status').textContent.includes('条')")
        assert history_requests, "Reconnect must reread persisted history"
        passed("WebSocket reconnect refreshes persisted history")

        # Suppress only the next WS reconnect; HTTP fallback still reads the actual server.
        page.evaluate("state.ws.close(); clearTimeout(state.reconnect)")
        page.wait_for_timeout(100)
        page.evaluate("clearTimeout(state.reconnect)")
        first = page.evaluate("state.frame.window_start")
        page.wait_for_function("old => state.frame.window_start > old", arg=first, timeout=12000)
        page.evaluate("connectStream()")
        passed("HTTP fallback advances real frames while WS is disconnected")

        if args.scenario_controls:
            page.locator("#scenario-controls button").first.click()
            page.wait_for_function(
                "document.querySelector('#simulation-status').textContent.includes('当前场景')"
            )
            passed("admin scenario button reaches real simulator control")

        if args.event_history:
            page.locator('[data-page="1"]').click()
            page.wait_for_function("state.events.some(row => (row.payload || row).phase === 'CLOSED')")
            page.locator("#point-select").select_option("strain_08")
            event_id = page.evaluate(
                "state.events.map(row => row.payload || row).find(e => e.phase === 'CLOSED').id"
            )
            page.locator(f'[data-event="{event_id}"]').click()
            page.wait_for_function(
                "state.event && document.querySelector('#history-status').textContent.includes('条')"
            )
            assert page.locator("#point-select").input_value() != "strain_08"
            assert page.evaluate(
                "[...document.querySelector('#point-select').options].every(option => historyDefinitions().some(p => p.id === option.value))"
            )
            page.screenshot(path=str(args.output / "page-01-event.png"), full_page=True)
            page.locator('[data-page="4"]').click()
            page.wait_for_function(
                "document.querySelector('#thermal-status-2')?.textContent.includes('条') && document.querySelector('#thermal-status-0')?.textContent.includes('条')"
            )
            plotted = page.evaluate(
                "[...document.querySelectorAll('#thermal-trends canvas')].map(canvas => ({start:canvas.plotData.start,end:canvas.plotData.end,count:canvas.plotData.rows.length,sorted:canvas.plotData.rows.every((row,i,rows) => i===0 || row.t>=rows[i-1].t)}))"
            )
            event = page.evaluate("({start:state.event.start,end:state.event.end})")
            assert len(plotted) == 3
            assert all(
                series["start"] == event["start"] and series["end"] == event["end"] for series in plotted
            )
            assert all(series["sorted"] and series["count"] > 1000 for series in plotted)
            page.screenshot(path=str(args.output / "page-04-event.png"), full_page=True)
            passed(
                "event selection restricts points and three thermal plots use full cursor-paged event interval"
            )
            page.locator("#live-history").click()

        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-page="2"]').click()
        page.wait_for_function("document.querySelector('#history-status').textContent.includes('条')")
        page.screenshot(path=str(args.output / "page-02-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        passed("mobile layout remains within viewport")

        viewer = context.new_page()
        viewer.on("pageerror", lambda error: failures.append(str(error)))
        login(viewer, "viewer")
        expect(viewer.locator("#scenario-controls button").first).to_be_disabled()
        forbidden = viewer.evaluate("""async () => {
          const response = await fetch('/api/simulation', {method:'POST', headers:{
            Authorization:'Bearer '+sessionStorage.getItem('htsg.credential'),
            'Content-Type':'application/json'}, body:JSON.stringify({scenario:'baseline'})});
          return response.status;
        }""")
        assert forbidden == 403
        viewer.locator('[data-page="15"]').click()
        expect(viewer.locator("#account-area")).to_contain_text("仅管理员")
        assert viewer.locator("#account-form").count() == 0
        passed("viewer controls hidden or disabled and backend mutation returns 403")
        assert not failures, failures
        passed("no browser JavaScript exceptions")
        browser.close()

    report = {"checks": checks, "count": len(checks), "failures": failures, "time": time.time()}
    (args.output / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"checks_passed": len(checks), "failures": len(failures)}))


if __name__ == "__main__":
    main()
