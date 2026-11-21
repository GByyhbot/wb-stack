# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号管理平台 - 数据库层
SQLite 存储，三张表：accounts(账号)、checkin_logs(签到日志)、settings(全局配置)
"""
import sqlite3
import threading
import json
import os
import time

DB_PATH = os.environ.get("WB_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "wb_manager.db"))

_local = threading.local()
_db_lock = threading.Lock()


def get_db():
    """线程内复用连接，SQLite 写操作用全局锁保护"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


def init_db():
    """建表（幂等）"""
    with _db_lock:
        db = get_db()
        db.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,                  -- 备注名，如 "主号"
            user_id TEXT NOT NULL,               -- WorkBuddy 账号标识（邮箱/手机号）
            token TEXT NOT NULL,                 -- 登录令牌 / Cookie
            priority INTEGER NOT NULL DEFAULT 100,  -- 数字越小优先级越高，1=主账号
            quota_total REAL NOT NULL DEFAULT 0,    -- 总额度（积分/次数），0=未知
            quota_used REAL NOT NULL DEFAULT 0,     -- 已用额度
            status TEXT NOT NULL DEFAULT 'active',  -- active / disabled / error
            last_checkin_at TEXT,                -- 最近一次签到时间
            last_checkin_result TEXT,            -- 最近一次签到结果
            checkin_streak INTEGER NOT NULL DEFAULT 0,  -- 连续签到天数
            quota_checked_at TEXT,               -- 最近一次额度查询时间
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS checkin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            ok INTEGER NOT NULL,                 -- 1=成功 0=失败
            message TEXT,                        -- 结果详情
            reward TEXT,                         -- 签到奖励（如 +50 积分）
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        INSERT OR IGNORE INTO settings (key, value) VALUES ('checkin_time', '08:30');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('quota_min_percent', '10');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('switch_enabled', '1');
        """)
        # 轻量迁移：老库补列（幂等）
        cols = [r[1] for r in db.execute("PRAGMA table_info(accounts)")]
        if "quota_expire_at" not in cols:
            # quota_expire_at = 积分最早到期时间
            db.execute("ALTER TABLE accounts ADD COLUMN quota_expire_at TEXT")
        if "note" not in cols:
            # note = 用户自定义备注（面板可编辑）
            db.execute("ALTER TABLE accounts ADD COLUMN note TEXT")
        db.commit()


def row_to_dict(row):
    return dict(row) if row is not None else None


# ---------- 账号 CRUD ----------

def add_account(name, user_id, token, priority=100, quota_total=0, quota_used=0):
    db = get_db()
    with _db_lock:
        cur = db.execute(
            "INSERT INTO accounts (name, user_id, token, priority, quota_total, quota_used) VALUES (?,?,?,?,?,?)",
            (name, user_id, token, priority, quota_total, quota_used),
        )
        db.commit()
        return cur.lastrowid


def list_accounts(include_disabled=True):
    db = get_db()
    sql = "SELECT * FROM accounts"
    if not include_disabled:
        sql += " WHERE status='active'"
    sql += " ORDER BY priority ASC, id ASC"
    return [row_to_dict(r) for r in db.execute(sql).fetchall()]


def get_account(account_id):
    db = get_db()
    return row_to_dict(db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone())


def update_account(account_id, **fields):
    """允许更新的字段白名单"""
    allowed = {"name", "user_id", "token", "priority", "quota_total", "quota_used",
               "status", "last_checkin_at", "last_checkin_result", "checkin_streak",
               "quota_checked_at", "quota_expire_at", "note"}
    keys = [k for k in fields if k in allowed]
    if not keys:
        return
    db = get_db()
    with _db_lock:
        sql = "UPDATE accounts SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE id=?"
        db.execute(sql, [fields[k] for k in keys] + [account_id])
        db.commit()


def delete_account(account_id):
    db = get_db()
    with _db_lock:
        db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        db.execute("DELETE FROM checkin_logs WHERE account_id=?", (account_id,))
        db.commit()


# ---------- 签到日志 ----------

def add_checkin_log(account_id, ok, message="", reward=""):
    db = get_db()
    with _db_lock:
        db.execute(
            "INSERT INTO checkin_logs (account_id, ok, message, reward) VALUES (?,?,?,?)",
            (account_id, 1 if ok else 0, message, reward),
        )
        db.commit()


def recent_logs(limit=100, account_id=None):
    db = get_db()
    sql = "SELECT l.*, a.name AS account_name FROM checkin_logs l LEFT JOIN accounts a ON a.id=l.account_id"
    params = []
    if account_id:
        sql += " WHERE l.account_id=?"
        params.append(account_id)
    sql += " ORDER BY l.id DESC LIMIT ?"
    params.append(limit)
    return [row_to_dict(r) for r in db.execute(sql, params).fetchall()]


# ---------- 设置 ----------

def get_settings():
    db = get_db()
    return {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings").fetchall()}


def set_setting(key, value):
    db = get_db()
    with _db_lock:
        db.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))
        db.commit()


# ---------- 额度切换 ----------

def account_quota_percent(acc):
    """
    剩余额度百分比。
    2026-11-06 起 quota_total 存【本周期总发放】、quota_used 存【已用】，
    可用积分 = quota_total - quota_used（get-user-resource-summary 真实值）。
    未查询过额度返回 None；总发放为 0 视为 0%。
    """
    total = float(acc.get("quota_total") or 0)
    if total <= 0:
        return 0.0 if acc.get("quota_checked_at") else None
    used = float(acc.get("quota_used") or 0)
    return max(0.0, (total - used) / total * 100)


def account_available_credits(acc):
    """可用积分 = 总发放 - 已用；未查询过额度返回 None"""
    if not acc.get("quota_checked_at"):
        return None
    return max(0.0, float(acc.get("quota_total") or 0) - float(acc.get("quota_used") or 0))


def pick_active_account(exclude_ids=None):
    """
    挑选当前应使用的账号：
    1. 只看 status=active 的账号
    2. 排除可用积分 <= 0 的账号（已查询过 => 耗尽）
    3. 未查询过额度的账号（quota_checked_at 为空）不拦截
    4. 按 priority 升序取第一个（priority=1 是主账号）
    返回 dict 或 None

    B6: 设置 switch_enabled='0'（自动切换关闭）时跳过第 2 步——只认优先级
    第一的账号，耗尽也不顺延。该设置此前只存不用，关了也不生效。
    """
    exclude_ids = set(exclude_ids or [])
    accounts = list_accounts(include_disabled=False)
    try:
        switch_on = get_settings().get("switch_enabled", "1") != "0"
    except Exception:
        switch_on = True
    if not switch_on:
        for acc in accounts:
            if acc["id"] not in exclude_ids:
                return acc
        return None
    for acc in accounts:
        if acc["id"] in exclude_ids:
            continue
        # 已查询过额度且可用积分 <= 0 => 耗尽，跳过
        avail = account_available_credits(acc)
        if avail is not None and avail <= 0:
            continue
        return acc
    return None


def mark_quota_consumed(account_id, amount):
    """业务调用后扣减额度（本地记账）"""
    acc = get_account(account_id)
    if not acc:
        return
    update_account(account_id, quota_used=float(acc.get("quota_used") or 0) + amount)
