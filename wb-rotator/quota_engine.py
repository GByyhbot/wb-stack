"""
wb-stack 模型级每日额度监控与事前主动切号引擎 (QuotaEngine)

核心特性：
1. 免费模型与付费积分模型分流：
   - requires_credits=1（付费模型）：总积分 <= 0 时跳过；总积分充足但该模型达到 95% 限额时提前切号。
   - requires_credits=0（免费模型）：跳过总积分检查（即使账号积分为 0 仍可使用），仅按该模型每日限额 95% 提前切号。
2. 内存并发预占 + SQLite 持久化分离：
   - 在途请求预占 (in_flight) 仅保存在进程内存并加锁保护，容器重启/异常退出自动归零，彻底杜绝 DB 预占泄漏。
   - SQLite (WAL + BEGIN IMMEDIATE + busy_timeout) 仅在请求完成或状态变更时落盘，避免高并发写锁阻塞。
3. 严格 95% 提前切号 + 全员达限直接停止 (503)：
   - 选号时对 pin / 粘性会话 / 优先级队列统一执行事前额度硬预检。
   - 所有账号均达到安全阈值或不可用时，直接返回 None（网关返回 HTTP 503），不继续消耗剩余 5% 缓冲额度。
4. 两层配置优先级：
   - 支持 account_name='*' 作为模型全局默认限额，单账号特例配置覆盖全局默认。
   - 未知限额保存为 NULL，绝不伪造固定上限。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


DEFAULT_TZ_NAME = os.environ.get("TZ", "Asia/Shanghai")
DEFAULT_SAFE_PERCENT = float(os.environ.get("WB_QUOTA_SAFE_PERCENT", "95"))
DEFAULT_SOFT_COOLDOWN_SECONDS = int(os.environ.get("WB_QUOTA_SOFT_COOLDOWN", "600"))
DEFAULT_BUSY_TIMEOUT_MS = 5000
WILDCARD_ACCOUNT = "*"

_LOG = logging.getLogger("quota_engine")
_ROLLING_WARN_LOCK = threading.Lock()
_ROLLING_WARNED: set[Tuple[str, str]] = set()


def _effective_window_type(account_name: str, model_id: str, window_type: str) -> str:
    """二期第 1 批：rolling_24h 只有声明、没有实现，加载时按 daily 处理并显式告警一次。"""
    if window_type != "rolling_24h":
        return window_type
    key = (account_name, model_id)
    with _ROLLING_WARN_LOCK:
        if key not in _ROLLING_WARNED:
            _ROLLING_WARNED.add(key)
            _LOG.warning(
                "quota_engine: account=%s model=%s 规则 window_type=rolling_24h "
                "本批按 daily 自然日窗口处理（滚动窗口未实现）",
                account_name,
                model_id,
            )
    return "daily"


@dataclass(frozen=True)
class AccountCandidate:
    """网关传入的候选账号快照。"""

    name: str
    priority: int = 100
    enabled: bool = True
    credits: Optional[float] = None  # None 表示尚未查询到余额（不拦截）


@dataclass(frozen=True)
class QuotaRule:
    """解析后的最终生效规则（单账号特例 > '*' 模型默认 > 系统默认）。"""

    model_id: str
    account_name: str
    requires_credits: bool = True
    daily_request_limit: Optional[int] = None
    daily_token_limit: Optional[int] = None
    safe_percent: float = DEFAULT_SAFE_PERCENT
    observed_limit: Optional[int] = None
    limit_source: str = "unknown"
    window_type: str = "daily"  # 'daily' | 'rolling_24h'
    enabled: bool = True


@dataclass(frozen=True)
class Reservation:
    """一次请求的内存并发预占凭证，必须在请求结束时 commit 或 release。"""

    account_name: str
    model_id: str
    window_key: str
    reserved_at: float
    switched_from: Optional[str] = None


@dataclass(frozen=True)
class SelectionResult:
    """选号结果：成功返回预占凭证，全部不可用时 reservation 为 None（网关应返回 503）。"""

    reservation: Optional[Reservation]
    skip_reasons: Dict[str, str]
    all_exhausted: bool


def _now_dt(tz_name: str = DEFAULT_TZ_NAME, now_ts: Optional[float] = None) -> dt.datetime:
    tz = ZoneInfo(tz_name)
    if now_ts is None:
        return dt.datetime.now(tz)
    return dt.datetime.fromtimestamp(now_ts, tz)


def _iso_now(tz_name: str = DEFAULT_TZ_NAME, now_ts: Optional[float] = None) -> str:
    return _now_dt(tz_name, now_ts).isoformat(timespec="seconds")


def _daily_window_key(tz_name: str = DEFAULT_TZ_NAME, now_ts: Optional[float] = None) -> str:
    return _now_dt(tz_name, now_ts).strftime("%Y-%m-%d")


def compute_safe_limit(limit: Optional[int], safe_percent: float) -> Optional[int]:
    """计算提前切号阈值：在达到 limit 之前触发切换。"""
    if limit is None or limit <= 0:
        return None
    pct = max(1.0, min(100.0, float(safe_percent)))
    threshold = int(math.floor(limit * (pct / 100.0)))
    if pct < 100.0 and limit > 1:
        threshold = min(limit - 1, threshold)
    # 限额为 1 时阈值不能压到 0，否则该账号×模型被永久拦截。
    return max(1, threshold)


def parse_retry_after(value: Optional[str], now_ts: Optional[float] = None) -> Optional[int]:
    """解析 HTTP Retry-After（秒数或 HTTP-date），返回相对秒数。"""
    if not value:
        return None
    raw = str(value).strip()
    if raw.isdigit():
        return max(0, int(raw))
    try:
        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=dt.timezone.utc)
        base = now_ts if now_ts is not None else time.time()
        return max(0, int(target.timestamp() - base))
    except Exception:
        return None


def parse_reset_timestamp(headers: Optional[Dict[str, str]], now_ts: Optional[float] = None) -> Optional[float]:
    """从响应头尝试提取上游重置时间戳（Unix epoch 秒）。"""
    if not headers:
        return None
    lower = {k.lower(): v for k, v in headers.items() if v is not None}
    base = now_ts if now_ts is not None else time.time()

    for key in ("x-ratelimit-reset", "x-ratelimit-reset-requests", "ratelimit-reset"):
        val = lower.get(key)
        if not val:
            continue
        s = str(val).strip()
        try:
            num = float(s)
            # 大于 1e9 视为 Unix 时间戳，否则视为相对秒数
            return num if num > 1_000_000_000 else base + max(0.0, num)
        except ValueError:
            pass

    ra = parse_retry_after(lower.get("retry-after"), now_ts=base)
    if ra is not None:
        return base + ra
    return None


class QuotaEngine:
    """
    模型级额度账本与事前主动切号引擎。

    线程安全：
    - 内存锁 `_lock` 保护 `_in_flight` 并发预占与选号原子性。
    - SQLite 每次操作使用短事务 + WAL + `busy_timeout`，支持 wb-rotator 与 wb-manager 跨进程共享。
    """

    def __init__(
        self,
        db_path: str,
        *,
        tz_name: str = DEFAULT_TZ_NAME,
        default_safe_percent: float = DEFAULT_SAFE_PERCENT,
        default_soft_cooldown: int = DEFAULT_SOFT_COOLDOWN_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.db_path = db_path
        self.tz_name = tz_name
        self.default_safe_percent = float(default_safe_percent)
        self.default_soft_cooldown = int(default_soft_cooldown)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        # key: (account_name, model_id, window_key) -> active in-flight count
        self._in_flight: Dict[Tuple[str, str, str], int] = {}
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,  # 手动控制 BEGIN IMMEDIATE
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def init_schema(self) -> None:
        """幂等创建额度表，不破坏 wb_manager.db 现有表结构。"""
        with closing(self._connect()) as conn:
            conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS model_quota_limits (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_name TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        requires_credits INTEGER NOT NULL DEFAULT 1,
                        daily_request_limit INTEGER,
                        daily_token_limit INTEGER,
                        safe_percent REAL NOT NULL DEFAULT 95.0,
                        observed_limit INTEGER,
                        limit_source TEXT NOT NULL DEFAULT 'unknown',
                        window_type TEXT NOT NULL DEFAULT 'daily',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        UNIQUE(account_name, model_id)
                    );

                    CREATE TABLE IF NOT EXISTS model_quota_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_name TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        window_key TEXT NOT NULL,
                        window_start TEXT,
                        reset_at TEXT,
                        request_count INTEGER NOT NULL DEFAULT 0,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        completion_tokens INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        UNIQUE(account_name, model_id, window_key)
                    );

                    CREATE TABLE IF NOT EXISTS model_quota_state (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_name TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        hard_exhausted_window TEXT,
                        cooldown_until REAL,
                        reset_at REAL,
                        last_error_code TEXT,
                        updated_at TEXT NOT NULL,
                        UNIQUE(account_name, model_id)
                    );

                    CREATE TABLE IF NOT EXISTS model_quota_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_name TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        http_status INTEGER,
                        error_code TEXT,
                        retry_after INTEGER,
                        reset_at TEXT,
                        raw_headers TEXT,
                        raw_body TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_mqu_lookup
                        ON model_quota_usage(account_name, model_id, window_key);
                    CREATE INDEX IF NOT EXISTS idx_mqe_created
                        ON model_quota_events(created_at DESC);
                    """
            )

    # ------------------------------------------------------------------
    # 规则配置
    # ------------------------------------------------------------------

    def upsert_limit(
        self,
        model_id: str,
        *,
        account_name: str = WILDCARD_ACCOUNT,
        requires_credits: bool = True,
        daily_request_limit: Optional[int] = None,
        daily_token_limit: Optional[int] = None,
        safe_percent: Optional[float] = None,
        limit_source: str = "manual",
        window_type: str = "daily",
        enabled: bool = True,
    ) -> None:
        """新增或更新模型每日限额规则。account_name='*' 表示模型全局默认规则。"""
        now_iso = _iso_now(self.tz_name, self._clock())
        sp = self.default_safe_percent if safe_percent is None else float(safe_percent)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO model_quota_limits (
                        account_name, model_id, requires_credits,
                        daily_request_limit, daily_token_limit, safe_percent,
                        limit_source, window_type, enabled, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_name, model_id) DO UPDATE SET
                        requires_credits = excluded.requires_credits,
                        daily_request_limit = excluded.daily_request_limit,
                        daily_token_limit = excluded.daily_token_limit,
                        safe_percent = excluded.safe_percent,
                        limit_source = excluded.limit_source,
                        window_type = excluded.window_type,
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_name,
                        model_id,
                        1 if requires_credits else 0,
                        daily_request_limit,
                        daily_token_limit,
                        sp,
                        limit_source,
                        window_type,
                        1 if enabled else 0,
                        now_iso,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def seed_free_model(self, model_id: str) -> None:
        """幂等种入免费模型的全局规则：只在行不存在时写入，不覆盖管理员已有配置。"""
        now_iso = _iso_now(self.tz_name, self._clock())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO model_quota_limits (
                        account_name, model_id, requires_credits,
                        daily_request_limit, daily_token_limit, safe_percent,
                        limit_source, window_type, enabled, updated_at
                    ) VALUES (?, ?, 0, NULL, NULL, ?, 'seed_free_model', 'daily', 1, ?)
                    """,
                    (WILDCARD_ACCOUNT, model_id, self.default_safe_percent, now_iso),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_effective_rule(self, account_name: str, model_id: str) -> QuotaRule:
        """读取生效规则：单账号特例 > '*' 全局模型默认 > 系统默认（限额未知为 None）。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM model_quota_limits
                WHERE model_id = ? AND account_name IN (?, ?)
                """,
                (model_id, account_name, WILDCARD_ACCOUNT),
            ).fetchall()

        by_acc = {r["account_name"]: r for r in rows}
        row = by_acc.get(account_name) or by_acc.get(WILDCARD_ACCOUNT)
        if row is None:
            return QuotaRule(
                model_id=model_id,
                account_name=account_name,
                requires_credits=True,
                daily_request_limit=None,
                daily_token_limit=None,
                safe_percent=self.default_safe_percent,
            )

        return QuotaRule(
            model_id=model_id,
            account_name=row["account_name"],
            requires_credits=bool(row["requires_credits"]),
            daily_request_limit=row["daily_request_limit"],
            daily_token_limit=row["daily_token_limit"],
            safe_percent=float(row["safe_percent"]),
            observed_limit=row["observed_limit"],
            limit_source=row["limit_source"],
            window_type=_effective_window_type(
                account_name, model_id, row["window_type"]
            ),
            enabled=bool(row["enabled"]),
        )

    # ------------------------------------------------------------------
    # 事前预检、并发预占与自动切号
    # ------------------------------------------------------------------

    def _get_usage_and_state(
        self, conn: sqlite3.Connection, account_name: str, model_id: str, window_key: str
    ) -> Tuple[int, int, Optional[sqlite3.Row]]:
        u = conn.execute(
            """
            SELECT success_count, prompt_tokens, completion_tokens
            FROM model_quota_usage
            WHERE account_name = ? AND model_id = ? AND window_key = ?
            """,
            (account_name, model_id, window_key),
        ).fetchone()
        success_count = int(u["success_count"]) if u else 0
        total_tokens = int(u["prompt_tokens"]) + int(u["completion_tokens"]) if u else 0

        s = conn.execute(
            """
            SELECT hard_exhausted_window, cooldown_until, reset_at
            FROM model_quota_state
            WHERE account_name = ? AND model_id = ?
            """,
            (account_name, model_id),
        ).fetchone()
        return success_count, total_tokens, s

    def try_reserve(
        self,
        account: AccountCandidate,
        model_id: str,
        *,
        switched_from: Optional[str] = None,
    ) -> Tuple[Optional[Reservation], Optional[str]]:
        """
        原子执行事前额度预检并预占 1 个并发名额。
        返回 (Reservation, None) 表示可用；返回 (None, reason) 表示应跳过该账号。
        """
        if not account.enabled:
            return None, "account_disabled"

        now_ts = self._clock()
        window_key = _daily_window_key(self.tz_name, now_ts)
        rule = self.get_effective_rule(account.name, model_id)

        # 1. 付费模型检查积分；免费模型 (requires_credits=False) 直接跳过积分检查
        if rule.requires_credits and account.credits is not None and account.credits <= 0:
            return None, "credits_exhausted"

        with self._lock:
            with closing(self._connect()) as conn:
                success_count, total_tokens, state = self._get_usage_and_state(
                    conn, account.name, model_id, window_key
                )

            # 2. 检查硬耗尽状态（跨窗口或过 reset_at 后自动解除）
            if state is not None:
                hard_win = state["hard_exhausted_window"]
                reset_at = state["reset_at"]
                cooldown_until = state["cooldown_until"]

                if hard_win:
                    if reset_at is not None:
                        if now_ts < float(reset_at):
                            return None, "daily_exhausted"
                    elif hard_win == window_key:
                        return None, "daily_exhausted"

                if cooldown_until is not None and now_ts < float(cooldown_until):
                    return None, "rate_limit_cooldown"

            # 3. 事前 95% 安全阈值预检（含内存并发预占 in_flight）
            if rule.enabled:
                inflight_key = (account.name, model_id, window_key)
                in_flight = self._in_flight.get(inflight_key, 0)

                safe_req = compute_safe_limit(rule.daily_request_limit, rule.safe_percent)
                if safe_req is not None and (success_count + in_flight) >= safe_req:
                    return None, "request_threshold_reached"

                safe_tok = compute_safe_limit(rule.daily_token_limit, rule.safe_percent)
                if safe_tok is not None and total_tokens >= safe_tok:
                    return None, "token_threshold_reached"

                # 预检通过：原子递增内存并发预占
                self._in_flight[inflight_key] = in_flight + 1
            else:
                inflight_key = (account.name, model_id, window_key)
                self._in_flight[inflight_key] = self._in_flight.get(inflight_key, 0) + 1

            return (
                Reservation(
                    account_name=account.name,
                    model_id=model_id,
                    window_key=window_key,
                    reserved_at=now_ts,
                    switched_from=switched_from,
                ),
                None,
            )

    def reserve_for_observe(
        self,
        account: AccountCandidate,
        model_id: str,
        reason: str,
        *,
        switched_from: Optional[str] = None,
    ) -> Reservation:
        """observe 专用：预检未通过也不拦截，仍预占以照常记账，并把 would_skip 落为事件。"""
        now_ts = self._clock()
        window_key = _daily_window_key(self.tz_name, now_ts)
        with self._lock:
            key = (account.name, model_id, window_key)
            self._in_flight[key] = self._in_flight.get(key, 0) + 1
        try:
            self.record_event(account.name, model_id, "would_skip", error_code=reason)
        except Exception:
            _LOG.warning("quota_engine: would_skip 事件落盘失败(%s/%s): %s",
                         account.name, model_id, exc_info=True)
        return Reservation(
            account_name=account.name,
            model_id=model_id,
            window_key=window_key,
            reserved_at=now_ts,
            switched_from=switched_from,
        )

    def select_and_reserve(
        self,
        accounts: Sequence[AccountCandidate],
        model_id: str,
        *,
        pinned_account: Optional[str] = None,
        sticky_account: Optional[str] = None,
        exclude_accounts: Optional[Iterable[str]] = None,
    ) -> SelectionResult:
        """
        按 `置顶 > 粘性会话 > priority 升序` 依次进行事前额度预检并预占第一个满足条件的账号。
        严格模式：所有账号均未通过 95% 阈值或均不可用时，直接返回 reservation=None（网关返回 503）。
        """
        excluded = set(exclude_accounts or ())
        by_name = {a.name: a for a in accounts if a.name not in excluded}

        ordered: List[AccountCandidate] = []
        seen = set()

        for preferred in (pinned_account, sticky_account):
            if preferred and preferred in by_name and preferred not in seen:
                ordered.append(by_name[preferred])
                seen.add(preferred)

        for acc in sorted(by_name.values(), key=lambda x: (x.priority, x.name)):
            if acc.name not in seen:
                ordered.append(acc)
                seen.add(acc.name)

        skip_reasons: Dict[str, str] = {}
        for acc in ordered:
            switched_from = (
                sticky_account
                if sticky_account and acc.name != sticky_account and sticky_account in skip_reasons
                else None
            )
            reservation, reason = self.try_reserve(acc, model_id, switched_from=switched_from)
            if reservation is not None:
                if switched_from:
                    self.record_event(
                        acc.name,
                        model_id,
                        "account_switched",
                        error_code=f"from:{switched_from}:{skip_reasons.get(switched_from, '')}",
                    )
                return SelectionResult(
                    reservation=reservation,
                    skip_reasons=skip_reasons,
                    all_exhausted=False,
                )
            skip_reasons[acc.name] = reason or "unavailable"

        return SelectionResult(
            reservation=None,
            skip_reasons=skip_reasons,
            all_exhausted=True,
        )

    def _release_inflight(self, reservation: Reservation) -> None:
        key = (reservation.account_name, reservation.model_id, reservation.window_key)
        with self._lock:
            cur = self._in_flight.get(key, 0)
            if cur <= 1:
                self._in_flight.pop(key, None)
            else:
                self._in_flight[key] = cur - 1

    def commit(
        self,
        reservation: Reservation,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """请求成功后原子落盘成功计数与 token 消耗，再释放内存预占（杜绝并发穿透窗口）。"""
        now_iso = _iso_now(self.tz_name, self._clock())
        key = (reservation.account_name, reservation.model_id, reservation.window_key)
        with self._lock:
            try:
                with closing(self._connect()) as conn:
                    conn.execute("BEGIN IMMEDIATE;")
                    try:
                        conn.execute(
                            """
                            INSERT INTO model_quota_usage (
                                account_name, model_id, window_key, window_start,
                                request_count, success_count, failed_count,
                                prompt_tokens, completion_tokens, updated_at
                            ) VALUES (?, ?, ?, ?, 1, 1, 0, ?, ?, ?)
                            ON CONFLICT(account_name, model_id, window_key) DO UPDATE SET
                                request_count = request_count + 1,
                                success_count = success_count + 1,
                                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                                completion_tokens = completion_tokens + excluded.completion_tokens,
                                updated_at = excluded.updated_at
                            """,
                            (
                                reservation.account_name,
                                reservation.model_id,
                                reservation.window_key,
                                now_iso,
                                max(0, int(prompt_tokens)),
                                max(0, int(completion_tokens)),
                                now_iso,
                            ),
                        )
                        conn.execute("COMMIT;")
                    except Exception:
                        conn.execute("ROLLBACK;")
                        raise
            finally:
                cur = self._in_flight.get(key, 0)
                if cur <= 1:
                    self._in_flight.pop(key, None)
                else:
                    self._in_flight[key] = cur - 1

    def release(self, reservation: Reservation, *, count_failure: bool = True) -> None:
        """请求失败/取消时释放内存预占，不增加 success_count。"""
        self._release_inflight(reservation)
        if not count_failure:
            return
        now_iso = _iso_now(self.tz_name, self._clock())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO model_quota_usage (
                        account_name, model_id, window_key, window_start,
                        request_count, success_count, failed_count,
                        prompt_tokens, completion_tokens, updated_at
                    ) VALUES (?, ?, ?, ?, 1, 0, 1, 0, 0, ?)
                    ON CONFLICT(account_name, model_id, window_key) DO UPDATE SET
                        request_count = request_count + 1,
                        failed_count = failed_count + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        reservation.account_name,
                        reservation.model_id,
                        reservation.window_key,
                        now_iso,
                        now_iso,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    # ------------------------------------------------------------------
    # 上游限流兜底与事件观测
    # ------------------------------------------------------------------

    def record_upstream_limit(
        self,
        account_name: str,
        model_id: str,
        *,
        http_status: int,
        is_daily_hard_limit: bool,
        headers: Optional[Dict[str, str]] = None,
        body_text: str = "",
        error_code: Optional[str] = None,
    ) -> None:
        """
        兜底处理上游真实返回的 429/限额错误：
        - is_daily_hard_limit=True：标记当日该模型硬耗尽（不影响同账号其他模型）。
        - is_daily_hard_limit=False：按 Retry-After 或默认软冷却时间进入短时冷却。
        - 完整记录 headers 与截断后的 body，方便后续分析真实限额与重置字段。
        """
        now_ts = self._clock()
        now_iso = _iso_now(self.tz_name, now_ts)
        window_key = _daily_window_key(self.tz_name, now_ts)
        reset_ts = parse_reset_timestamp(headers, now_ts=now_ts)
        retry_after = parse_retry_after(
            (headers or {}).get("Retry-After") or (headers or {}).get("retry-after"),
            now_ts=now_ts,
        )

        hard_win = window_key if is_daily_hard_limit else None
        hard_reset_ts = reset_ts if is_daily_hard_limit else None
        cooldown_until: Optional[float] = None
        if not is_daily_hard_limit:
            wait_sec = retry_after if retry_after is not None else self.default_soft_cooldown
            cooldown_until = now_ts + max(1, wait_sec)

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO model_quota_state (
                        account_name, model_id, hard_exhausted_window,
                        cooldown_until, reset_at, last_error_code, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_name, model_id) DO UPDATE SET
                        hard_exhausted_window = CASE
                            WHEN excluded.hard_exhausted_window IS NOT NULL THEN excluded.hard_exhausted_window
                            ELSE hard_exhausted_window
                        END,
                        cooldown_until = CASE
                            WHEN excluded.cooldown_until IS NOT NULL THEN excluded.cooldown_until
                            ELSE cooldown_until
                        END,
                        reset_at = CASE
                            WHEN excluded.hard_exhausted_window IS NOT NULL THEN excluded.reset_at
                            ELSE reset_at
                        END,
                        last_error_code = excluded.last_error_code,
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_name,
                        model_id,
                        hard_win,
                        cooldown_until,
                        hard_reset_ts,
                        error_code or str(http_status),
                        now_iso,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

        self.record_event(
            account_name,
            model_id,
            "daily_exhausted" if is_daily_hard_limit else "rate_limited",
            http_status=http_status,
            error_code=error_code,
            retry_after=retry_after,
            reset_at=_iso_now(self.tz_name, reset_ts) if reset_ts else None,
            raw_headers=headers,
            raw_body=body_text[:2000] if body_text else None,
        )

    def clear_model_state(self, account_name: str, model_id: str) -> None:
        """管理员手动解除指定账号×模型的冷却/耗尽状态。"""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    "DELETE FROM model_quota_state WHERE account_name = ? AND model_id = ?",
                    (account_name, model_id),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def record_event(
        self,
        account_name: str,
        model_id: str,
        event_type: str,
        *,
        http_status: Optional[int] = None,
        error_code: Optional[str] = None,
        retry_after: Optional[int] = None,
        reset_at: Optional[str] = None,
        raw_headers: Optional[Dict[str, str]] = None,
        raw_body: Optional[str] = None,
    ) -> None:
        now_iso = _iso_now(self.tz_name, self._clock())
        headers_json = json.dumps(raw_headers, ensure_ascii=False) if raw_headers else None
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO model_quota_events (
                        account_name, model_id, event_type, http_status,
                        error_code, retry_after, reset_at, raw_headers, raw_body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_name,
                        model_id,
                        event_type,
                        http_status,
                        error_code,
                        retry_after,
                        reset_at,
                        headers_json,
                        raw_body,
                        now_iso,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def get_snapshot(self, accounts: Sequence[AccountCandidate], models: Sequence[str]) -> List[Dict[str, Any]]:
        """供 wb-manager 面板展示的实时「账号 × 模型」额度状态快照。"""
        now_ts = self._clock()
        window_key = _daily_window_key(self.tz_name, now_ts)
        result: List[Dict[str, Any]] = []

        with self._lock:
            inflight_copy = dict(self._in_flight)

        with closing(self._connect()) as conn:
            for acc in accounts:
                for model_id in models:
                    rule = self.get_effective_rule(acc.name, model_id)
                    success_count, total_tokens, state = self._get_usage_and_state(
                        conn, acc.name, model_id, window_key
                    )
                    in_flight = inflight_copy.get((acc.name, model_id, window_key), 0)
                    safe_req = compute_safe_limit(rule.daily_request_limit, rule.safe_percent)
                    safe_tok = compute_safe_limit(rule.daily_token_limit, rule.safe_percent)

                    status = "ok"
                    if not acc.enabled:
                        status = "disabled"
                    elif rule.requires_credits and acc.credits is not None and acc.credits <= 0:
                        status = "credits_exhausted"
                    elif state and state["hard_exhausted_window"] == window_key and (
                        state["reset_at"] is None or now_ts < float(state["reset_at"])
                    ):
                        status = "exhausted"
                    elif state and state["cooldown_until"] is not None and now_ts < float(state["cooldown_until"]):
                        status = "cooldown"
                    elif safe_req is not None and (success_count + in_flight) >= safe_req:
                        status = "threshold_reached"
                    elif safe_tok is not None and total_tokens >= safe_tok:
                        status = "threshold_reached"
                    elif rule.daily_request_limit is None and rule.daily_token_limit is None:
                        status = "unknown_limit"

                    result.append(
                        {
                            "account_name": acc.name,
                            "model_id": model_id,
                            "window_key": window_key,
                            "requires_credits": rule.requires_credits,
                            "daily_request_limit": rule.daily_request_limit,
                            "safe_request_limit": safe_req,
                            "success_count": success_count,
                            "in_flight": in_flight,
                            "daily_token_limit": rule.daily_token_limit,
                            "safe_token_limit": safe_tok,
                            "tokens_used": total_tokens,
                            "safe_percent": rule.safe_percent,
                            "limit_source": rule.limit_source,
                            "status": status,
                        }
                    )
        return result