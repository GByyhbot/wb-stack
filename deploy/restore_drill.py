# -*- coding: utf-8 -*-
"""恢复演练（批次4 项4 硬性交付）：在临时目录用一份真实备份恢复，
恢复后做数据完整校验（表行数/账号数与备份 manifest 对账）。

用法：
  python restore_drill.py <备份目录> [--workdir <临时目录>]

流程：
1. 读取备份目录的 manifest.json（行数与 sha1 清单）
2. 把备份的 wb_manager.db 拷贝到临时目录（模拟恢复落盘）
3. 用 sqlite3 打开恢复后的库，核对三张表行数与 manifest 一致
4. 核对恢复后 accounts.json 账号数与 manifest 一致
5. 打印对账结果，全对则退出码 0，否则 1

只读备份、只写临时目录，不触碰现网数据。
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile

TABLES = ("accounts", "checkin_logs", "settings")


def _table_counts(conn):
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in TABLES}


def run_drill(backup_dir, workdir=None):
    """执行恢复演练，返回对账结果 dict。"""
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise RuntimeError("备份目录缺少 manifest.json: %s" % backup_dir)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    workdir = workdir or tempfile.mkdtemp(prefix="wb-restore-")
    os.makedirs(workdir, exist_ok=True)

    # 1. 恢复 DB：从备份拷贝到临时目录
    db_src = os.path.join(backup_dir, "wb_manager.db")
    db_dst = os.path.join(workdir, "wb_manager.db")
    shutil.copy2(db_src, db_dst)

    # 2. 恢复 rotator 数据文件（若存在）
    restored_files = [os.path.basename(db_dst)]
    for name in ("accounts.json", "pin.json", "session_state.json",
                 "circuit_state.json"):
        src = os.path.join(backup_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(workdir, name))
            restored_files.append(name)

    # 3. 数据完整校验：表行数对账
    conn = sqlite3.connect(db_dst)
    try:
        actual_rows = _table_counts(conn)
    finally:
        conn.close()
    expected_rows = manifest.get("db", {}).get("rows", {})

    rows_ok = actual_rows == expected_rows

    # 4. 账号数对账
    accounts_count = None
    acc_file = os.path.join(workdir, "accounts.json")
    if os.path.isfile(acc_file):
        with open(acc_file, encoding="utf-8") as f:
            pool = json.load(f) or {}
        accounts_count = len(pool.get("accounts") or []) if isinstance(pool, dict) else len(pool)
    expected_acc = manifest.get("accounts_count")
    accounts_ok = (accounts_count == expected_acc) if expected_acc is not None else True

    return {
        "backup_dir": backup_dir,
        "workdir": workdir,
        "restored_files": restored_files,
        "expected_rows": expected_rows,
        "actual_rows": actual_rows,
        "rows_ok": rows_ok,
        "expected_accounts": expected_acc,
        "actual_accounts": accounts_count,
        "accounts_ok": accounts_ok,
        "passed": rows_ok and accounts_ok,
    }


def main():
    ap = argparse.ArgumentParser(description="wb-stack 恢复演练")
    ap.add_argument("backup_dir", help="备份目录（含 manifest.json）")
    ap.add_argument("--workdir", default=None, help="恢复落盘的临时目录（默认自动创建）")
    args = ap.parse_args()

    try:
        result = run_drill(args.backup_dir, args.workdir)
    except Exception as e:
        print("❌ 恢复演练失败: %s" % e)
        return 1

    print("=== 恢复演练对账 ===")
    print("备份目录 : %s" % result["backup_dir"])
    print("恢复目录 : %s" % result["workdir"])
    print("恢复文件 : %s" % ", ".join(result["restored_files"]))
    print("表行数   : 预期 %s | 实际 %s -> %s"
          % (result["expected_rows"], result["actual_rows"],
             "一致" if result["rows_ok"] else "不一致"))
    print("账号数   : 预期 %s | 实际 %s -> %s"
          % (result["expected_accounts"], result["actual_accounts"],
             "一致" if result["accounts_ok"] else "不一致"))
    if result["passed"]:
        print("✅ 恢复演练通过：数据完整，行数/账号数对账一致")
        return 0
    print("❌ 恢复演练未通过：对账不一致")
    return 1


if __name__ == "__main__":
    sys.exit(main())
