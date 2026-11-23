# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号管理平台 - 备份模块（批次 4 项4）

备份内容与方式：
1. SQLite：走 sqlite3 backup API 在线快照（WAL 热库安全，不用裸 cp）
2. 网关数据文件（accounts.json / pin.json / session_state.json /
   circuit_state.json）：原子拷贝（tmp + os.replace，避免读到半截文件）
3. 关键日志滚动归档：wb-manager.log（含 RotatingFileHandler 滚动件）与
   rotator 的 requests-*.jsonl，拷入备份目录 logs/ 子目录

产物落盘：<BACKUP_DIR>/wb-backup-YYYYMMDD-HHMMSS/，内含 db 快照、
rotator 数据、logs/、manifest.json（行数与 sha1 清单，供恢复演练对账）。

保留策略：默认保留最近 14 份 且 不超过 30 天，两者取先到者删除
（WB_BACKUP_KEEP_COUNT / WB_BACKUP_KEEP_DAYS 可调）。

定时执行：scheduler 每日 03:30 挂 run_backup（避开签到 08:30 与整点刷新）。
"""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime

from . import db
from . import gateway_sync

log = logging.getLogger("wb-manager.backup")

# 备份根目录：默认在数据库上级目录（容器内 /data/backup），可用环境变量指定
BACKUP_DIR = os.environ.get("WB_BACKUP_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(db.DB_PATH)), "backup")

# 保留策略（份数 / 天数，取先到者）
KEEP_COUNT = int(os.environ.get("WB_BACKUP_KEEP_COUNT", "14"))
KEEP_DAYS = int(os.environ.get("WB_BACKUP_KEEP_DAYS", "30"))

# 需要原子拷贝的 rotator 数据文件（存在才拷）
ROTATOR_DATA_FILES = ("accounts.json", "pin.json", "session_state.json",
                      "circuit_state.json")

# manager 日志滚动件后缀（RotatingFileHandler: wb-manager.log.1/.2/.3）
_LOG_ROLL_SUFFIXES = ("", ".1", ".2", ".3", ".4", ".5")


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_copy(src, dst):
    """原子拷贝：先写 tmp 再 replace，目标目录需已存在"""
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def _table_counts(conn):
    """三张表行数快照，供 manifest 与恢复演练对账"""
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in ("accounts", "checkin_logs", "settings")}


def _sqlite_snapshot(src_path, dst_path):
    """sqlite3 backup API 在线快照：WAL 热库安全，不锁写"""
    src = sqlite3.connect(src_path, timeout=15)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _rotator_home():
    return gateway_sync.rotator_home()


def _archive_logs(backup_dir, db_path):
    """归档关键日志：manager 日志滚动件 + rotator 请求日志"""
    logs_dir = os.path.join(backup_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    archived = []
    # manager 日志：与 DB 同目录的 wb-manager.log 及其滚动件
    log_base = os.path.join(os.path.dirname(db_path), "wb-manager.log")
    for suffix in _LOG_ROLL_SUFFIXES:
        p = log_base + suffix
        if os.path.isfile(p):
            dst = os.path.join(logs_dir, os.path.basename(p))
            _atomic_copy(p, dst)
            archived.append(os.path.basename(p))
    # rotator 请求日志：logs/requests-*.jsonl
    rot_logs = os.path.join(str(_rotator_home()), "logs")
    if os.path.isdir(rot_logs):
        for name in sorted(os.listdir(rot_logs)):
            if name.startswith("requests-") and name.endswith(".jsonl"):
                src = os.path.join(rot_logs, name)
                dst = os.path.join(logs_dir, name)
                _atomic_copy(src, dst)
                archived.append(name)
    return archived


def run_backup(trigger_source="manual"):
    """执行一次完整备份，返回摘要 dict（供调度器日志、测试与回执）。

    健壮性口径与签到/额度刷新一致：单步失败记录后继续，最终摘要里
    标记 ok=False，不抛异常打断调度器。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_DIR, "wb-backup-" + stamp)
    summary = {"ok": False, "backup_dir": backup_dir, "created_at": stamp,
               "files": [], "errors": []}
    try:
        os.makedirs(backup_dir, exist_ok=True)

        # 1. SQLite 在线快照
        db_dst = os.path.join(backup_dir, "wb_manager.db")
        _sqlite_snapshot(db.DB_PATH, db_dst)
        conn = sqlite3.connect(db_dst)
        try:
            rows = _table_counts(conn)
        finally:
            conn.close()
        summary["db_rows"] = rows

        # 2. rotator 数据文件原子拷贝
        rot_files = {}
        rot_home = _rotator_home()
        for name in ROTATOR_DATA_FILES:
            src = os.path.join(str(rot_home), name)
            if os.path.isfile(src):
                dst = os.path.join(backup_dir, name)
                _atomic_copy(src, dst)
                rot_files[name] = _sha1(dst)
        accounts_count = None
        if os.path.isfile(os.path.join(backup_dir, "accounts.json")):
            with open(os.path.join(backup_dir, "accounts.json"), encoding="utf-8") as f:
                pool = json.load(f) or {}
            accounts_count = len(pool.get("accounts") or []) if isinstance(pool, dict) else len(pool)
        summary["rotator_files"] = rot_files
        summary["accounts_count"] = accounts_count

        # 3. 日志滚动归档
        summary["logs"] = _archive_logs(backup_dir, db.DB_PATH)

        # 4. manifest：行数 + sha1 清单，恢复演练据此对账
        manifest = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger_source,
            "db": {"file": "wb_manager.db", "sha1": _sha1(db_dst), "rows": rows},
            "rotator_files": rot_files,
            "accounts_count": accounts_count,
            "logs": summary["logs"],
        }
        with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        summary["files"] = sorted(os.listdir(backup_dir))
        summary["ok"] = True
        log.info("[%s] 备份完成: %s 行数=%s 账号=%s", trigger_source, backup_dir,
                 rows, accounts_count)
    except Exception as e:
        log.exception("[%s] 备份失败: %s", trigger_source, backup_dir)
        summary["errors"].append(str(e)[:200])
    # 5. 保留策略清理（无论本次成败都执行，避免旧备份堆积）
    summary["pruned"] = prune_backups()
    return summary


def prune_backups():
    """保留策略：份数与天数取先到者，删除过期备份目录。返回删除列表。"""
    pruned = []
    try:
        if not os.path.isdir(BACKUP_DIR):
            return pruned
        names = [n for n in os.listdir(BACKUP_DIR)
                 if n.startswith("wb-backup-") and
                 os.path.isdir(os.path.join(BACKUP_DIR, n))]
        # 目录名内嵌时间戳，字典序即时间序
        names.sort()
        cutoff_ts = time.time() - KEEP_DAYS * 86400
        for name in names:
            path = os.path.join(BACKUP_DIR, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            # 超期（天数）或超出保留份数（排序靠前的旧份）
            if st.st_mtime < cutoff_ts or len(names) - names.index(name) > KEEP_COUNT:
                shutil.rmtree(path, ignore_errors=True)
                pruned.append(name)
        if pruned:
            log.info("备份保留策略清理: %s（保留 %s 份 / %s 天）",
                     pruned, KEEP_COUNT, KEEP_DAYS)
    except Exception:
        log.exception("备份清理失败")
    return pruned
