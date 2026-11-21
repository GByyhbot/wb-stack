# -*- coding: utf-8 -*-
"""
WorkBuddy 多账号管理平台 - WorkBuddy 客户端适配层（真实接口版）

已实测验证的官方接口（2026-11-06）：

1. 令牌刷新（refreshToken 会轮换，必须回写保存！）：
   POST https://copilot.tencent.com/v2/plugin/auth/token/refresh
   头: X-Refresh-Token: <rt> | X-Auth-Refresh-Source: plugin | X-Domain: copilot.tencent.com
   返回: {code:0, data:{accessToken, refreshToken}}

2. 签到状态（返回签到活动的累计总积分，不是可用余额！）：
   POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status
   头: Authorization: Bearer <accessToken>
   返回 data: {today_checked_in, streak_days, total_credits, daily_credit, ...}
   ⚠️ total_credits 是签到活动累计发放的总积分，客户端"可用积分"另查接口 4。

3. 签到领取：
   POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin
   返回 data.credit 为本次领取积分

4. 资源余额汇总（真实【可用积分】，2026-11-06 与客户端展示核对一致）：
   POST https://www.codebuddy.cn/billing/meter/get-user-resource-summary  （无 /v2 前缀！）
   头: Authorization: Bearer <accessToken> + 浏览器形态 User-Agent
   ⚠️ WAF 会拦截 python/Node 默认 UA（403 code=10085），UA 必须伪装。
   返回 data: {TotalCredits(可用), Packages[{CycleTotalCapacity,CycleUsedCapacity,
   CycleRemainCapacity}...], CreditGroups[...]}
   TotalCredits = 各套餐 CycleRemainCapacity 之和。

账号存储：每个账号保存自己的 refresh_token；access_token 短效，每次操作前现换现用。
"""
import json
import datetime as dt
import urllib.request
import urllib.error

REFRESH_URL = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
BILLING_BASE = "https://www.codebuddy.cn/v2"
STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"
SUMMARY_URL = "https://www.codebuddy.cn/billing/meter/get-user-resource-summary"
EXPIRY_PATH = "/billing/meter/get-user-resource-free-packages"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) WorkBuddy/5.9.3 Chrome/136.0.0.0 Safari/537.36")
DEFAULT_TIMEOUT = 15


def _post(url, token=None, headers=None, payload=None, timeout=DEFAULT_TIMEOUT):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "wb-manager/1.0"}
    if token:
        h["Authorization"] = "Bearer " + token
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body[:300]}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body[:300]}
        except Exception:
            return e.code, {}
    except Exception as e:
        return -1, {"error": str(e)}


def refresh_access_token(refresh_token):
    """
    用长效令牌换取接口令牌。
    返回 (ok, access_token, new_refresh_token, message)
    注意：refreshToken 官方会轮换，调用方必须保存返回的 new_refresh_token。
    """
    headers = {
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "plugin",
        "X-Domain": "copilot.tencent.com",
    }
    status, body = _post(REFRESH_URL, headers=headers)
    if status == -1:
        return False, "", "", "网络错误: %s" % body.get("error", "unknown")
    if body.get("code") == 0 and isinstance(body.get("data"), dict):
        data = body["data"]
        at = data.get("accessToken") or ""
        new_rt = data.get("refreshToken") or ""
        if at:
            return True, at, new_rt, "ok"
        return False, "", "", "刷新应答缺少 accessToken"
    code = body.get("code")
    msg = body.get("msg") or str(body)[:150]
    return False, "", "", "刷新失败 code=%s %s" % (code, msg)


def checkin(refresh_token, user_id=None):
    """
    执行签到（查询状态 → 未签则领取）。
    传入账号的 refresh_token，内部自动换 access_token。
    返回 (ok, message, reward, new_refresh_token, total_credits, streak_days)
      - new_refresh_token 轮换后非空，调用方必须回写
      - total_credits 为签到活动【累计总积分】（非可用余额，仅参考）
      - streak_days 为接口返回的真实连签天数（可能为 None）
    """
    ok, at, new_rt, msg = refresh_access_token(refresh_token)
    if not ok:
        return False, msg, "", "", None, None

    # 1) 查询状态
    status, body = _post(BILLING_BASE + STATUS_PATH, token=at)
    if status == -1:
        return False, "网络错误: %s" % body.get("error", ""), "", new_rt, None, None
    if body.get("code") == 0 and body.get("data"):
        d = body["data"]
        total = d.get("total_credits")
        streak = d.get("streak_days")
        if d.get("today_checked_in"):
            return True, "今日已签到（连续 %s 天）" % (streak if streak is not None else "?"), \
                   "0", new_rt, total, streak
    elif status != 200:
        return False, "HTTP %s: %s" % (status, str(body)[:150]), "", new_rt, None, None

    # 2) 领取签到
    status, body = _post(BILLING_BASE + CHECKIN_PATH, token=at)
    if status == -1:
        return False, "网络错误: %s" % body.get("error", ""), "", new_rt, None, None
    if body.get("code") == 0 and body.get("data"):
        d = body["data"]
        credit = d.get("credit") or d.get("daily_credit") or d.get("today_credit") or ""
        total = d.get("total_credits")
        # 领取接口可能不返回连签天数，再查一次状态
        streak = d.get("streak_days")
        if streak is None:
            _s, st_body = _post(BILLING_BASE + STATUS_PATH, token=at)
            if _s == 200 and st_body.get("code") == 0 and st_body.get("data"):
                streak = st_body["data"].get("streak_days")
                if total is None:
                    total = st_body["data"].get("total_credits")
        return True, "签到成功，+%s 积分" % credit, str(credit), new_rt, total, streak
    # code=10001 或已签提示
    msg2 = body.get("msg") or ""
    if "10001" in str(body.get("code")) or "已签" in msg2:
        return True, "今日已签到", "0", new_rt, None, None
    return False, "HTTP %s: %s" % (status, msg2 or str(body)[:150]), "", new_rt, None, None


def _billing_get_summary(at):
    """带浏览器 UA 请求余额汇总（WAF 拦截默认 UA）。返回解析后的 dict 或 None"""
    req = urllib.request.Request(SUMMARY_URL, data=b"{}", method="POST")
    req.add_header("Authorization", "Bearer " + at)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", BROWSER_UA)  # 缺失会被 WAF 403
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if body.get("code") == 0 and isinstance(body.get("data"), dict):
        return body["data"]
    return None


def query_quota_full(refresh_token):
    """
    一次刷新同时拿【可用积分 + 总发放/已用 + 积分到期时间】。
    到期实现：summary 拿套餐 PackageCodes → free-packages 查各包 CycleEndTime，取最早。
    返回 dict: {ok, available, granted, used, expire_at, days_left, msg, new_rt}
    """
    ok, at, new_rt, msg = refresh_access_token(refresh_token)
    if not ok:
        return {"ok": False, "available": None, "granted": None, "used": None,
                "expire_at": None, "days_left": None, "msg": msg, "new_rt": ""}

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    d = _billing_get_summary(at)
    if not d:
        return {"ok": False, "available": None, "granted": None, "used": None,
                "expire_at": None, "days_left": None, "msg": "余额查询失败", "new_rt": new_rt}
    available = _f(d.get("TotalCredits"))
    pkgs = d.get("Packages") or []
    granted = sum(_f(p.get("CycleTotalCapacity")) or 0 for p in pkgs)
    used = sum(_f(p.get("CycleUsedCapacity")) or 0 for p in pkgs)

    # 积分到期：用套餐 code 查 free-packages 的 CycleEndTime
    expire_at, days_left = None, None
    codes = [p.get("PackageCode") for p in pkgs if p.get("PackageCode")]
    if codes:
        try:
            body = {"PageNumber": 1, "PageSize": 50, "Status": [0, 3], "PackageCodes": codes}
            req = urllib.request.Request("https://www.codebuddy.cn" + EXPIRY_PATH,
                                         data=json.dumps(body).encode(), method="POST")
            req.add_header("Authorization", "Bearer " + at)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", BROWSER_UA)
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                ed = json.loads(resp.read().decode("utf-8", "replace"))
            times = []
            for a in ((ed.get("data") or {}).get("Accounts")) or []:
                cet = a.get("CycleEndTime")
                if cet:
                    times.append(cet)
            if times:
                earliest = min(times)  # 'YYYY-MM-DD HH:MM:SS' 字符串可直接比较
                expire_at = earliest
                days_left = (dt.datetime.strptime(earliest, "%Y-%m-%d %H:%M:%S")
                             - dt.datetime.now()).days
        except Exception:
            pass
    return {"ok": True, "available": available, "granted": granted or None,
            "used": used or None, "expire_at": expire_at, "days_left": days_left,
            "msg": "ok", "new_rt": new_rt}


def query_available_credits(refresh_token):
    """兼容包装：只取可用积分。返回 (ok, available, granted, used, msg, new_rt)"""
    r = query_quota_full(refresh_token)
    return (r["ok"], r["available"], r["granted"], r["used"], r["msg"], r["new_rt"])


def query_quota(refresh_token):
    """
    查询可用积分（真实剩余额度）。
    返回 (ok, credits, message, new_refresh_token)
    注意：旧版走签到接口 total_credits，那其实是累计总积分，已废弃。
    """
    ok, available, _g, _u, msg, new_rt = query_available_credits(refresh_token)
    return ok, available, msg, new_rt


def validate_token(refresh_token):
    """验证长效令牌有效性：尝试刷新"""
    ok, _at, _new, msg = refresh_access_token(refresh_token)
    return ok, msg
