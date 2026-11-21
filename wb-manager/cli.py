# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号管理平台 - 命令行工具

用法示例：
  python cli.py add --name 主号 --user me@qq.com --token TOKEN123 --priority 1
  python cli.py list
  python cli.py checkin              # 全部签到
  python cli.py checkin --id 1       # 指定账号签到
  python cli.py quota                # 查看所有账号额度
  python cli.py quota --refresh      # 刷新额度
  python cli.py switch               # 查看当前应使用的账号（自动切换结果）
  python cli.py serve --port 8300    # 启动 Web 界面
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import db  # noqa: E402
from app import scheduler  # noqa: E402
from app import workbuddy_client as wb  # noqa: E402


def cmd_add(args):
    aid = db.add_account(args.name, args.user or "", args.token, args.priority, args.total, args.used)
    print(f"✅ 账号已添加: id={aid} name={args.name} priority={args.priority}")


def cmd_list(args):
    accounts = db.list_accounts()
    if not accounts:
        print("（暂无账号）")
        return
    print(f"{'ID':<4}{'优先级':<7}{'名称':<14}{'账号':<22}{'状态':<10}{'额度':<16}{'上次签到'}")
    for a in accounts:
        total = a["quota_total"] or 0
        used = a["quota_used"] or 0
        quota = f"{used:.0f}/{total:.0f}" if total else "未设置"
        print(f"{a['id']:<4}{a['priority']:<7}{a['name']:<14}{(a['user_id'] or '-'):<22}{a['status']:<10}{quota:<16}{a['last_checkin_at'] or '从未'}")


def cmd_del(args):
    acc = db.get_account(args.id)
    if not acc:
        print(f"❌ 账号 id={args.id} 不存在")
        return
    db.delete_account(args.id)
    print(f"✅ 已删除账号: {acc['name']}")


def cmd_checkin(args):
    if args.id:
        acc = db.get_account(args.id)
        if not acc:
            print(f"❌ 账号 id={args.id} 不存在")
            return
        # 与 Web/定时签到共用同一后处理（scheduler._checkin_one）。
        # 此前这里单独一份，把签到接口的【累计总积分】写进 quota_total 冒充
        # 可用余额——正是全仓反复警示的口径错误，会污染面板积分与选号，
        # 直到下个整点额度刷新才自愈。统一后口径只有一处。
        r = scheduler._checkin_one(acc, "cli")
        bal = f"（可用积分 {r['available_credits']:.0f}）" if r.get("available_credits") is not None else ""
        print(f"{'✅ 签到成功' if r['ok'] else '❌ 签到失败'}: {acc['name']} -> {r['message']} {r['reward']}{bal}")
    else:
        results = scheduler.run_checkin_all(trigger_source="cli")
        for r in results:
            print(f"{'✅' if r['ok'] else '❌'} {r['account']}: {r['message']} {r['reward']}")


def cmd_quota(args):
    if args.refresh:
        scheduler.run_quota_refresh()
    accounts = db.list_accounts()
    print(f"{'ID':<4}{'名称':<14}{'积分余额':<12}{'状态':<10}{'余额更新时间'}")
    for a in accounts:
        # 积分余额 = 可用积分（总发放 - 已用），不是 quota_total（总发放）
        avail = db.account_available_credits(a)
        ts = f"{avail:.0f}" if avail is not None else "未查询"
        print(f"{a['id']:<4}{a['name']:<14}{ts:<12}{a['status']:<10}{a['quota_checked_at'] or '-'}")


def cmd_switch(args):
    acc = db.pick_active_account()
    if not acc:
        print("❌ 没有可用账号（全部积分耗尽或停用）")
        return
    avail = db.account_available_credits(acc)
    ts = f"{avail:.0f} 积分" if avail is not None else "未查询"
    print(f"📌 当前应使用: id={acc['id']} {acc['name']} (priority={acc['priority']}) 积分余额: {ts}")


def cmd_serve(args):
    import uvicorn
    from app.main import app
    print(f"🚀 Web 界面: http://0.0.0.0:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port)


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 多账号管理平台 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="添加账号")
    p.add_argument("--name", required=True)
    p.add_argument("--user", default="")
    p.add_argument("--token", required=True)
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--total", type=float, default=0)
    p.add_argument("--used", type=float, default=0)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="列出账号")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("del", help="删除账号")
    p.add_argument("--id", type=int, required=True)
    p.set_defaults(func=cmd_del)

    p = sub.add_parser("checkin", help="执行签到（默认全部账号）")
    p.add_argument("--id", type=int, default=None)
    p.set_defaults(func=cmd_checkin)

    p = sub.add_parser("quota", help="查看额度")
    p.add_argument("--refresh", action="store_true", help="先刷新再显示")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("switch", help="查看当前应使用的账号")
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("serve", help="启动 Web 服务")
    p.add_argument("--port", type=int, default=8300)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
