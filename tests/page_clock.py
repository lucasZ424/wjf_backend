"""Readings at the times a test chooses, and a history answer the test writes (修复轮19).

The 5-minute strain change and the 30-minute / 1-hour trend windows are about what the page saw minutes ago; a test
cannot wait an hour.  The page draws and caches by the time a reading was taken (终版 §13.3), so the readings are the
real ones from the running application with `taken_time` - and the channels a test names - replaced on the way to the
browser.  Two readings minutes apart would be one interruption (`ui_stall_notice`), so the notice is raised for the
page under test: an interruption is then made on purpose (an invalid channel, a new run), never by the clock jumping.

The history answer for the back-fill (终版 §13.3) is built in the form of `history.query` and `/api/replay`, so the page
reads what the backend would send.  Nothing here holds an expected value: the tests take theirs from the 验收表.
"""

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse

READING, CONFIG, REPLAY = "**/api/snapshot", "**/api/ui-config", "**/api/replay?*"
FAR = 10**6  # seconds: no two readings of a test are this far apart

# In-flight counts seen from inside the page: every fetch of a reading or of the history, from its call until it is
# answered or given up (an abort rejects at once).  Installed before the application loads.
FLIGHT = """
(() => {
  const f = window.__flight = {replay: 0, replayMost: 0, replayCalls: 0, snapshot: 0, snapshotMost: 0, snapshotCalls: 0};
  const real = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const url = String(input && input.url ? input.url : input);
    const kind = url.includes('/api/replay') ? 'replay' : url.includes('/api/snapshot') ? 'snapshot' : null;
    const p = real(input, init);
    if (!kind) return p;
    f[kind]++; f[kind + 'Calls']++; f[kind + 'Most'] = Math.max(f[kind + 'Most'], f[kind]);
    let out = true;
    const done = () => { if (out) { out = false; f[kind]--; } };
    p.then(done, done);
    init?.signal?.addEventListener('abort', done, {once: true});  // given up: at once, not when the rejection is handled
    return p;
  };
})();
"""


class Clock:
    """Every reading the page gets is the real one taken at `self.t`, with the channels set by `at` replaced."""

    def __init__(self, page, t):
        self.page, self.t, self.views, self.run = page, float(t), {}, None
        page.add_init_script(FLIGHT)
        page.route(CONFIG, self._config)
        page.route(READING, self._reading)

    def _config(self, route):
        response = route.fetch()
        body = response.json()
        body["timing"]["ui_stall_notice"] = FAR
        route.fulfill(response=response, body=json.dumps(body))

    def _reading(self, route):
        response = route.fetch()
        body = response.json()
        body["taken_time"] = self.t
        if self.run:
            body["run_id"] = self.run
        for cid, view in self.views.items():
            body["channels"][cid] = {**body["channels"][cid], **view}
        route.fulfill(response=response, body=json.dumps(body))

    def set(self, t=None, **channels):
        """`channel=value` (valid), `channel=None` (invalid), or `channel={...}` (fields of the view, e.g. a fault)."""
        if t is not None:
            self.t = float(t)
        for cid, value in channels.items():
            if value is None:
                self.views[cid] = {"valid": False, "value": None, "reason": "SOURCE_INVALID", "fault": None}
            elif isinstance(value, dict):
                self.views[cid] = {"valid": True, "fault": None, **value}
            else:
                self.views[cid] = {"valid": True, "value": float(value), "fault": None}

    def at(self, t, **channels):
        """Set, then wait until the page has taken a reading at `t` and drawn it."""
        self.set(t, **channels)
        self.page.wait_for_function(
            "t => Number(document.querySelector('#stream-time')?.dataset.taken) === t", arg=self.t, timeout=10000
        )
        self.page.wait_for_timeout(50)  # the frame that draws it

    def flight(self):
        return self.page.evaluate("({...window.__flight})")


def instant(text):
    try:
        return float(text)
    except ValueError:
        return datetime.fromisoformat(text).timestamp()


def asked(request_url):
    """(points, start, end) of a /api/replay request: the window is `seconds` around `center` (history_api)."""
    q = parse_qs(urlparse(request_url).query)
    center, seconds = instant(q["center"][0]), float(q["seconds"][0])
    points = [p for p in q["points"][0].split(",") if p]
    return points, center - seconds / 2, center + seconds / 2


def history(start, end, channels, units=None):
    """A /api/replay answer.  `channels` maps an id to rows (time, value, until, ended_by[, valid]); a row whose
    `ended_by` is "GAP" ends at an archive gap, which is listed, and the next row opens a new segment."""
    units = units or {}
    out, gaps = {}, []
    for cid, rows in channels.items():
        segments, seg = [], {"run_id": "web", "source_id": "ua_field", "seg": 1, "start_wall": None, "end_wall": None,
                              "end_reason": None, "covered_until": end, "rows": []}  # fmt: skip
        for n, (t, value, until, ended_by, *valid) in enumerate(rows):
            ok = valid[0] if valid else True
            seg["rows"].append({
                "id": f"{cid}/{n}", "kind": "raw", "time": t, "value": value if ok else None, "valid": ok,
                "quality": "GOOD" if ok else "BAD", "status_code": 0, "status": None, "reason": None, "extreme": False,
                "held": t < start, "until": until, "ended_by": ended_by,
            })  # fmt: skip
            if seg["start_wall"] is None:
                seg["start_wall"] = t
            if ended_by == "GAP":
                following = rows[n + 1][0] if n + 1 < len(rows) else None
                gaps.append({"run_id": "web", "source_id": "ua_field", "start_wall": until, "end_wall": following, "reason": "QUEUE_FULL"})
                seg.update(end_wall=until, end_reason="GAP")
                segments.append(seg)
                seg = {**seg, "seg": seg["seg"] + 1, "start_wall": None, "end_wall": None, "end_reason": None, "rows": []}
        if seg["rows"]:
            segments.append(seg)
        out[cid] = {"source_id": "ua_field", "segments": segments, "truncated": False, "unit": units.get(cid)}
    return {
        "start": start, "end": end, "query_time": end, "channels": out, "gaps": gaps, "partial": False, "unavailable": [],
        "limit": 10000, "truncated": False, "sources": ["ua_field"], "retention": {}, "coverage": "UP_TO_COVERAGE_END",
        "coverage_end": {"ua_field": end}, "coverage_complete_until": end, "center": (start + end) / 2, "seconds": end - start,
    }  # fmt: skip


def flat(start, end, value):
    """One value held over the whole window, as a channel that did not change would come back."""
    return [(start, value, end, "WATERMARK")]


class Backfill:
    """Answers every /api/replay request of the page.  `answer(points, start, end)` gives the rows by channel;
    `hold=True` keeps the requests unanswered until `release`; `fail` answers 503 (or aborts, `fail="abort"`)."""

    def __init__(self, page, answer=None):
        self.page, self.answer, self.hold, self.fail = page, answer, False, None
        self.requests, self.held = [], []
        page.route(REPLAY, self._replay)

    def _replay(self, route):
        points, start, end = asked(route.request.url)
        self.requests.append((points, start, end))
        if self.hold:
            self.held.append(route)
            return
        self._answer(route, points, start, end)

    def _answer(self, route, points, start, end, fail=None):
        fail = fail if fail is not None else self.fail
        if fail == "abort":
            route.abort()
        elif fail:
            route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "历史库无法读取"}))
        else:
            rows = self.answer(points, start, end) if self.answer else {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(history(start, end, rows)))

    def release(self, which, answer=None):
        """Answer a held request - the `which`-th, or the one for the channel `which` - with `answer` instead of the
        usual one if given; False if the page gave it up (or never sent it)."""
        if isinstance(which, str):
            found = [r for r in self.held if asked(r.request.url)[0] == [which]]
            if not found:
                return False
            route = found[0]
        else:
            route = self.held[which]
        points, start, end = asked(route.request.url)
        if answer is not None:
            saved, self.answer = self.answer, answer
        try:
            self._answer(route, points, start, end)
            return True
        except Exception:  # noqa: BLE001 - the page cancelled it: there is nothing to answer
            return False
        finally:
            if answer is not None:
                self.answer = saved


# ----------------------------------------------------------------------------------------- the chart, read back


def plot(page):
    return page.evaluate("() => { const p = document.querySelector('#trend-canvas').plot; return p ? JSON.parse(JSON.stringify(p)) : null; }")


def pixels(page, panel, t0, t1, color, v0=None, v1=None):
    """Pixels in `color` (#rrggbb, within 40 a channel) of panel `panel` between two instants, 3 px in from each end, and -
    with `v0`/`v1` - between two values of the panel's axis (1.5 px out from each, for the width of the line).  The
    panel's name in its top-left corner is left out (the rows of its first 130 px)."""
    return page.evaluate(
        """([panel, t0, t1, color, v0, v1]) => {
          const c = document.querySelector('#trend-canvas'), p = c.plot, g = c.getContext('2d'), r = c.width / c.clientWidth;
          const q = p.panels[panel];
          const x = t => p.left + (Math.min(Math.max(t, p.t0), p.now) - p.t0) / (p.now - p.t0) * (p.right - p.left);
          const y = v => q.bottom - (v - q.lo) / (q.hi - q.lo) * (q.bottom - q.top);
          let top = x(t0) < p.left + 130 ? q.top + 14 : q.top, bottom = q.bottom;
          if (v0 !== null) { top = Math.max(top, y(Math.max(v0, v1)) - 1.5); bottom = Math.min(bottom, y(Math.min(v0, v1)) + 1.5); }
          const a = Math.round((x(t0) + 3) * r), b = Math.round((x(t1) - 3) * r);
          const h = Math.round((bottom - top) * r);
          if (b <= a || h <= 0) return 0;
          const [lr, lg, lb] = [1, 3, 5].map(k => parseInt(color.slice(k, k + 2), 16));
          const d = g.getImageData(a, Math.round(top * r), b - a, h).data;
          let n = 0;
          for (let i = 0; i < d.length; i += 4) {
            if (d[i + 3] > 200 && Math.abs(d[i] - lr) < 40 && Math.abs(d[i + 1] - lg) < 40 && Math.abs(d[i + 2] - lb) < 40) n++;
          }
          return n;
        }""",
        [panel, t0, t1, color, v0, v1],
    )
