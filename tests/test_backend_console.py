"""The web server reports into the rotated backend log, not into the console (audit 2026-10-29, no. 01-1).

Why it matters (the hard rule, a year without a crash): the supervisor sends the backend's console into one file per run
that nothing rotates.  The web server's own reports went there - an error that repeats on every request would grow that
file for as long as the run lasts, on the program's disk.  `.local/backend.log` is rotated (2 MiB x 4), so what lands
there is bounded however long the backend runs.
"""

from test_account_startup import answer, launch, measure

from htsg.auth import Accounts


async def test_the_web_servers_reports_land_in_the_rotated_log_and_the_console_stays_quiet(archive_db_factory, tmp_path):
    config = measure.make_config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    accounts = Accounts(tmp_path / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    accounts.create("admin", "admin")
    backend = launch(tmp_path, config, primary, emergency)
    try:
        assert (await answer(backend, "/api/me", viewer))["role"] == "viewer"
        assert isinstance(await backend.stop(), int), "it stops the normal way"
    finally:
        backend.ensure_gone()
    log = (tmp_path / ".local/backend.log").read_text(encoding="utf-8")
    console = "".join(p.read_text(errors="replace") for p in tmp_path.glob("backend-*.log"))
    assert "Application startup complete" in log, "the web server's report is in the rotated log"
    assert "Application startup complete" not in console and "Uvicorn running" not in console, console[-1500:]
