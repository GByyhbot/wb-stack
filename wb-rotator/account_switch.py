# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号快照切换工具（借鉴 Buddy Switch 的登录态备份/恢复思路）

原理：客户端登录态整体保存在 workbuddy-desktop.info（含加密信封，仅本机可解）。
切换账号 = 用目标账号登录时保存的整份快照覆盖该文件，无需解密/重加密。

子命令：
  snapshot --name 备用X   保存当前登录账号的快照（含 rt 导出），取名后长期复用
  list                    列出所有快照与当前登录的账号
  switch  --name 备用X    恢复指定快照（需先完全退出客户端，会自动检查）
  switch  --name X --force客户端运行中仍强制执行（危险，不推荐）
  launch                  启动 WorkBuddy 客户端
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

# 路径可用环境变量覆盖（WB_AUTH_FILE / WB_CLIENT_EXE / WB_RT_AUTH），换机器免改代码
AUTH_FILE = os.environ.get(
    "WB_AUTH_FILE",
    r"C:\Users\Administrator\AppData\Local\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info")
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
META_FILE = os.path.join(SNAP_DIR, "meta.json")
CLIENT_EXE = os.environ.get("WB_CLIENT_EXE", r"D:\workbuddy\WorkBuddy.exe")
RT_AUTH = os.environ.get(
    "WB_RT_AUTH",
    r"C:\Users\Administrator\.workbuddy\skills"
    r"\totorosir-workbuddy-checkin__skillhub\scripts\rt_auth.py")


def load_meta():
    if os.path.isfile(META_FILE):
        return json.load(open(META_FILE, encoding="utf-8"))
    return {}


def save_meta(meta):
    os.makedirs(SNAP_DIR, exist_ok=True)
    json.dump(meta, open(META_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def current_uid():
    """当前客户端登录的账号 uid"""
    d = json.load(open(AUTH_FILE, encoding="utf-8"))
    return d.get("account", {}).get("uid", "?"), d


def client_running():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WorkBuddy.exe"],
                       capture_output=True, text=True,
                       encoding="gbk", errors="replace")
    return "WorkBuddy.exe" in (r.stdout or "")


def cmd_snapshot(args):
    if client_running() and os.name == "nt":
        # 客户端运行中也能读文件（快照），只读操作
        pass
    uid, doc = current_uid()
    nick = doc.get("account", {}).get("nickname", "")
    # nickname 是加密信封则展示 uid 前缀
    if isinstance(nick, dict):
        nick = ""
    name = args.name
    os.makedirs(SNAP_DIR, exist_ok=True)
    dst = os.path.join(SNAP_DIR, uid + ".info")
    shutil.copy2(AUTH_FILE, dst)
    meta = load_meta()
    meta[uid] = {"name": name, "snapshot": dst, "saved_at": time.strftime("%F %T")}
    save_meta(meta)

    # 同时导出 rt（供代理池/签到平台使用）
    print("---- 导出长效令牌 ----")
    subprocess.run([sys.executable, RT_AUTH, "--export-rt", "--save"])
    print("---- 快照完成 ----")
    print("账号: %s (uid %s...)" % (name, uid[:8]))
    print("快照: %s" % dst)
    print("当前客户端仍登录在该账号，可正常使用或按需切换。")


def cmd_list(_args):
    uid, _ = current_uid()
    meta = load_meta()
    print("当前登录账号 uid: %s" % uid)
    print("---- 快照列表 ----")
    if not meta:
        print("（暂无快照，用 snapshot --name 名字 保存）")
    for u, info in sorted(meta.items(), key=lambda kv: kv[1].get("name", "")):
        cur = " <== 当前" if u == uid else ""
        print("[%s] %s  保存于 %s%s" % (u[:8], info.get("name", "?"),
                                       info.get("saved_at", "?"), cur))


def cmd_switch(args):
    uid_now, _ = current_uid()
    meta = load_meta()
    target = next((u for u, i in meta.items() if i.get("name") == args.name), None)
    if not target:
        print("❌ 未找到名为 [%s] 的快照。用 list 查看已有快照。" % args.name)
        sys.exit(1)
    if target == uid_now:
        print("ℹ️ 当前已登录该账号，无需切换。")
        return
    if client_running() and not args.force:
        print("❌ WorkBuddy 客户端正在运行。请先完全退出客户端（托盘也退出），")
        print("   或确认知晓风险后加 --force 强制执行（运行中写入会被内存态覆盖，切号大概率无效）。")
        sys.exit(2)
    # 写前备份当前登录态（失败可回滚）
    backup = os.path.join(SNAP_DIR, "_before_switch.info")
    shutil.copy2(AUTH_FILE, backup)
    try:
        shutil.copy2(meta[target]["snapshot"], AUTH_FILE)
    except Exception as e:
        shutil.copy2(backup, AUTH_FILE)  # 回滚
        print("❌ 写入失败已回滚:", e)
        sys.exit(3)
    # 更新 meta 里当前账号的快照为刚才的备份（保证两边都能切回）
    meta[uid_now] = meta.get(uid_now, {"name": meta[uid_now].get("name", uid_now[:8]) if uid_now in meta else uid_now[:8],
                                       "snapshot": backup, "saved_at": time.strftime("%F %T")})
    meta[uid_now]["snapshot"] = backup
    meta[uid_now]["saved_at"] = time.strftime("%F %T")
    save_meta(meta)
    print("✅ 已切换到 [%s]（uid %s...）" % (args.name, target[:8]))
    print("   原登录态已备份，可用 switch --name <原名> 切回。")
    print("   现在执行: python %s launch   （或手动启动 WorkBuddy）" % os.path.abspath(__file__))


def cmd_launch(_args):
    subprocess.Popen([CLIENT_EXE])
    print("WorkBuddy 已启动")


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 多账号快照切换工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="保存当前登录账号快照")
    p.add_argument("--name", required=True, help="账号代号，如 主号/备用1")
    sub.add_parser("list", help="列出快照")
    p = sub.add_parser("switch", help="切换到指定快照（先退出客户端）")
    p.add_argument("--name", required=True)
    p.add_argument("--force", action="store_true", help="客户端运行中仍强制写入（危险）")
    sub.add_parser("launch", help="启动客户端")
    args = ap.parse_args()
    {"snapshot": cmd_snapshot, "list": cmd_list,
     "switch": cmd_switch, "launch": cmd_launch}[args.cmd](args)


if __name__ == "__main__":
    main()
