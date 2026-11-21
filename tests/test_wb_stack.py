# -*- coding: utf-8 -*-
"""
wb-stack 修复回归测试（2026-11-21 审计修复批配套）。

覆盖：B1 签到口径统一、B2 priority 选号、B3 名称唯一解析、B4 刷新失败分类、
B5 额度刷新异常兜底、B6 死设置生效、A1 双向令牌同步、A2 跨进程文件锁、
C1 非流式 tool_calls 聚合。

全部离线运行：上游接口一律 mock，数据库与账号文件都在临时目录。
运行：python -m pytest tests -q （需 fastapi/apscheduler/pydantic，与面板同依赖）
"""
import importlib.util
import io
import json
import os
import sqlite3
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# rotator
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rot(tmp_path_factory):
    home = tmp_path_factory.mktemp("rotator")
    os.environ["WB_ROTATOR_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location(
        "rotator_mod", os.path.join(ROOT, "wb-rotator", "rotator.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._test_home = str(home)
    return m


def _write_pool(rot, accounts):
    with open(rot.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, ensure_ascii=False)


def test_load_accounts_sorts_by_priority(rot):
    """B2: 文件顺序与 priority 不一致时，加载即按 priority 排序"""
    _write_pool(rot, [
        {"name": "乙", "refresh_token": "x", "priority": 100},
        {"name": "甲", "refresh_token": "y", "priority": 1},
    ])
    assert [a["name"] for a in rot.load_accounts()] == ["甲", "乙"]


def test_resolve_name(rot):
    """B3: 全等优先；前缀唯一命中才生效；多命中返回 None 防串号"""
    assert rot.resolve_name("备用1", ["备用1", "备用10"]) == "备用1"
    assert rot.resolve_name("备用", ["备用1", "备用10"]) is None
    assert rot.resolve_name("主号", ["主号(主人)", "备用1"]) == "主号(主人)"
    assert rot.resolve_name("", ["甲"]) is None
    assert rot.resolve_name("不存在", ["甲"]) is None


def test_aggregate_stream_tool_calls(rot):
    """C1: 非流式聚合必须拼装 tool_calls 并透传 finish_reason"""
    lines = [
        b'data: {"id":"c1","model":"glm-5.3-flash","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"city\\":"}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"BJ\\"}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":3,"completion_tokens":4}}',
        b"data: [DONE]",
    ]
    out = rot.aggregate_stream(lines)
    msg = out["choices"][0]["message"]
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"city":"BJ"}'
    assert out["usage"]["prompt_tokens"] == 3


def test_refresh_failure_classification(rot, monkeypatch):
    """B4: 网络异常=network，上游明确拒绝=auth，会话层抛对应类型"""
    def raise_urlerror(req, timeout=None):
        raise urllib.error.URLError("connection reset")
    monkeypatch.setattr(rot.urllib.request, "urlopen", raise_urlerror)
    ok, _at, _rt, _msg, kind = rot.refresh_access_token_ex("rt-x")
    assert not ok and kind == "network"

    body = json.dumps({"code": 1001, "msg": "invalid refresh token"}).encode()

    def raise_httperror(req, timeout=None):
        raise urllib.error.HTTPError(
            rot.REFRESH_URL, 401, "Unauthorized", {}, io.BytesIO(body))
    monkeypatch.setattr(rot.urllib.request, "urlopen", raise_httperror)
    ok, _at, _rt, _msg, kind = rot.refresh_access_token_ex("rt-x")
    assert not ok and kind == "auth"

    _write_pool(rot, [{"name": "会话号", "refresh_token": "rt-x", "priority": 1}])
    s = rot.AccountSession("会话号")
    with pytest.raises(rot.TokenAuthError):
        s.get_access_token()
    monkeypatch.setattr(rot.urllib.request, "urlopen", raise_urlerror)
    with pytest.raises(rot.TokenNetworkError):
        s.get_access_token()


def test_persist_rotation_syncs_panel_db(rot, tmp_path, monkeypatch):
    """A1: 网关轮换新令牌后同步回面板 DB（同名精确匹配）"""
    dbf = tmp_path / "panel.db"
    conn = sqlite3.connect(str(dbf))
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT, token TEXT)")
    conn.execute("INSERT INTO accounts (name, token) VALUES ('同步号', 'rt_old')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("WB_DB_PATH", str(dbf))
    _write_pool(rot, [{"name": "同步号", "refresh_token": "rt_old", "priority": 1}])
    acc = {"name": "同步号", "refresh_token": "rt_old"}
    rot.persist_rotation(acc, "rt_new")
    pool = rot.load_accounts()
    assert pool[0]["refresh_token"] == "rt_new"
    conn = sqlite3.connect(str(dbf))
    row = conn.execute("SELECT token FROM accounts WHERE name='同步号'").fetchone()
    conn.close()
    assert row[0] == "rt_new"


def test_accounts_file_lock(rot):
    """A2: 跨进程文件锁可获取/释放，锁文件落在数据目录"""
    with rot._accounts_file_lock():
        assert os.path.isfile(rot.ACCOUNTS_FILE + ".lock")


# --------------------------------------------------------------------------
# wb-manager
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mgr(tmp_path_factory):
    home = tmp_path_factory.mktemp("manager")
    rot_home = home / "rotator"
    rot_home.mkdir()
    os.environ["WB_DB_PATH"] = str(home / "wb.db")
    os.environ["WB_ROTATOR_HOME"] = str(rot_home)
    sys.path.insert(0, os.path.join(ROOT, "wb-manager"))
    import app.db as db
    import app.gateway_sync as gs
    import app.scheduler as sch
    import app.main as main
    db.init_db()
    return {"db": db, "gs": gs, "sch": sch, "main": main,
            "rot_home": str(rot_home)}


def test_checkin_one_quota_semantics(mgr, monkeypatch):
    """B1: 签到后处理不得把累计总积分写进 quota_total；以可用积分口径落账"""
    db, sch = mgr["db"], mgr["sch"]
    aid = db.add_account("签到甲", "u@x.cn", "rt1", 1)
    monkeypatch.setattr(sch.wb, "checkin",
                        lambda tok, uid=None: (True, "签到成功", "10", "rt2", 9999, 5))
    monkeypatch.setattr(sch.wb, "query_available_credits",
                        lambda tok: (True, 120.0, 500.0, 380.0, "ok", ""))
    r = sch._checkin_one(db.get_account(aid), "test")
    row = db.get_account(aid)
    assert r["ok"] and r["available_credits"] == 120.0
    assert row["quota_total"] == 500.0   # 总发放 granted，而非累计 9999
    assert row["quota_used"] == 380.0
    assert row["token"] == "rt2"         # 轮换回写
    assert row["checkin_streak"] == 5


def test_pick_active_switch_setting(mgr):
    """B6: switch_enabled=0 时不顺延耗尽的主账号；=1 时跳过耗尽账号"""
    db = mgr["db"]
    a1 = db.add_account("开关主", "", "t1", 1)
    a2 = db.add_account("开关备", "", "t2", 2)
    # 模块级共享 DB 里有其他用例的账号，先全部停用，只留本用例两个参与选号
    for other in db.list_accounts():
        if other["id"] not in (a1, a2):
            db.update_account(other["id"], status="disabled")
    db.update_account(a1, quota_total=100, quota_used=100,
                      quota_checked_at="2026-11-21 00:00:00")
    db.update_account(a2, quota_total=100, quota_used=0,
                      quota_checked_at="2026-11-21 00:00:00")
    db.set_setting("switch_enabled", "1")
    assert db.pick_active_account()["id"] == a2
    db.set_setting("switch_enabled", "0")
    assert db.pick_active_account()["id"] == a1
    db.set_setting("switch_enabled", "1")


def test_gateway_sync_rt(mgr, monkeypatch):
    """A1/A2/B3: 面板→网关同步走文件锁；前缀歧义不写、不猜"""
    gs = mgr["gs"]
    monkeypatch.setenv("WB_ROTATOR_HOME", mgr["rot_home"])
    path = os.path.join(mgr["rot_home"], "accounts.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"accounts": [
            {"name": "同步甲", "refresh_token": "old", "priority": 1},
            {"name": "备用1", "refresh_token": "r1", "priority": 2},
            {"name": "备用10", "refresh_token": "r10", "priority": 3},
        ]}, f, ensure_ascii=False)
    assert gs.sync_gateway_rt("同步甲", "new") is True
    with open(path, encoding="utf-8") as f:
        pool = json.load(f)["accounts"]
    assert pool[0]["refresh_token"] == "new"
    assert gs.sync_gateway_rt("备用", "x") is False
    with open(path, encoding="utf-8") as f:
        pool = json.load(f)["accounts"]
    assert pool[1]["refresh_token"] == "r1" and pool[2]["refresh_token"] == "r10"


def test_quota_refresh_survives_one_bad_account(mgr, monkeypatch):
    """B5: 单账号刷新抛异常不得中断整批"""
    db, sch = mgr["db"], mgr["sch"]
    bad = db.add_account("故障号", "", "tok_bad", 1)
    good = db.add_account("正常号", "", "tok_good", 2)

    def fake_full(tok):
        if tok == "tok_bad":
            raise RuntimeError("落库模拟失败")
        return {"ok": True, "available": 50.0, "granted": 100.0, "used": 50.0,
                "expire_at": None, "days_left": None, "msg": "ok", "new_rt": ""}
    monkeypatch.setattr(sch.wb, "query_quota_full", fake_full)
    sch.run_quota_refresh()
    assert db.get_account(good)["quota_checked_at"]


def test_pick_gateway_active_uses_priority_not_file_order(mgr, monkeypatch):
    """B2/B3: 网关选号按 priority 而非文件顺序；耗尽跳过；名字唯一解析"""
    db, main = mgr["db"], mgr["main"]
    monkeypatch.setenv("WB_ROTATOR_HOME", mgr["rot_home"])
    m1 = db.add_account("网关主", "", "g1", 1)
    m2 = db.add_account("网关备", "", "g2", 2)
    db.update_account(m1, quota_total=100, quota_used=100,
                      quota_checked_at="2026-11-21 00:00:00")
    db.update_account(m2, quota_total=100, quota_used=10,
                      quota_checked_at="2026-11-21 00:00:00")
    path = os.path.join(mgr["rot_home"], "accounts.json")
    with open(path, "w", encoding="utf-8") as f:
        # 文件顺序故意与 priority 相反
        json.dump({"accounts": [
            {"name": "网关备", "refresh_token": "g2", "priority": 2, "disabled": False},
            {"name": "网关主", "refresh_token": "g1", "priority": 1, "disabled": False},
        ]}, f, ensure_ascii=False)
    assert main._pick_gateway_active()["id"] == m2   # 主号耗尽 → 备号
    db.update_account(m1, quota_used=0)
    assert main._pick_gateway_active()["id"] == m1   # 主号恢复 → 按 priority 选主号


def test_main_resolve_name(mgr):
    main = mgr["main"]
    assert main._resolve_name("备用", ["备用1", "备用10"]) is None
    assert main._resolve_name("备用1", ["备用1", "备用10"]) == "备用1"
    assert main._resolve_name("主号", ["主号(主人)", "备用1"]) == "主号(主人)"


def test_pick_excludes_tried_accounts(rot):
    """B4 配套: exclude 内的账号在本次请求内不再被选中"""
    _write_pool(rot, [
        {"name": "一号", "refresh_token": "a", "priority": 1},
        {"name": "二号", "refresh_token": "b", "priority": 2},
    ])
    sessions = [rot.AccountSession("一号"), rot.AccountSession("二号")]
    assert rot.pick_available_session(sessions).name == "一号"
    assert rot.pick_available_session(sessions, exclude=["一号"]).name == "二号"
    assert rot.pick_available_session(sessions, exclude=["一号", "二号"]) is None


# --------------------------------------------------------------------------
# 每日用量推荐引擎（app/recommendation.py）
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rec():
    spec = importlib.util.spec_from_file_location(
        "recommendation_mod",
        os.path.join(ROOT, "wb-manager", "app", "recommendation.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _acc(name, **kw):
    base = {"name": name, "available": None, "days_left": None, "expire_at": None,
            "status": "active", "in_gateway": True,
            "gateway_disabled": False, "gateway_exhausted": False}
    base.update(kw)
    return base


def test_recommendation_expiry_first(rec):
    accounts = [_acc("富裕号", available=900, days_left=30),
                _acc("临期号", available=300, days_left=2)]
    usage = {("富裕号", "m1"): {"requests": 70, "tokens": 1000, "failed": 0, "limited": 0},
             ("临期号", "m1"): {"requests": 70, "tokens": 1000, "failed": 0, "limited": 0}}
    out = rec.build_recommendation(accounts, usage, {}, window_days=7)
    m = out["models"][0]
    assert m["primary"]["account"] == "临期号"          # 到期最近者优先
    assert m["backup"]["account"] == "富裕号"
    assert sum(s["suggested_requests"] for s in m["shares"]) == 20   # 日均 140/7
    assert any(r["type"] == "expiring" and r["account"] == "临期号" for r in out["risks"])


def test_recommendation_respects_limits(rec):
    accounts = [_acc("A", available=100, days_left=5),
                _acc("B", available=100, days_left=5)]
    usage = {("A", "m1"): {"requests": 700, "tokens": 1, "failed": 0, "limited": 0}}
    limits = {"A|m1": {"requests": 10, "tokens": 0}}
    out = rec.build_recommendation(accounts, usage, limits, window_days=7)
    shares = {s["account"]: s["suggested_requests"] for s in out["models"][0]["shares"]}
    assert shares["A"] == 10                            # 被每日限量封顶
    assert shares["B"] == 90                            # 溢出转移到 B


def test_recommendation_excludes_dead_and_flags_single(rec):
    accounts = [_acc("活号", available=50, days_left=9),
                _acc("空号", available=0, days_left=9),
                _acc("坏号", available=50, days_left=9, status="error")]
    usage = {("活号", "m1"): {"requests": 14, "tokens": 1, "failed": 0, "limited": 0}}
    out = rec.build_recommendation(accounts, usage, {}, window_days=7)
    m = out["models"][0]
    assert m["primary"]["account"] == "活号" and m["backup"] is None
    types = {(r["type"], r.get("account")) for r in out["risks"]}
    assert ("single_account", "活号") in types
    assert ("exhausted", "空号") in types and ("error", "坏号") in types
    by_name = {a["account"]: a for a in out["accounts"]}
    assert by_name["空号"]["candidate"] is False
    assert by_name["活号"]["daily_credits_target"] == round(50 / 9, 1)


def test_recommendation_deprioritizes_unhealthy(rec):
    accounts = [_acc("稳号", available=100, days_left=10),
                _acc("抖号", available=100, days_left=10)]
    usage = {("稳号", "m1"): {"requests": 10, "tokens": 1, "failed": 0, "limited": 0},
             ("抖号", "m1"): {"requests": 10, "tokens": 1, "failed": 8, "limited": 0}}
    out = rec.build_recommendation(accounts, usage, {}, window_days=7)
    assert out["models"][0]["primary"]["account"] == "稳号"
