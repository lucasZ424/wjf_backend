"""D44: the seven pages' common behaviour, the self-check and permissions (the communication page itself: test_web_comm.py).

Real Chrome against the running application.  Why it matters: when something is wrong the operator reads the
self-check page and the banner, so they must say what really happened - what the server granted, what the archive
holds, what was lost - and the permissions must be the ones that are enforced.
"""

import json
import time
from types import SimpleNamespace

import psycopg
import pytest
from browser_support import sign_in
from fastapi.testclient import TestClient
from live_support import BRIDGES
from owner_support import small_config
from web_rig import WebRig, nominal

from htsg.auth import Accounts
from htsg.config import Settings
from htsg.owner import Owner
from htsg.web import create_web_app

NAMES = ["系统总览", "水冷壁应变场", "热应力分析", "历史查询", "系统自检", "通信与日志", "系统管理与权限"]


def sign_in_with(page, rig, credential):
    page.goto(rig.url)
    user, secret = credential.split(":", 1)
    page.locator("#login-user").fill(user)
    page.locator("#login-secret").fill(secret)
    page.locator("#login-form button").click()
    page.locator("#workspace").wait_for(state="visible")


def api(page, rig, method, path, credential, **kw):
    return page.request.fetch(rig.url + path, method=method, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"}, **kw)


def test_the_seven_pages_keep_receiving_groups_when_switched(page, archive_rig):
    sign_in(page, archive_rig)
    shown = [t.replace(" ", "").replace("\n", "") for t in page.locator("#navigation button").all_inner_texts()]
    assert shown == NAMES
    point = next(d.inputs[0] for d in archive_rig.config.derived if d.operation == "difference")
    try:
        for index, name in enumerate(NAMES):
            page.get_by_role("button", name=name).click()
            assert page.locator(".page-heading h2").inner_text() == name, "a page is known by its title; no page number is shown"
            archive_rig.call(archive_rig.ua, [(point, 470.0 + index)])
            changed = time.time()  # a reading taken after this holds the change
            page.wait_for_function("t => Number(document.querySelector('#stream-time').dataset.taken) >= t", arg=changed)
            assert page.errors == [], f"a live update after switching to {name} must not touch removed DOM"
    finally:
        archive_rig.call(archive_rig.ua, [(point, nominal()[point])])
    assert "HTSG /" not in page.locator(".page-heading").inner_text() and not any(c.isdigit() for t in shown for c in t)
    assert page.errors == []


def test_the_self_check_shows_what_the_server_granted_the_coverage_limit_and_the_archive(page, archive_rig):
    archive_rig.diagnostics = {"ua_field": {
        "requested": {"publish_interval": 1.0, "keepalive_count": 2, "lifetime_count": 60, "queue_size": 10, "session_timeout": 60.0},
        "publish_interval": 1.0, "keepalive_count": 3, "lifetime_count": 60, "session_timeout": 60.0,
        "queue_capability": "OVERWRITE_UNDETECTABLE", "min_queue": 1, "violations": ["保活静默加余量须小于数据有效期"],
    }}  # fmt: skip
    try:
        sign_in(page, archive_rig)
        page.get_by_role("button", name="系统自检").click()
        negotiated = page.locator("#check-negotiated")
        negotiated.get_by_text("覆盖不可检测").wait_for()
        rows = {r.locator("td").nth(0).inner_text(): r.locator("td").all_inner_texts() for r in negotiated.locator("tbody tr").all()}
        assert rows["保活计数"][1:] == ["2", "3 与请求不同"], "requested 2, granted 3: both shown, the difference marked"
        assert rows["测点队列（最小值）"][1].strip() == "10" and rows["测点队列（最小值）"][2].startswith("1")
        assert "不满足约束：保活静默加余量须小于数据有效期" in negotiated.inner_text()
        assert "发布序号连续不能证明监控项无丢值" in negotiated.inner_text()
        archive = page.locator("#check-archive")
        archive.get_by_text("正常库").wait_for()
        assert "写入队列" in archive.inner_text() and "已确认" in archive.inner_text()
        assert any("正常" in r for r in archive.locator("tbody tr").all_inner_texts()), "per-source archive state"
        assert "5 秒" in page.locator("#check-timing").inner_text() and "1000 条" in page.locator("#check-timing").inner_text()
        archive_rig.diagnostics = {}
        page.get_by_role("button", name="系统总览").click()
        page.get_by_role("button", name="系统自检").click()
        negotiated.get_by_text("尚未取得协商结果").wait_for()
        assert page.errors == []
    finally:
        archive_rig.diagnostics = {}


@pytest.fixture(scope="module")
def tight_rig(tmp_path_factory):
    """The same system with room for ONE unwritten fault record, and nothing that ever writes them."""
    from live_support import FULL

    payload = FULL.model_dump(mode="json")
    payload["timing"]["fault_buffer_limit"] = 1
    with WebRig(tmp_path_factory.mktemp("web"), config=Settings.model_validate(payload), archive=True, feed_archive=False, steady=False) as running:
        yield running


def test_a_full_fault_buffer_is_announced_on_every_page_with_the_counts(page, tight_rig):
    sign_in(page, tight_rig)
    assert page.locator("#archive-banner").is_hidden()
    values = {b: 0.6 for b in BRIDGES}
    values[BRIDGES[0]] = values[BRIDGES[1]] = 4.5  # two strains beyond the limit at once: room for one record only
    tight_rig.call(tight_rig.round, values)
    deadline = time.monotonic() + 20
    page.locator("#archive-banner").wait_for(state="visible", timeout=20000)
    assert time.monotonic() < deadline
    text = page.locator("#archive-banner").inner_text()
    assert "故障归档不完整" in text and "1/1" in text and "已拒绝 1 条" in text and "mb_field" in text
    assert "实时的“已确认”状态仍然正确" in text
    for name in NAMES:
        page.get_by_role("button", name=name).click()
        assert page.locator("#archive-banner").is_visible(), f"on the current page: {name}"
    page.get_by_role("button", name="系统自检").click()
    faults = page.locator("#check-faults")
    faults.get_by_text("故障归档不完整").wait_for()
    assert "1/1" in faults.inner_text() and "累计拒绝 1 条" in faults.inner_text()
    assert "mb_field" in faults.locator("tbody").inner_text(), "the loss summary names the source"
    assert "只在内存中，进程退出会丢失" in faults.inner_text() and "IN_MEMORY" not in faults.inner_text()
    tight_rig.call(tight_rig.round)  # everything normal again: the counts are cumulative, they do not vanish with the state
    page.wait_for_timeout(1500)
    assert page.locator("#archive-banner").is_visible()
    assert page.errors == []


def test_roles_are_enforced_and_the_development_login_still_works(page, archive_rig):
    sign_in(page, archive_rig, "viewer")
    page.get_by_role("button", name="系统管理与权限").click()
    assert "仅管理员可管理账户" in page.locator("#account-area").inner_text()
    assert "仅管理员可查看管理操作记录" in page.locator("#audit-log").inner_text()
    matrix = page.locator("#workspace table").first.inner_text()
    assert "在线增加、删除或修改通道、阈值与配置" in matrix and matrix.count("不允许") >= 3, "nobody changes channels online"
    viewer = archive_rig.credentials["viewer"]
    assert api(page, archive_rig, "GET", "/api/admin/accounts", viewer).status == 403
    assert api(page, archive_rig, "POST", "/api/admin/accounts", viewer, data=json.dumps({"username": "x", "role": "admin"})).status == 403
    assert api(page, archive_rig, "GET", "/api/admin/audit", viewer).status == 403
    assert api(page, archive_rig, "GET", "/api/me", "no:such").status == 401
    page.locator("#logout").click()
    sign_in_with(page, archive_rig, "admin:admin")  # the simplified development login
    assert "管理员" in page.locator("#identity").inner_text()
    assert page.errors == []


def test_the_login_page_asks_for_the_user_name_and_the_password_apart(page, rig):
    """修复轮07 (user ruling 2026-10-28): one field for "user:token" meant copying one long string out of a one-line JSON
    file; the page asks for the two apart and joins them itself."""
    user, secret = rig.credentials["viewer"].split(":", 1)
    page.goto(rig.url)
    form = page.locator("#login-form")
    assert form.locator("input").count() == 2 and form.locator("#login-secret").get_attribute("type") == "password"
    form.locator("#login-user").fill(user)
    form.locator("#login-secret").fill(secret)
    form.locator("button").click()
    page.locator("#workspace").wait_for(state="visible")
    assert "只读用户" in page.locator("#identity").inner_text()
    assert page.locator("#login-secret").input_value() == "", "the password does not stay in the form"
    assert page.errors == []


def test_an_administrator_creates_and_stops_accounts_and_every_step_is_on_record(page, archive_rig):
    sign_in_with(page, archive_rig, "admin:admin")
    page.get_by_role("button", name="系统管理与权限").click()
    page.locator("#account-form").wait_for()
    page.locator('#account-form [name="username"]').fill("operator1")
    page.locator("#account-form button").click()
    try:
        page.locator("#new-credential").wait_for(timeout=8000)
    except Exception as error:  # say what the page and the server looked like, not just that time ran out
        raise AssertionError(f"no credential shown; page: {page.locator('#account-area').inner_text()!r}; users: {sorted(archive_rig.accounts.users)}") from error
    user, secret = page.locator("#new-user").inner_text(), page.locator("#new-secret").inner_text()
    assert user == "operator1", "the user name and the password are shown apart, as the login page asks for them"
    credential = f"{user}:{secret}"
    assert api(page, archive_rig, "GET", "/api/me", credential).json() == {"username": "operator1", "role": "viewer"}
    row = page.locator("#account-area tbody tr", has_text="operator1")
    assert "有效" in row.inner_text()
    page.get_by_role("button", name="历史查询").click()  # leaving the page: the credential is not shown a second time
    page.get_by_role("button", name="系统管理与权限").click()
    page.locator("#account-form").wait_for()
    assert page.locator("#new-credential").count() == 0
    row = page.locator("#account-area tbody tr", has_text="operator1")
    row.get_by_role("button", name="停用").click()
    row.get_by_role("button", name="确认停用 operator1").click()  # two clicks: no pop-up dialog
    page.locator("#account-area tbody tr", has_text="operator1").get_by_text("已停用").wait_for()
    assert api(page, archive_rig, "GET", "/api/me", credential).status == 401, "the stopped credential no longer works"
    # 修复轮12: the one administrator has no stop button - it could only fail; the server still refuses, in Chinese
    admin_row = page.locator("#account-area tbody tr", has_text="admin").first
    assert admin_row.get_by_role("button").count() == 0 and "唯一管理员，不能停用" in admin_row.inner_text()
    refused = api(page, archive_rig, "DELETE", "/api/admin/accounts/admin", "admin:admin")
    assert refused.status == 409 and refused.json()["detail"] == "不能停用唯一的管理员"
    page.locator("#audit-log tbody tr", has_text="停用账户").first.wait_for()  # 终版 §17.5: in words
    log = page.locator("#audit-log").inner_text()
    assert "新建只读账户" in log and "停用账户" in log and "operator1" in log and "admin" in log
    assert [e for e in page.errors if "409" not in e] == [], "only the refusal this test provoked (409) shows in the console"


def test_removing_the_model_page_and_its_config_payload_keeps_live_thermal_results(page, rig):
    sign_in(page, rig)
    assert page.locator("#navigation button").all_inner_texts() == NAMES, "the seven pages, the communication log between self-check and permissions"
    config = api(page, rig, "GET", "/api/ui-config", rig.credentials["viewer"]).json()
    assert "models" not in config, "the removed page's parameter payload is no longer exposed"
    chosen = next(d for d in rig.config.derived if d.operation == "difference")
    page.get_by_role("button", name="热应力分析").click()
    page.locator("#pair-select").select_option("0")
    try:
        for inner, delta, stress in ((500.0, 50.0, 57.375), (430.0, -20.0, -22.95)):
            rig.call(rig.ua, [(chosen.inputs[0], inner), (chosen.inputs[1], 450.0)])
            page.wait_for_function("delta => document.querySelector('#trend-canvas').plot?.panels[1].points.at(-1)?.v === delta", arg=delta)
            got = page.evaluate("document.querySelector('#trend-canvas').plot.panels.map(p => p.points.at(-1))")
            assert got[1]["valid"] and got[2]["valid"], "shared thermal calculation remains valid after removing the page"
            assert got[2]["v"] == pytest.approx(stress), "E 170 GPa x alpha 1.35e-5/K x restraint 0.5, still pushed live"
            assert page.locator("#model-cards").count() == 0
        assert "估算值" in page.locator("#trend-latest").inner_text()
    finally:
        rig.call(rig.ua, [(pid, nominal()[pid]) for pid in chosen.inputs])
    assert page.errors == []


def test_the_administrator_changes_their_own_password_on_the_page_and_only_read_only_users_are_added(page, archive_rig):
    """修复轮02 (user ruling 2026-10-26): one administrator, made by the deployment; on the page only read-only users are
    added, and the administrator sets a password of their own whenever they want - the page carries on with it."""
    old, new = archive_rig.credentials["admin"], "a-password-of-my-own"
    sign_in(page, archive_rig, "admin")
    page.get_by_role("button", name="系统管理与权限").click()
    form = page.locator("#account-form")
    form.wait_for()
    assert form.locator('[name="role"]').get_attribute("value") == "viewer" and "管理员" not in form.inner_text(), "no second administrator"
    password = page.locator("#password-form")
    password.locator('[name="secret"]').fill(new)
    password.locator('[name="confirm"]').fill(new + "x")
    password.locator("button").click()
    page.locator("#account-area").get_by_text("两次输入的口令不一致").wait_for()
    assert api(page, archive_rig, "GET", "/api/me", old).status == 200, "a mistyped confirmation changes nothing"
    try:
        password.locator('[name="secret"]').fill(new)
        password.locator('[name="confirm"]').fill(new)
        password.locator("button").click()
        page.locator("#account-area").get_by_text("口令已修改").wait_for()
        assert api(page, archive_rig, "GET", "/api/me", old).status == 401, "the old credential stops working"
        assert api(page, archive_rig, "GET", "/api/me", "admin:" + new).status == 200
        page.get_by_role("button", name="历史查询").click()
        page.get_by_role("button", name="系统管理与权限").click()  # the page goes on with the new credential: no new sign-in
        page.locator("#audit-log tbody tr", has_text="修改口令").first.wait_for()
        assert page.locator("#workspace").is_visible()
    finally:  # the rig is shared by the module: what the other tests sign in with works again
        api(page, archive_rig, "POST", "/api/admin/password", "admin:" + new, data=json.dumps({"secret": old.split(":", 1)[1]}))
    assert page.errors == []


def test_a_database_failure_while_changing_an_account_is_reported_as_unconfirmed_not_as_a_crash(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    admin = accounts.create("admin", "admin")
    owner = Owner(small_config(), 1000.0, 100.0, run_id="t")
    app = create_web_app(config=owner.config, accounts=accounts, owner=owner)

    def down(*args):
        raise psycopg.OperationalError("database down")

    accounts.record = down
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post("/api/admin/accounts", json={"username": "operator", "role": "viewer"}, headers={"Authorization": "Bearer " + admin})
    assert answer.status_code == 503 and "变更未确认" in answer.json()["detail"]
    assert "operator" not in accounts.users, "no record, no change (2026-10-30)"
    with TestClient(app) as client:
        assert client.get("/api/me", headers={"Authorization": "Bearer " + admin}).status_code == 200, "nobody is locked out"


def test_the_self_check_names_a_missing_bucket_and_why_a_bucket_could_not_be_prepared(page, archive_rig):
    """A group waiting for its bucket holds back every group behind it (an accepted limit, 修复轮10): the page must say so,
    with since when, and why the maintenance thread could not prepare a bucket - and stop saying it once it is over."""
    retention = {"databases": {"primary": {"readable": True, "kept_buckets": 3, "kept_from": None, "boundary": None}},
                 "last_deletion": None, "deletion_period_s": None, "forecast": None, "alarm": {"active": False}}  # fmt: skip
    archive_rig.acquisition.maintenance = SimpleNamespace(snapshot=SimpleNamespace(health={
        "retention": retention, "primary": "PREPARE_FAILED:InsufficientPrivilege", "emergency": "OK", "numbering": "PAUSED:FILE_BEHIND",
    }))  # fmt: skip
    archive_rig.router.health["waiting_for_bucket"] = {"bucket": 42, "group": "g", "since": time.time() - 60}
    try:
        sign_in(page, archive_rig)
        page.get_by_role("button", name="系统自检").click()
        card = page.locator("#check-retention")
        card.get_by_text("桶准备：").wait_for()
        text = card.inner_text()
        assert "正常库 准备失败（InsufficientPrivilege）" in text and "应急库 正常" in text and "编号暂停（FILE_BEHIND）" in text, text
        assert "正常库缺第 42 号桶" in text and "在等待写入" in text, text
        archive_rig.router.health.pop("waiting_for_bucket")
        page.get_by_role("button", name="系统总览").click()
        page.get_by_role("button", name="系统自检").click()
        card.get_by_text("桶准备：").wait_for()
        page.wait_for_timeout(1500)
        assert "缺第 42 号桶" not in card.inner_text(), "written: no longer said to wait"
        assert page.errors == []
    finally:
        archive_rig.router.health.pop("waiting_for_bucket", None)
        del archive_rig.acquisition.maintenance
