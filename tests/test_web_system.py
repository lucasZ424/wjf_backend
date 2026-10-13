"""D44: the seven pages, the self-check, permissions and the model page (终版 §11, §12, §15).

Real Chrome against the running application.  Why it matters: when something is wrong the operator reads the
self-check page and the banner, so they must say what really happened - what the server granted, what the archive
holds, what was lost - and the permissions must be the ones that are enforced.
"""

import json
import time

import psycopg
import pytest
from browser_support import sign_in
from fastapi.testclient import TestClient
from live_support import BRIDGES
from owner_support import small_config
from web_rig import WebRig

from htsg.auth import Accounts
from htsg.config import Settings
from htsg.owner import Owner
from htsg.publisher import Publisher
from htsg.web import create_web_app

NAMES = ["系统总览", "水冷壁应变场", "热应力分析", "历史查询", "系统自检", "系统管理·权限", "算法引擎·数字孪生"]


def sign_in_with(page, rig, credential):
    page.goto(rig.url)
    page.locator("#credential").fill(credential)
    page.locator("#login-form button").click()
    page.locator("#workspace").wait_for(state="visible")


def api(page, rig, method, path, credential, **kw):
    return page.request.fetch(rig.url + path, method=method, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"}, **kw)


def test_the_seven_pages_come_in_the_designed_order(page, archive_rig):
    sign_in(page, archive_rig)
    shown = [t.replace(" ", "").replace("\n", "") for t in page.locator("#navigation button").all_inner_texts()]
    assert shown == NAMES
    for name in NAMES:
        page.get_by_role("button", name=name).click()
        assert page.locator(".page-heading h2").inner_text() == name, "a page is known by its title; no page number is shown"
    assert "HTSG /" not in page.locator(".page-heading").inner_text() and not any(c.isdigit() for t in shown for c in t)
    assert page.errors == []


def test_the_self_check_shows_what_the_server_granted_the_coverage_limit_and_the_archive(page, archive_rig):
    archive_rig.diagnostics = {"ua_field": {
        "requested": {"publish_interval": 1.0, "keepalive_count": 2, "lifetime_count": 60, "queue_size": 10, "session_timeout": 60.0},
        "publish_interval": 1.0, "keepalive_count": 3, "lifetime_count": 60, "session_timeout": 60.0,
        "queue_capability": "OVERWRITE_UNDETECTABLE", "min_queue": 1, "violations": ["keepalive silence plus margin must be below the data validity"],
    }}  # fmt: skip
    try:
        sign_in(page, archive_rig)
        page.get_by_role("button", name="系统自检").click()
        negotiated = page.locator("#check-negotiated")
        negotiated.get_by_text("覆盖不可检测").wait_for()
        rows = {r.locator("td").nth(0).inner_text(): r.locator("td").all_inner_texts() for r in negotiated.locator("tbody tr").all()}
        assert rows["保活计数"][1:] == ["2", "3 与请求不同"], "requested 2, granted 3: both shown, the difference marked"
        assert rows["测点队列（最小值）"][1].strip() == "10" and rows["测点队列（最小值）"][2].startswith("1")
        assert "不满足约束：keepalive silence" in negotiated.inner_text()
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
    for name in ("系统总览", "热应力分析", "系统自检", "算法引擎·数字孪生"):
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
    page.get_by_role("button", name="系统管理·权限").click()
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


def test_an_administrator_creates_and_stops_accounts_and_every_step_is_on_record(page, archive_rig):
    sign_in_with(page, archive_rig, "admin:admin")
    page.get_by_role("button", name="系统管理·权限").click()
    page.locator("#account-form").wait_for()
    page.locator('#account-form [name="username"]').fill("operator1")
    page.locator("#account-form button").click()
    try:
        page.locator("#new-credential").wait_for(timeout=8000)
    except Exception as error:  # say what the page and the server looked like, not just that time ran out
        raise AssertionError(f"no credential shown; page: {page.locator('#account-area').inner_text()!r}; accounts uncertain: {archive_rig.accounts.uncertain}; users: {sorted(archive_rig.accounts.users)}") from error
    credential = page.locator("#new-credential").inner_text().split("：", 1)[1].strip()
    assert credential.startswith("operator1:")
    assert api(page, archive_rig, "GET", "/api/me", credential).json() == {"username": "operator1", "role": "viewer"}
    row = page.locator("#account-area tbody tr", has_text="operator1")
    assert "有效" in row.inner_text()
    page.get_by_role("button", name="历史查询").click()  # leaving the page: the credential is not shown a second time
    page.get_by_role("button", name="系统管理·权限").click()
    page.locator("#account-form").wait_for()
    assert page.locator("#new-credential").count() == 0
    row = page.locator("#account-area tbody tr", has_text="operator1")
    row.get_by_role("button", name="停用").click()
    row.get_by_role("button", name="确认停用 operator1").click()  # two clicks: no pop-up dialog
    page.locator("#account-area tbody tr", has_text="operator1").get_by_text("已停用").wait_for()
    assert api(page, archive_rig, "GET", "/api/me", credential).status == 401, "the stopped credential no longer works"
    admin_row = page.locator("#account-area tbody tr", has_text="admin").first
    admin_row.get_by_role("button", name="停用").click()
    admin_row.get_by_role("button", name="确认停用 admin").click()
    page.locator("#account-area").get_by_text("cannot revoke last active admin").wait_for()
    page.locator("#audit-log tbody tr", has_text="REVOKE_ACCOUNT").first.wait_for()
    log = page.locator("#audit-log").inner_text()
    assert "CREATE_ACCOUNT" in log and "REVOKE_ACCOUNT" in log and "operator1" in log and "admin" in log
    assert [e for e in page.errors if "409" not in e] == [], "only the refusal this test provoked (409) shows in the console"


def test_the_model_page_shows_the_real_parameters_version_and_the_research_caveats_and_nothing_heavier(page, archive_rig):
    sign_in(page, archive_rig)
    page.get_by_role("button", name="算法引擎·数字孪生").click()
    card = page.locator("#model-cards .card").first
    card.get_by_text("当前估算示例").wait_for()
    text = card.inner_text()
    model = archive_rig.config.models[0]
    assert model.id in text and f"版本 {model.version}" in text and model.basis in text
    assert "170 GPa" in text and "1.35e-5 1/K" in text and "0.5（绝对变化 ±0.2）" in text and "-50 ～ 800 ℃" in text
    assert "96 路热应力代理量" in text
    assert "参数敏感性范围" in text and "符号约定：第一路温度（内壁）高于第二路（外壁）为正" in text
    whole = page.locator("#workspace").inner_text()
    assert "研究代理量" in whole and "不是统计置信区间" in whole and "本期只有这一个轻量的温差—应力代理模型" in whole
    for heavy in ("RBF", "PINN", "iFEM", "Norton", "蠕变", "剩余寿命", "占位", "即将"):
        assert heavy not in whole, f"no placeholder for a model that does not exist: {heavy}"
    assert page.errors == []


def test_a_database_failure_while_changing_an_account_is_reported_as_unconfirmed_not_as_a_crash(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    admin = accounts.create("admin", "admin")
    owner = Owner(small_config(), 1000.0, 100.0, run_id="t")
    app = create_web_app(config=owner.config, accounts=accounts, publisher=Publisher(owner))

    def down(*args):
        raise psycopg.OperationalError("database down")

    accounts.persist = down
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post("/api/admin/accounts", json={"username": "operator", "role": "viewer"}, headers={"Authorization": "Bearer " + admin})
    assert answer.status_code == 503 and "change not confirmed" in answer.json()["detail"]
    assert accounts.uncertain, "the account state is flagged as needing reconciliation, not silently trusted"
