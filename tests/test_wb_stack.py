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


# --------------------------------------------------------------------------
# A 包：协议翻译层 + 模型路由（2026-11-21 完善批）
# --------------------------------------------------------------------------
def _sse_lines(chunks):
    out = []
    for c in chunks:
        out.append(("data: " + json.dumps(c) + "\n").encode("utf-8"))
    out.append(b"data: [DONE]\n")
    return out


def test_route_model_tiers(rot):
    """A: Claude 档位名按路由表改写；普通模型名不改写"""
    assert rot.route_model("claude-opus-4-5") == "glm-5.3-flash"
    assert rot.route_model("claude-sonnet-4-5") == "glm-5.3-flash"
    assert rot.route_model("claude-haiku-4-5") == "deepseek-v4.1-flash"
    assert rot.route_model("opus") == "glm-5.3-flash"
    assert rot.route_model("glm-5.3-flash") is None
    assert rot.route_model("") is None


def test_anthropic_to_chat_full(rot):
    """A: Anthropic 请求翻译——system/图片/tool_use/tool_result/工具定义"""
    body = {
        "model": "claude-sonnet-4-5", "max_tokens": 1024,
        "system": "你是助手",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "source": {"type": "base64",
                                              "media_type": "image/png",
                                              "data": "AAAA"}}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "我查一下"},
                {"type": "tool_use", "id": "t1", "name": "search",
                 "input": {"q": "x"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "结果"}]}]},
        ],
        "tools": [{"name": "search", "description": "搜",
                   "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
    }
    out = rot.anthropic_to_chat(body)
    assert out["messages"][0] == {"role": "system", "content": "你是助手"}
    um = out["messages"][1]
    assert um["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    am = out["messages"][2]
    assert am["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(am["tool_calls"][0]["function"]["arguments"]) == {"q": "x"}
    tm = out["messages"][3]
    assert tm["role"] == "tool" and tm["tool_call_id"] == "t1"
    assert tm["content"] == "结果"
    assert out["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert out["tool_choice"] == "required"
    assert out["max_tokens"] == 1024


def test_chat_to_anthropic(rot):
    """A: Chat 聚合响应 → Anthropic Message（tool_use/stop_reason/usage 映射）"""
    result = {"id": "abc", "model": "glm-5.3-flash",
              "choices": [{"message": {
                  "role": "assistant", "content": "答",
                  "tool_calls": [{"id": "t1", "type": "function",
                                  "function": {"name": "f",
                                               "arguments": "{\"a\": 1}"}}]},
                  "finish_reason": "tool_calls"}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    out = rot.chat_to_anthropic(result, "glm-5.3-flash")
    assert out["type"] == "message" and out["id"] == "msg_abc"
    assert out["content"][0] == {"type": "text", "text": "答"}
    assert out["content"][1]["type"] == "tool_use"
    assert out["content"][1]["input"] == {"a": 1}
    assert out["stop_reason"] == "tool_use"
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_responses_roundtrip(rot):
    """A: Responses 请求翻译（instructions/函数调用对）与响应对象形状"""
    body = {"model": "gpt-5-codex", "instructions": "系统",
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "c1", "name": "f",
                 "arguments": "{\"x\": 2}"},
                {"type": "function_call_output", "call_id": "c1",
                 "output": "done"}],
            "tools": [{"type": "function", "name": "f", "parameters": {}}],
            "max_output_tokens": 99}
    chat = rot.responses_to_chat(body)
    assert chat["messages"][0]["role"] == "system"
    assert chat["messages"][1] == {"role": "user", "content": "hi"}
    assert chat["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert chat["messages"][3] == {"role": "tool", "tool_call_id": "c1",
                                   "content": "done"}
    assert chat["max_tokens"] == 99
    result = {"id": "z9", "model": "glm-5.3-flash",
              "choices": [{"message": {"role": "assistant", "content": "ok"},
                           "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 3, "completion_tokens": 4,
                        "total_tokens": 7}}
    resp = rot.chat_to_responses(result, "glm-5.3-flash")
    assert resp["object"] == "response" and resp["status"] == "completed"
    assert resp["output"][0]["content"][0] == {
        "type": "output_text", "text": "ok", "annotations": []}
    assert resp["usage"]["total_tokens"] == 7


def test_anthropic_sse_translation(rot):
    """A: 流式翻译——reasoning/text/tool 三类块顺序开闭，收尾 stop_reason"""
    chunks = [
        {"id": "s1", "model": "glm-5.3-flash",
         "choices": [{"delta": {"reasoning_content": "想"}}]},
        {"choices": [{"delta": {"content": "答"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "t1",
             "function": {"name": "f", "arguments": "{\"a\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}}]},
                      "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 9}},
    ]
    sink = {}
    raw = b"".join(rot.anthropic_sse(_sse_lines(chunks), "glm-5.3-flash", sink))
    text = raw.decode("utf-8")
    events = [l[7:] for l in text.splitlines() if l.startswith("event: ")]
    assert events == ["message_start", "content_block_start",
                      "content_block_delta", "content_block_stop",
                      "content_block_start", "content_block_delta",
                      "content_block_stop", "content_block_start",
                      "content_block_delta", "content_block_delta",
                      "content_block_stop", "message_delta", "message_stop"]
    assert '"partial_json": "{\\"a\\":"' in text
    assert '"stop_reason": "tool_use"' in text
    assert sink["usage"]["completion_tokens"] == 9


def test_responses_sse_translation(rot):
    """A: Responses 流式——文本增量与 completed 终态对象完整"""
    chunks = [
        {"id": "s2", "model": "glm-5.3-flash",
         "choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
    ]
    sink = {}
    raw = b"".join(rot.responses_sse(_sse_lines(chunks), "glm-5.3-flash", sink))
    text = raw.decode("utf-8")
    assert "response.created" in text and "response.completed" in text
    assert '"delta": "你"' in text and '"delta": "好"' in text
    done = [l[6:] for l in text.splitlines()
            if l.startswith("data: ") and "response.completed" in l]
    final = json.loads(done[0])["response"]
    assert final["status"] == "completed"
    assert final["output"][0]["content"][0]["text"] == "你好"
    assert final["usage"]["total_tokens"] == 3


# --------------------------------------------------------------------------
# B 包：成长中心（2026-11-21 完善批）
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def grw(mgr):
    import app.growth_client as gc
    return gc


def test_makeup_candidates_rules(grw):
    """B: 补登候选——当月/今天前/上线后/score=0/未补登，最近断点优先"""
    streak = {"data": {"launch_date": "2026-06-01",
                       "streak": {"days": 5, "makeup_dates": ["2026-11-10"]},
                       "makeup_cards": {"balance": 2}}}
    heat = {"data": {"today": {"date": "2026-11-21"}, "cells": [
        {"date": "2026-11-05", "score": 0},
        {"date": "2026-11-08", "score": 0},
        {"date": "2026-11-10", "score": 0},
        {"date": "2026-11-12", "score": 3},
        {"date": "2026-10-30", "score": 0},
        {"date": "2026-11-21", "score": 0},
    ]}}
    assert grw._makeup_candidates(streak, heat) == ["2026-11-08", "2026-11-05"]


class _FakeResp:
    def __init__(self, status, obj):
        self.status = status
        self._b = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_run_growth_full_flow(grw, monkeypatch):
    """B: 全流程编排——领礼物→接单/领奖→连登兑换→抽奖→派出，积分汇总与契约形状"""
    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        path = url.split("/v2/activity/growth", 1)[1]
        body = json.loads(req.data) if req.data else None
        calls.append((req.get_method(), path, body))
        routes = {
            ("GET", "/buddy/travel/status"): (200, {"code": 0, "data": {
                "state": "arrived", "record_id": 9, "daily_limit_reached": False}}),
            ("POST", "/buddy/travel/claim"): (200, {"code": 0, "data": {
                "reward_credit": 50}}),
            ("GET", "/tasks"): (200, {"code": 0, "data": {"tasks": [
                {"task_code": "t_new", "title": "新任务", "locked": False,
                 "accept_status": "not_accepted"},
                {"task_code": "t_done", "title": "已完成", "locked": False,
                 "accept_status": "completed", "reward_credit": 30}]}}),
            ("POST", "/tasks/accept"): (200, {"code": 0, "data": {"results": [
                {"task_code": "t_new", "status": "ok"}]}}),
            ("POST", "/tasks/t_done/claim"): (200, {"code": 0, "data": {
                "credit": 30, "energy": 1}}),
            ("GET", "/streak"): (200, {"code": 0, "data": {
                "makeup_cards": {"balance": 0},
                "streak": {"days": 7, "makeup_dates": []},
                "launch_date": "2026-06-01"}}),
            ("GET", "/redeem/summary"): (200, {"code": 0, "data": {
                "starter_status": "available", "advanced_status": "claimed",
                "legendary_status": "locked"}}),
            ("POST", "/redeem"): (200, {"code": 0, "data": {"credit_granted": 20}}),
            ("GET", "/lottery/chances"): (200, {"code": 0, "data": {"balance": 1}}),
            ("POST", "/lottery/draw"): (200, {"code": 0, "data": {
                "prize_name": "谢谢参与"}}),
            ("GET", "/buddy/quota"): (200, {"code": 0, "data": {
                "affordable": 0, "max_open_count": 1}}),
            ("GET", "/buddy/travel/config"): (200, {"code": 0, "data": {
                "locations": [{"id": 3, "name": "海岛"}]}}),
            ("POST", "/buddy/travel/depart"): (200, {"code": 0, "data": {
                "location": {"name": "海岛", "duration_hours": 6}}}),
            ("GET", "/energy"): (200, {"code": 0, "data": {"balance": 12}}),
        }
        status, obj = routes[(req.get_method(), path)]
        return _FakeResp(status, obj)

    monkeypatch.setattr(grw.wb, "refresh_access_token",
                        lambda rt: (True, "AT", "RT2", "ok"))
    monkeypatch.setattr(grw.urllib.request, "urlopen", fake_urlopen)
    r = grw.run_growth("RT1", "uid-1")
    assert r["ok"] and r["new_rt"] == "RT2"
    assert r["credits_gained"] == 100          # 50 礼物 + 30 任务 + 20 连登
    assert r["energy"] == 12 and r["streak_days"] == 7
    for frag in ("领旅行礼物 +50", "领任务奖", "连登兑换", "开盲盒", "派 Buddy 去海岛"):
        assert frag in r["report"], frag
    accept = [c for c in calls if c[1] == "/tasks/accept"][0]
    assert accept[2] == {"task_codes": ["t_new"]}     # 复数数组契约
    draw = [c for c in calls if c[1] == "/lottery/draw"][0]
    assert str(draw[2].get("client_token", "")).startswith("u-")
    redeem = [c for c in calls if c[1] == "/redeem"][0]
    assert redeem[2]["tier"] == "7d"                  # 档位标识契约


def test_checkin_one_runs_growth_and_chains_rt(mgr, monkeypatch):
    """B: 签到成功后跑成长中心；成长轮换的令牌并入回写且结果进返回值/日志"""
    db, sch = mgr["db"], mgr["sch"]
    aid = db.add_account("成长甲", "g@x.cn", "grt1", 1)
    monkeypatch.setattr(sch.wb, "checkin",
                        lambda tok, uid=None: (True, "签到成功", "10", "grt2", 100, 3))
    monkeypatch.setattr(sch.wb, "query_available_credits",
                        lambda tok: (True, 88.0, 200.0, 112.0, "ok", ""))
    monkeypatch.setattr(sch.growth_client, "run_growth",
                        lambda tok, uid=None: {
                            "ok": True, "report": "领旅行礼物 +50 积分",
                            "credits_gained": 50, "energy": 1,
                            "streak_days": 3, "idle": False, "failures": 0,
                            "auth_failed": False, "new_rt": "grt3"})
    r = sch._checkin_one(db.get_account(aid), "test")
    row = db.get_account(aid)
    assert r["growth"]["credits_gained"] == 50
    assert row["token"] == "grt3"          # 成长中心轮换的最新一代胜出
    logs = db.recent_logs(limit=5, account_id=aid)
    assert any("成长中心" in (l["message"] or "") for l in logs)


def test_gateway_info_and_growth_setting(mgr):
    """UI 包：网关信息接口（路由表+三协议端点）；growth_enabled 设置可落库"""
    main, db = mgr["main"], mgr["db"]
    info = main.api_gateway_info()
    paths = {e["path"] for e in info["endpoints"]}
    assert {"/v1/chat/completions", "/v1/messages", "/v1/responses"} <= paths
    assert info["routes"]["opus"] == "glm-5.3-flash"
    assert info["routes"]["haiku"] == "deepseek-v4.1-flash"
    main.api_set_settings(main.SettingsIn(growth_enabled="0"))
    assert db.get_settings()["growth_enabled"] == "0"
    main.api_set_settings(main.SettingsIn(growth_enabled="1"))
    assert db.get_settings()["growth_enabled"] == "1"


def test_panel_ui_contract():
    """UI 包：面板含成长开关/成长统计卡/网关路由卡的挂载点（防误删守卫）"""
    html = open(os.path.join(ROOT, "wb-manager", "static", "index.html"),
                encoding="utf-8").read()
    for marker in ('id="s_growth"', 'id="st_growth"', 'id="gwRoutes"',
                   "loadGatewayInfo()", "loadGrowthToday()", "growth_enabled"):
        assert marker in html, marker


# --------------------------------------------------------------------------
# 第一阶段·项1：协议回归测试补充（2026-11-23）
# 覆盖：流式/非流式、tool_calls 拼装、错误码映射、SSE 翻译、模型路由命中与回退
# --------------------------------------------------------------------------
class _FakeHandler:
    """最小 HTTP handler 替身：捕获 _json/响应头/响应体，供 _err/_stream_response 离线测试。"""

    def __init__(self, fmt="chat"):
        self._fmt = fmt
        self._ctx = None
        self.headers = {}
        self.wfile = io.BytesIO()
        self.status = None
        self.headers_out = []
        self.last_json = None

    def _json(self, code, obj, log_obj=None):
        self.status = code
        self.last_json = obj

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_out.append((k, v))

    def end_headers(self):
        pass


# ---- 流式 / 非流式 ----
def test_aggregate_stream_text_content(rot):
    """流式文本分片聚合为非流式响应：content 拼接、finish_reason、usage 落账"""
    chunks = [
        {"id": "t1", "model": "glm-5.3-flash",
         "choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    out = rot.aggregate_stream(_sse_lines(chunks))
    msg = out["choices"][0]["message"]
    assert msg["content"] == "你好"
    assert "tool_calls" not in msg
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}
    assert out["model"] == "glm-5.3-flash"
    assert out["id"] == "t1"


def test_aggregate_stream_reasoning_content(rot):
    """reasoning_content 与 content 分别聚合，推理内容不污染正文"""
    chunks = [
        {"id": "r1", "choices": [{"delta": {"reasoning_content": "思考中"}}]},
        {"choices": [{"delta": {"reasoning_content": "继续"}}]},
        {"choices": [{"delta": {"content": "答案"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    out = rot.aggregate_stream(_sse_lines(chunks))
    msg = out["choices"][0]["message"]
    assert msg["content"] == "答案"
    assert msg["reasoning_content"] == "思考中继续"


def test_chat_stream_passthrough(rot):
    """chat 流式透传：上游 SSE 字节原样下发，并从收尾帧抓 usage 记账"""
    sse = [
        ('data: {"id":"c1","choices":[{"delta":{"content":"你"}}]}\n').encode("utf-8"),
        ('data: {"choices":[{"delta":{"content":"好"}}]}\n').encode("utf-8"),
        ('data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n').encode("utf-8"),
        b"data: [DONE]\n",
    ]
    fake = _FakeHandler()
    fake._ctx = {"model": "glm-5.3-flash", "t0": 0, "account": "测试号"}
    rot.ProxyHandler._stream_response(fake, sse)
    assert fake.status == 200
    assert fake.wfile.getvalue() == b"".join(sse)   # 原样透传，不改写


# ---- tool_calls 拼装 ----
def test_aggregate_stream_parallel_tool_calls(rot):
    """并行 tool_calls（多 index）按序拼装，各自 id/name/arguments 独立"""
    lines = [
        b'data: {"id":"p1","choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"id":"c1","function":{"name":"f1","arguments":""}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,'
        b'"id":"c2","function":{"name":"f2","arguments":""}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"{\\"a\\":1}"}}]}}]}',
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,'
        b'"function":{"arguments":"{\\"b\\":2}"}}]},"finish_reason":"tool_calls"}]}',
        b"data: [DONE]",
    ]
    out = rot.aggregate_stream(lines)
    tcs = out["choices"][0]["message"]["tool_calls"]
    assert [tc["id"] for tc in tcs] == ["c1", "c2"]
    assert tcs[0]["function"]["name"] == "f1"
    assert tcs[1]["function"]["name"] == "f2"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"a": 1}
    assert json.loads(tcs[1]["function"]["arguments"]) == {"b": 2}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_anthropic_to_chat_tool_choice_variants(rot):
    """Anthropic tool_choice 映射：auto→auto、any→required、none 剔除工具"""
    body = {"model": "claude-sonnet-4-5", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "f", "input_schema": {"type": "object"}}]}
    body["tool_choice"] = {"type": "auto"}
    assert rot.anthropic_to_chat(body)["tool_choice"] == "auto"
    body["tool_choice"] = {"type": "any"}
    assert rot.anthropic_to_chat(body)["tool_choice"] == "required"


# ---- 错误码映射 ----
def test_is_exhausted_response_mapping(rot):
    """错误码映射：402/429 直接判耗尽；关键词命中且>=400 判耗尽；无关键词/2xx 不判"""
    assert rot.is_exhausted_response(402, "") is True
    assert rot.is_exhausted_response(429, "") is True
    assert rot.is_exhausted_response(400, "insufficient quota") is True
    assert rot.is_exhausted_response(400, "余额不足") is True
    assert rot.is_exhausted_response(500, "credit error") is True    # 关键词命中且>=400
    assert rot.is_exhausted_response(500, "server crash") is False   # 无关键词
    assert rot.is_exhausted_response(400, "unrelated error") is False
    assert rot.is_exhausted_response(200, "credit") is False         # 2xx 不判


def test_err_shape_per_protocol(rot):
    """错误响应形状按协议：chat/responses 用 OpenAI 形态，messages 用 Anthropic 形态"""
    chat = _FakeHandler(fmt="chat")
    rot.ProxyHandler._err(chat, 503, "所有账号今日额度已耗尽", "all_accounts_exhausted")
    assert chat.status == 503
    assert chat.last_json["error"]["message"] == "所有账号今日额度已耗尽"
    assert chat.last_json["error"]["type"] == "all_accounts_exhausted"

    msgs = _FakeHandler(fmt="messages")
    rot.ProxyHandler._err(msgs, 400, "bad request", "invalid_request_error")
    assert msgs.status == 400
    assert msgs.last_json["type"] == "error"
    assert msgs.last_json["error"]["type"] == "invalid_request_error"
    assert msgs.last_json["error"]["message"] == "bad request"


def test_require_auth_401_shape(rot, monkeypatch):
    """未授权：401 + OpenAI 错误形态；x-api-key 与 Bearer 均可通过"""
    monkeypatch.setattr(rot, "AUTH_KEY", "secret-key")
    fake = _FakeHandler()
    assert rot._require_auth(fake) is False
    assert fake.status == 401
    assert fake.last_json == {"error": {"message": "unauthorized"}}

    fake.headers = {"Authorization": "Bearer secret-key"}
    assert rot._require_auth(fake) is True
    fake.headers = {"x-api-key": "secret-key"}
    assert rot._require_auth(fake) is True


# ---- SSE 翻译 ----
def test_anthropic_sse_tool_use_stream(rot):
    """Anthropic 流式 tool_use：input_json_delta 拼装与 stop_reason=tool_use"""
    chunks = [
        {"id": "s1", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "t1",
             "function": {"name": "f", "arguments": "{\"a\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}}]},
            "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 6}},
    ]
    sink = {}
    raw = b"".join(rot.anthropic_sse(_sse_lines(chunks), "glm-5.3-flash", sink))
    text = raw.decode("utf-8")
    assert '"partial_json": "{\\"a\\":"' in text
    assert '"partial_json": "1}"' in text
    assert '"stop_reason": "tool_use"' in text
    assert sink["usage"]["completion_tokens"] == 6


def test_responses_sse_function_call_stream(rot):
    """Responses 流式函数调用：function_call 增量与 completed 终态对象"""
    chunks = [
        {"id": "s2", "choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1",
             "function": {"name": "f", "arguments": "{\"x\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "2}"}}]},
            "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 4}},
    ]
    sink = {}
    raw = b"".join(rot.responses_sse(_sse_lines(chunks), "glm-5.3-flash", sink))
    text = raw.decode("utf-8")
    assert "response.function_call_arguments.delta" in text
    assert "response.function_call_arguments.done" in text
    done = [l[6:] for l in text.splitlines()
            if l.startswith("data: ") and "response.completed" in l]
    final = json.loads(done[0])["response"]
    assert final["status"] == "completed"
    fc = [it for it in final["output"] if it["type"] == "function_call"][0]
    assert fc["call_id"] == "c1" and fc["name"] == "f"
    assert fc["arguments"] == "{\"x\":2}"


# ---- 模型路由命中与回退 ----
def test_route_model_env_override_and_fallback(rot, monkeypatch):
    """WB_MODEL_ROUTES 环境变量覆盖/增补路由；未命中档位回退保持原名"""
    monkeypatch.setenv("WB_MODEL_ROUTES",
                       '{"opus": "hy4free", "gpt": "deepseek-v4.1-flash"}')
    routes = rot._load_model_routes()
    assert routes["opus"] == "hy4free"                 # 覆盖默认
    assert routes["gpt"] == "deepseek-v4.1-flash"      # 增补新键
    assert routes["sonnet"] == "glm-5.3-flash"         # 未覆盖者保留默认
    # 回退：普通模型名与空值不命中任何档位，route_model 返回 None（保持原名）
    assert rot.route_model("glm-5.3-flash") is None
    assert rot.route_model("unknown-model") is None
    assert rot.route_model("") is None
