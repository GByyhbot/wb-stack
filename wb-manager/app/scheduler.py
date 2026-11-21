# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号管理平台 - 调度器
APScheduler 定时任务：
1. 每日定时自动签到所有 active 账号
2. 每小时刷新一次所有账号额度
"""
import logging
import random
import time as _time
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from . import workbuddy_client as wb

log = logging.getLogger("wb-manager.scheduler")

# P4 签到随机化（借鉴 workbuddy2api）：总触发时间加抖动 + 账号间随机错开，避免整点齐发
CHECKIN_JITTER_SECONDS = 30 * 60   # 签到时刻随机偏移 ±（APScheduler jitter 在触发点后 0~jitter 内随机延迟）
ACCOUNT_SPREAD_SECONDS = 90        # 账号之间随机错开的最大间隔

_scheduler = None


def run_checkin_all(trigger_source="manual"):
    """对全部 active 账号执行签到，返回结果摘要。

    健壮性要求：单个账号失败或抛异常都不能中断整批。
    此前循环体内没有异常兜底，任一账号出问题会让后续账号全部漏签，
    且异常只进 stdout，容器重建后现场即丢失（2026-11-10 只签到主号一次即为此形态）。
    """
    accounts = db.list_accounts(include_disabled=False)
    random.shuffle(accounts)  # 打乱顺序，避免每次同一顺序
    results = []
    for i, acc in enumerate(accounts):
        if trigger_source == "cron" and i > 0:
            # 定时触发时账号间随机错开（手动触发立即执行）
            _time.sleep(random.uniform(5, ACCOUNT_SPREAD_SECONDS))
        try:
            results.append(_checkin_one(acc, trigger_source))
        except Exception as e:
            log.exception("[%s] 签到异常 %s", trigger_source, acc["name"])
            try:
                now = time_str()
                db.add_checkin_log(acc["id"], False, "异常: %s" % e, "")
                db.update_account(acc["id"], last_checkin_at=now,
                                  last_checkin_result=("异常 " + str(e))[:120])
            except Exception:
                log.exception("异常落库失败 %s", acc["name"])
            results.append({"account": acc["name"], "ok": False,
                            "message": "异常: %s" % str(e)[:120], "reward": ""})
    return results


def _checkin_one(acc, trigger_source):
    """单账号签到流程；任何异常由 run_checkin_all 兜底，不在此吞掉"""
    ok, msg, reward, new_rt, total, streak = wb.checkin(acc["token"], acc["user_id"])
    now = time_str()
    db.add_checkin_log(acc["id"], ok, msg, reward)
    fields = {
        "last_checkin_at": now,
        "last_checkin_result": ("成功 " + (reward or msg)) if ok else ("失败 " + msg[:120]),
    }
    # 令牌轮换：必须回写新的 refresh_token，否则下次刷新会失效
    if new_rt and new_rt != acc["token"]:
        fields["token"] = new_rt
    if ok:
        # 连签天数以接口返回的真实值为准，避免本地累加漂移
        if streak is not None:
            fields["checkin_streak"] = streak
        fields["status"] = "active"
        # 签到接口的 total_credits 是累计总积分而非可用余额，
        # 余额单独走 get-user-resource-summary 精确查询
        ok2, avail, granted, used, _m, new_rt2 = wb.query_available_credits(fields.get("token", acc["token"]))
        if new_rt2 and new_rt2 != fields.get("token", acc["token"]):
            fields["token"] = new_rt2
        if ok2 and avail is not None:
            if granted:  # 存总发放/已用，可用 = 差值，进度条真实反映消耗
                fields["quota_total"] = granted
                fields["quota_used"] = used or 0
            else:
                fields["quota_total"] = avail
                fields["quota_used"] = 0
            fields["quota_checked_at"] = now
    else:
        if "刷新失败" in msg or "认证" in msg:
            fields["status"] = "error"
    db.update_account(acc["id"], **fields)
    log.info("[%s] 签到 %s -> %s %s", trigger_source, acc["name"], "OK" if ok else "FAIL", msg)
    return {"account": acc["name"], "ok": ok, "message": msg, "reward": reward}


def notify_wecom(title, content):
    """企业微信群机器人告警（webhook 在设置里配置；未配置则跳过）"""
    webhook = (db.get_settings() or {}).get("wecom_webhook") or ""
    if not webhook.startswith("http"):
        return False
    import json as _json
    import urllib.request as _ur
    payload = {"msgtype": "markdown",
               "markdown": {"content": "**[WB-Manger] %s**\n%s" % (title, content)}}
    try:
        req = _ur.Request(webhook, data=_json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=10)
        log.info("企业微信通知已发送: %s", title)
        return True
    except Exception as e:
        log.warning("企业微信通知发送失败: %s", e)
        return False


def run_quota_refresh():
    """刷新全部 active 账号的额度信息（get-user-resource-summary，真实可用积分+到期时间）"""
    for acc in db.list_accounts(include_disabled=False):
        r = wb.query_quota_full(acc["token"])
        now = time_str()
        fields = {"quota_checked_at": now}
        prev_status = acc.get("status")
        if r["ok"] and r["available"] is not None:
            if r["granted"]:
                fields["quota_total"] = r["granted"]
                fields["quota_used"] = r["used"] or 0
            else:
                fields["quota_total"] = r["available"]
                fields["quota_used"] = 0
            fields["status"] = "active"
            if r["expire_at"]:
                fields["quota_expire_at"] = r["expire_at"]
        elif "刷新失败" in r["msg"] or "认证" in r["msg"]:
            fields["status"] = "error"
        if r["new_rt"] and r["new_rt"] != acc["token"]:
            fields["token"] = r["new_rt"]
        db.update_account(acc["id"], **fields)

        # 告警1：账号由正常转异常（每账号每自然日最多提醒一次）
        if fields.get("status") == "error" and prev_status == "active":
            st = db.get_settings()
            if st.get("token_alert_date") != now[:10]:
                db.set_setting("token_alert_date", now[:10])
                notify_wecom("⚠️ 账号 Token 异常",
                             "账号「%s」刷新失败：%s\n请重新导出令牌并更新。" % (acc["name"], r["msg"][:120]))
        # 告警2：积分 3 天内到期（每自然日最多提醒一次）
        if (r["ok"] and r["days_left"] is not None and r["days_left"] <= 3
                and (r["available"] or 0) > 0):
            st = db.get_settings()
            if st.get("expiry_alert_date") != now[:10]:
                db.set_setting("expiry_alert_date", now[:10])
                notify_wecom("⏳ 积分即将到期",
                             "账号「%s」的 %s 可用积分将于 %s 到期（还剩 %s 天），建议优先使用该账号。"
                             % (acc["name"], r["available"], r["expire_at"], r["days_left"]))
        log.info("额度刷新 %s -> ok=%s 可用=%s 到期=%s", acc["name"], r["ok"], r["available"], r["expire_at"])


def _cron_from_hhmm(hhmm):
    try:
        h, m = hhmm.split(":")
        return int(h), int(m)
    except Exception:
        return 8, 30


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    settings = db.get_settings()
    h, m = _cron_from_hhmm(settings.get("checkin_time", "08:30"))
    _scheduler.add_job(run_checkin_all, CronTrigger(hour=h, minute=m, timezone="Asia/Shanghai"),
                       id="daily_checkin", args=["cron"], replace_existing=True,
                       jitter=CHECKIN_JITTER_SECONDS,
                       # 错过触发点后 1 小时内仍补跑（默认只有 1 秒，容器刚重启就会漏签）
                       misfire_grace_time=3600, coalesce=True, max_instances=1)
    _scheduler.add_job(run_quota_refresh, CronTrigger(minute=0, timezone="Asia/Shanghai"),
                       id="quota_refresh", replace_existing=True,
                       misfire_grace_time=600, coalesce=True, max_instances=1)
    _scheduler.start()
    log.info("调度器已启动: 每日 %02d:%02d 签到 / 每小时整点刷新额度", h, m)
    return _scheduler


def reschedule_checkin():
    """签到时间被修改后重建 cron"""
    global _scheduler
    if _scheduler is None:
        return
    settings = db.get_settings()
    h, m = _cron_from_hhmm(settings.get("checkin_time", "08:30"))
    _scheduler.reschedule_job("daily_checkin", trigger=CronTrigger(hour=h, minute=m, timezone="Asia/Shanghai"))
    log.info("签到计划已更新为每日 %02d:%02d", h, m)


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def time_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
