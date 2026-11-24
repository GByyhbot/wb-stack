"""
quota_engine.py 单元测试：覆盖付费/免费模型分流、95% 事前切号、全员达限返回 503、
高并发预占不穿透、粘性会话切号、跨天自动恢复、超过 5 个账号轮换以及短时 429 隔离。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROTATOR_DIR = os.path.join(ROOT, "wb-rotator")
if ROTATOR_DIR not in sys.path:
    sys.path.insert(0, ROTATOR_DIR)

from quota_engine import AccountCandidate, QuotaEngine, compute_safe_limit


class FakeClock:
    def __init__(self, ts: float) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds: float) -> None:
        self.ts += seconds


class TestQuotaEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "quota.db")
        # 2026-11-24 10:00:00 Asia/Shanghai
        self.clock = FakeClock(1795400400.0)
        self.engine = QuotaEngine(
            self.db_path,
            tz_name="Asia/Shanghai",
            default_safe_percent=95.0,
            default_soft_cooldown=600,
            clock=self.clock,
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_proactive_switch_at_95_percent_and_strict_503(self) -> None:
        """达到 95% 自动切到下一个账号；所有账号均达 95% 后直接停止（返回 None -> 503）。"""
        # 每日限额 20 次 -> 95% 阈值 = 19 次
        self.engine.upsert_limit(
            "glm-5.3-flash",
            requires_credits=True,
            daily_request_limit=20,
            safe_percent=95.0,
        )
        accounts = [
            AccountCandidate(name="acc_a", priority=1, credits=500),
            AccountCandidate(name="acc_b", priority=2, credits=500),
        ]

        # 前 19 次全部走 acc_a
        for _ in range(19):
            res = self.engine.select_and_reserve(accounts, "glm-5.3-flash", sticky_account="acc_a")
            self.assertIsNotNone(res.reservation)
            self.assertEqual(res.reservation.account_name, "acc_a")
            self.engine.commit(res.reservation, prompt_tokens=10, completion_tokens=20)

        # 第 20 次（acc_a 已达 19/20 = 95%）自动切到 acc_b，且模型不变，不撞墙
        res20 = self.engine.select_and_reserve(accounts, "glm-5.3-flash", sticky_account="acc_a")
        self.assertIsNotNone(res20.reservation)
        self.assertEqual(res20.reservation.account_name, "acc_b")
        self.assertEqual(res20.reservation.switched_from, "acc_a")
        self.assertEqual(res20.skip_reasons["acc_a"], "request_threshold_reached")
        self.engine.commit(res20.reservation)

        # 把 acc_b 剩余的 18 次也用完（共 19 次达到 95%）
        for _ in range(18):
            r = self.engine.select_and_reserve(accounts, "glm-5.3-flash", sticky_account="acc_b")
            self.assertIsNotNone(r.reservation)
            self.assertEqual(r.reservation.account_name, "acc_b")
            self.engine.commit(r.reservation)

        # 两个账号都已达 95% -> 严格停止，返回 None（网关返回 503），不继续消耗最后 5%
        res_stop = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNone(res_stop.reservation)
        self.assertTrue(res_stop.all_exhausted)
        self.assertEqual(res_stop.skip_reasons["acc_a"], "request_threshold_reached")
        self.assertEqual(res_stop.skip_reasons["acc_b"], "request_threshold_reached")

    def test_free_model_ignores_zero_credits_but_obeys_95_percent_limit(self) -> None:
        """免费模型在账号积分为 0 时依然可用，但在达到自身 95% 日限额前照常自动切号。"""
        self.engine.upsert_limit(
            "hy4free",
            requires_credits=False,
            daily_request_limit=10,
            safe_percent=95.0,
        )
        self.engine.upsert_limit(
            "glm-5.3-flash",
            requires_credits=True,
            daily_request_limit=100,
            safe_percent=95.0,
        )
        accounts = [
            AccountCandidate(name="acc_zero", priority=1, credits=0),
            AccountCandidate(name="acc_rich", priority=2, credits=1000),
        ]

        # 付费模型跳过积分为 0 的 acc_zero，直接选 acc_rich
        paid_res = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNotNone(paid_res.reservation)
        self.assertEqual(paid_res.reservation.account_name, "acc_rich")
        self.assertEqual(paid_res.skip_reasons["acc_zero"], "credits_exhausted")
        self.engine.release(paid_res.reservation, count_failure=False)

        # 免费模型不看积分，前 9 次（floor(10*0.95)=9）正常使用 acc_zero
        for _ in range(9):
            free_res = self.engine.select_and_reserve(accounts, "hy4free")
            self.assertIsNotNone(free_res.reservation)
            self.assertEqual(free_res.reservation.account_name, "acc_zero")
            self.engine.commit(free_res.reservation)

        # 第 10 次达到 95% 阈值，自动切到 acc_rich
        free_res10 = self.engine.select_and_reserve(accounts, "hy4free")
        self.assertIsNotNone(free_res10.reservation)
        self.assertEqual(free_res10.reservation.account_name, "acc_rich")
        self.assertEqual(free_res10.skip_reasons["acc_zero"], "request_threshold_reached")
        self.engine.commit(free_res10.reservation)

    def test_model_isolation_same_account(self) -> None:
        """同一账号下 A 模型达限，不影响该账号继续使用 B 模型。"""
        self.engine.upsert_limit("glm-5.3-flash", daily_request_limit=2, safe_percent=95.0)
        self.engine.upsert_limit("deepseek-v4.1-flash", daily_request_limit=20, safe_percent=95.0)
        accounts = [AccountCandidate(name="acc_a", priority=1, credits=100)]

        # glm 限额 2 -> 安全阈值 1，用 1 次后即停
        r1 = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNotNone(r1.reservation)
        self.engine.commit(r1.reservation)
        r2 = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNone(r2.reservation)

        # 同账号 deepseek 仍完全可用
        r_ds = self.engine.select_and_reserve(accounts, "deepseek-v4.1-flash")
        self.assertIsNotNone(r_ds.reservation)
        self.assertEqual(r_ds.reservation.account_name, "acc_a")
        self.engine.commit(r_ds.reservation)

    def test_concurrent_reservations_never_exceed_threshold(self) -> None:
        """高并发下内存预占锁确保绝不穿透 95% 安全阈值。"""
        # 限额 20 -> 95% 阈值 = 19
        self.engine.upsert_limit("glm-5.3-flash", daily_request_limit=20, safe_percent=95.0)
        accounts = [AccountCandidate(name="acc_a", priority=1, credits=100)]

        barrier = threading.Barrier(30)

        def worker() -> bool:
            barrier.wait()
            res = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
            if res.reservation is None:
                return False
            self.engine.commit(res.reservation)
            return True

        with ThreadPoolExecutor(max_workers=30) as pool:
            outcomes = list(pool.map(lambda _: worker(), range(30)))

        self.assertEqual(sum(outcomes), 19)

    def test_more_than_five_accounts_rotation(self) -> None:
        """账号池超过 5 个时（如 8 个），突破原 MAX_SWITCH_RETRY=5 限制依次切到第 8 个。"""
        self.engine.upsert_limit("glm-5.3-flash", daily_request_limit=2, safe_percent=95.0)
        accounts = [AccountCandidate(name=f"acc_{i}", priority=i, credits=100) for i in range(1, 9)]

        chosen = []
        for _ in range(8):
            res = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
            self.assertIsNotNone(res.reservation)
            chosen.append(res.reservation.account_name)
            self.engine.commit(res.reservation)

        self.assertEqual(chosen, [f"acc_{i}" for i in range(1, 9)])
        # 第 9 次全部达到阈值 -> 503
        res9 = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNone(res9.reservation)

    def test_daily_window_rollover_and_short_429_isolation(self) -> None:
        """短时 429 不误标日耗尽；跨自然日后额度与日耗尽自动恢复。"""
        self.engine.upsert_limit("glm-5.3-flash", daily_request_limit=100, safe_percent=95.0)
        accounts = [AccountCandidate(name="acc_a", priority=1, credits=100)]

        # 短时 429 带 Retry-After: 30
        self.engine.record_upstream_limit(
            "acc_a",
            "glm-5.3-flash",
            http_status=429,
            is_daily_hard_limit=False,
            headers={"Retry-After": "30"},
            body_text='{"error":"rate_limit_exceeded"}',
        )
        r_cool = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNone(r_cool.reservation)
        self.assertEqual(r_cool.skip_reasons["acc_a"], "rate_limit_cooldown")

        # 31 秒后自动恢复
        self.clock.advance(31)
        r_ok = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNotNone(r_ok.reservation)
        self.engine.commit(r_ok.reservation)

        # 标记当日硬耗尽
        self.engine.record_upstream_limit(
            "acc_a",
            "glm-5.3-flash",
            http_status=429,
            is_daily_hard_limit=True,
            body_text='{"error":"daily_quota_exceeded"}',
        )
        r_hard = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNone(r_hard.reservation)
        self.assertEqual(r_hard.skip_reasons["acc_a"], "daily_exhausted")

        # 推进 24 小时跨自然日 -> 自动恢复
        self.clock.advance(86400)
        r_next_day = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNotNone(r_next_day.reservation)
        self.engine.commit(r_next_day.reservation)

    def test_safe_limit_math(self) -> None:
        self.assertEqual(compute_safe_limit(100, 95), 95)
        self.assertEqual(compute_safe_limit(20, 95), 19)
        self.assertEqual(compute_safe_limit(10, 95), 9)
        self.assertEqual(compute_safe_limit(2, 95), 1)
        self.assertIsNone(compute_safe_limit(None, 95))

    def test_safe_limit_one_never_zero(self) -> None:
        """必修 bug 回归：limit=1 阈值必须为 1，用满 1 次后才拦截，不得永久封死。"""
        self.assertEqual(compute_safe_limit(1, 95), 1)
        self.assertEqual(compute_safe_limit(1, 100), 1)

        self.engine.upsert_limit(
            "glm-5.3-flash", daily_request_limit=1, safe_percent=95.0
        )
        accounts = [AccountCandidate(name="acc_a", priority=1, credits=100)]

        first = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNotNone(first.reservation)
        self.engine.commit(first.reservation)

        second = self.engine.select_and_reserve(accounts, "glm-5.3-flash")
        self.assertIsNone(second.reservation)
        self.assertEqual(second.skip_reasons["acc_a"], "request_threshold_reached")

    def test_rolling_24h_is_loaded_as_daily_with_warning(self) -> None:
        """rolling_24h 只声明未实现：加载按 daily 处理，且在日志明示一行。"""
        self.engine.upsert_limit(
            "glm-5.3-flash",
            daily_request_limit=20,
            window_type="rolling_24h",
        )
        with self.assertLogs("quota_engine", level="WARNING") as captured:
            rule = self.engine.get_effective_rule("rolling_acc", "glm-5.3-flash")
        self.assertEqual(rule.window_type, "daily")
        self.assertTrue(
            any("rolling_24h" in msg and "daily" in msg for msg in captured.output)
        )


# ---------------------------------------------------------------------------
# 网关层接入测试（施工令第 16 条新增用例）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rot_q(tmp_path_factory):
    home = tmp_path_factory.mktemp("rotator_quota")
    saved = os.environ.get("WB_ROTATOR_HOME")
    os.environ["WB_ROTATOR_HOME"] = str(home)
    try:
        spec = importlib.util.spec_from_file_location(
            "rotator_quota_mod", os.path.join(ROTATOR_DIR, "rotator.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if saved is None:
            os.environ.pop("WB_ROTATOR_HOME", None)
        else:
            os.environ["WB_ROTATOR_HOME"] = saved
    mod._test_home = str(home)
    return mod


class _QFakeHandler:
    """_handle 的最小 handler 替身：只捕获响应，不碰真实 socket。"""

    def __init__(self):
        self._fmt = "chat"
        self._ctx = None
        self._client_stream = False
        self._last_usage = None
        self.headers = {}
        self.client_address = ("127.0.0.1", 4321)
        self.wfile = io.BytesIO()
        self.status = None
        self.last_json = None

    def _json(self, code, obj, log_obj=None):
        self.status = code
        self.last_json = obj
        self._last_usage = {"prompt_tokens": 1, "completion_tokens": 2}

    def _err(self, code, message, etype=None):
        self.status = code
        self.last_json = {"error": {"type": etype or "api_error", "message": message}}

    def _stream_response(self, resp):
        self.status = 200

    def _stream_response_translated(self, resp, fmt):
        self.status = 200


def _reset(rot, monkeypatch, db_path, mode="observe", enabled="1"):
    monkeypatch.setenv("WB_DB_PATH", db_path)
    monkeypatch.setenv("WB_QUOTA_MODE", mode)
    monkeypatch.setenv("WB_QUOTA_ENABLED", enabled)
    for fn in ("session_state.json", "model_state.json",
               "circuit_state.json", "pin.json"):
        p = os.path.join(rot.HERE, fn)
        if os.path.isfile(p):
            os.remove(p)
    rot._quota_engine = None
    rot._quota_event_drop_count = 0
    rot._inuse.clear()
    rot.ProxyHandler.sessions = []
    return rot.get_quota_engine()


def _run(rot, monkeypatch, accounts, model, user=None, forward=None):
    with open(rot.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f, ensure_ascii=False)
    fake = _QFakeHandler()
    fake.sessions = [rot.AccountSession(a["name"]) for a in accounts]
    for s in fake.sessions:
        monkeypatch.setattr(s, "get_access_token", lambda: "fake-at")
    monkeypatch.setattr(rot, "forward_stream",
                        forward or (lambda at, payload: object()))
    monkeypatch.setattr(
        rot, "aggregate_stream",
        lambda resp: {"usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    req = {"model": model, "messages": [], "stream": False}
    if user:
        req["user"] = user
    rot._handle(fake, req)
    return fake


def _usage_row(db_path, account, model):
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM model_quota_usage WHERE account_name=? AND model_id=?",
            (account, model)).fetchone()
    finally:
        conn.close()


def test_gateway_observe_would_skip_but_never_blocks(rot_q, monkeypatch, tmp_path, capsys):
    """observe：达阈值仍放行，日志含 would_skip，且超额用量照常记账。"""
    db = str(tmp_path / "observe.db")
    eng = _reset(rot_q, monkeypatch, db, mode="observe")
    eng.upsert_limit("glm-5.3-flash", daily_request_limit=1, safe_percent=95.0)
    accounts = [{"name": "一号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 100}]

    assert _run(rot_q, monkeypatch, accounts, "glm-5.3-flash").status == 200
    assert _run(rot_q, monkeypatch, accounts, "glm-5.3-flash").status == 200
    assert "would_skip" in capsys.readouterr().err
    row = _usage_row(db, "一号", "glm-5.3-flash")
    assert row is not None and row["success_count"] == 2
    assert rot_q._inuse == {}


def test_gateway_enforce_all_threshold_returns_503(rot_q, monkeypatch, tmp_path):
    """enforce：全员达阈值 → 503 quota_threshold_exhausted，不烧剩余缓冲。"""
    db = str(tmp_path / "enforce.db")
    eng = _reset(rot_q, monkeypatch, db, mode="enforce")
    eng.upsert_limit("glm-5.3-flash", daily_request_limit=1, safe_percent=95.0)
    accounts = [{"name": "一号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 100}]

    assert _run(rot_q, monkeypatch, accounts, "glm-5.3-flash").status == 200
    fake = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash")
    assert fake.status == 503
    assert fake.last_json["error"]["type"] == "quota_threshold_exhausted"
    assert _usage_row(db, "一号", "glm-5.3-flash")["success_count"] == 1
    assert rot_q._inuse == {}


def test_threshold_skip_refreshes_sticky_but_pin_does_not(rot_q, monkeypatch, tmp_path):
    """阈值跳过粘性账号后刷新绑定；pin 命中的服务不写粘性刷新。"""
    db = str(tmp_path / "sticky.db")
    eng = _reset(rot_q, monkeypatch, db, mode="enforce")
    eng.upsert_limit("glm-5.3-flash", daily_request_limit=1, safe_percent=95.0)
    accounts = [
        {"name": "一号", "refresh_token": "r1", "priority": 1, "last_credits": 100},
        {"name": "二号", "refresh_token": "r2", "priority": 2, "last_credits": 100},
    ]
    # 一号已达阈值，粘性还绑在一号上
    r = eng.try_reserve(AccountCandidate(name="一号", credits=100), "glm-5.3-flash")
    assert r[0] is not None
    eng.commit(r[0])
    rot_q._save_sticky("sess1", "一号")
    assert rot_q._get_sticky_account("sess1") == "一号"

    fake = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash", user="sess1")
    assert fake.status == 200
    assert rot_q._get_sticky_account("sess1") == "二号"

    # pin=一号、sticky=二号、无阈值拦截 → 一号服务且 sticky 不刷新
    db2 = str(tmp_path / "pin.db")
    eng2 = _reset(rot_q, monkeypatch, db2, mode="enforce")
    eng2.upsert_limit("glm-5.3-flash", daily_request_limit=100, safe_percent=95.0)
    with open(rot_q.PIN_FILE, "w", encoding="utf-8") as f:
        json.dump({"name": "一号"}, f)
    rot_q._save_sticky("sess2", "二号")
    fake2 = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash", user="sess2")
    assert fake2.status == 200
    assert rot_q._get_sticky_account("sess2") == "二号"


def test_quota_skip_does_not_touch_circuit_state(rot_q, monkeypatch, tmp_path):
    """引擎阈值 skip 不计入 circuit_state 失败计数。"""
    db = str(tmp_path / "circuit.db")
    eng = _reset(rot_q, monkeypatch, db, mode="enforce")
    eng.upsert_limit("glm-5.3-flash", daily_request_limit=1, safe_percent=95.0)
    accounts = [{"name": "一号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 100}]
    _run(rot_q, monkeypatch, accounts, "glm-5.3-flash")  # 成功，usage=1
    _run(rot_q, monkeypatch, accounts, "glm-5.3-flash")  # 阈值拦截 503
    # 造一条既有失败基线，确认后续阈值 skip 不会动它
    rot_q._record_circuit_fail("一号", "glm-5.3-flash")
    with open(rot_q.CIRCUIT_STATE_FILE, encoding="utf-8") as f:
        before = json.load(f)
    _run(rot_q, monkeypatch, accounts, "glm-5.3-flash")
    with open(rot_q.CIRCUIT_STATE_FILE, encoding="utf-8") as f:
        after = json.load(f)
    assert before == after


def test_gateway_exception_path_releases_both_reservations(rot_q, monkeypatch, tmp_path):
    """上游异常切号后：_inuse 与引擎 in_flight 双双归零。"""
    db = str(tmp_path / "except.db")
    eng = _reset(rot_q, monkeypatch, db, mode="enforce")
    accounts = [{"name": "一号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 100}]

    def boom(at, payload):
        raise urllib.error.URLError("TLS EOF")

    fake = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash", forward=boom)
    assert fake.status == 503
    assert rot_q._inuse == {}
    assert sum(eng._in_flight.values()) == 0


def test_free_model_allows_zero_credits_paid_model_skips(rot_q, monkeypatch, tmp_path):
    """免费模型 credits=0 可用；付费模型 credits=0 被 skip。"""
    db = str(tmp_path / "credits.db")
    eng = _reset(rot_q, monkeypatch, db, mode="enforce")
    accounts = [{"name": "零号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 0}]
    free = _run(rot_q, monkeypatch, accounts, "hy4free")
    assert free.status == 200
    assert _usage_row(db, "零号", "hy4free")["success_count"] == 1

    paid = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash")
    assert paid.status == 503
    assert paid.last_json["error"]["type"] == "all_accounts_exhausted"


def test_429_event_records_headers_body_and_failure_is_tolerated(rot_q, monkeypatch, tmp_path):
    """429 观测落盘含 headers/body；落盘异常被吞掉，不影响请求主流程。"""
    db = str(tmp_path / "event.db")
    eng = _reset(rot_q, monkeypatch, db, mode="observe")
    accounts = [{"name": "一号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 100}]

    def rate_limited(at, payload):
        raise urllib.error.HTTPError(
            "http://upstream", 429, "Too Many Requests",
            {"Retry-After": "30"}, io.BytesIO(b'{"error":"rate_limit_exceeded"}'))

    fake = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash", forward=rate_limited)
    assert fake.status is not None
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT * FROM model_quota_events "
            "WHERE event_type='upstream_exhaust_response'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "Retry-After" in row[8] and "rate_limit_exceeded" in row[9]

    def broken_event(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(eng, "record_event", broken_event)
    # 第一次 429 已把该账号×模型软冷却，第二次需绕过现行冷却才能再次触发 429
    monkeypatch.setattr(rot_q, "model_blocked", lambda name, model: False)
    before = rot_q._quota_event_drop_count
    fake2 = _run(rot_q, monkeypatch, accounts, "glm-5.3-flash", forward=rate_limited)
    assert fake2.status is not None
    assert rot_q._quota_event_drop_count == before + 1


def test_quota_disabled_is_full_bypass(rot_q, monkeypatch, tmp_path, capsys):
    """WB_QUOTA_ENABLED=0 完全旁路：达限额也不 skip、不记账、无 would_skip 日志。"""
    db = str(tmp_path / "off.db")
    eng = _reset(rot_q, monkeypatch, db, mode="enforce", enabled="0")
    assert eng is None
    accounts = [{"name": "一号", "refresh_token": "rt", "priority": 1,
                 "last_credits": 0}]
    assert _run(rot_q, monkeypatch, accounts, "glm-5.3-flash").status == 200
    assert _run(rot_q, monkeypatch, accounts, "glm-5.3-flash").status == 200
    assert "would_skip" not in capsys.readouterr().err
    # 完全旁路：引擎连 DB 都不碰，额度库根本不会被创建
    assert not os.path.isfile(db)


def test_quota_snapshot_endpoint_when_engine_disabled(rot_q, monkeypatch, tmp_path):
    """引擎关闭时 /quota/snapshot 仍返回 200，quota_enabled=false 且 cells 为空。"""
    _reset(rot_q, monkeypatch, str(tmp_path / "q2off.db"), enabled="0")
    with open(rot_q.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": [{"name": "一号", "refresh_token": "rt", "priority": 1}]}, f)
    fake = _QFakeHandler()
    fake.path = "/quota/snapshot"
    rot_q.ProxyHandler.do_GET(fake)
    assert fake.status == 200
    assert fake.last_json["quota_enabled"] is False
    assert fake.last_json["cells"] == []
    assert len(fake.last_json["accounts"]) == 1


def test_quota_snapshot_merges_model_state_fields(rot_q, monkeypatch, tmp_path):
    """快照每格合并 model_state 的 soft_cooldown_until 与 exhausted_today。"""
    db = str(tmp_path / "q2snap.db")
    eng = _reset(rot_q, monkeypatch, db)
    eng.upsert_limit("glm-5.3-flash", daily_request_limit=10, safe_percent=95.0)
    accounts = [
        {"name": "一号", "refresh_token": "r1", "priority": 1},
        {"name": "二号", "refresh_token": "r2", "priority": 2},
    ]
    with open(rot_q.ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"accounts": accounts}, f)
    with open(rot_q.MODEL_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "一号|glm-5.3-flash": {"date": rot_q.today(), "cooldown_until": time.time() + 120},
            "二号|glm-5.3-flash": {"date": rot_q.today(), "hard": True},
        }, f)
    fake = _QFakeHandler()
    fake.path = "/quota/snapshot"
    rot_q.ProxyHandler.do_GET(fake)
    assert fake.status == 200
    snap = fake.last_json
    assert snap["quota_mode"] == "observe"
    assert len(snap["cells"]) == len(accounts) * len(rot_q.QUOTA_MODELS) == 8
    by_key = {(c["account_name"], c["model_id"]): c for c in snap["cells"]}
    soft = by_key[("一号", "glm-5.3-flash")]
    assert soft["soft_cooldown_until"] > time.time()
    assert soft["exhausted_today"] is False
    hard = by_key[("二号", "glm-5.3-flash")]
    assert hard["exhausted_today"] is True
    assert hard["soft_cooldown_until"] is None
    # 未配置 model_state 的模型格不受影响
    assert by_key[("一号", "hy4free")]["exhausted_today"] is False


if __name__ == "__main__":
    unittest.main()