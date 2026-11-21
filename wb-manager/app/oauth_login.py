# -*- coding: utf-8 -*-
"""WorkBuddy 官方 OAuth 设备授权登录（插件登录链路）。

用途：在管理后台点「开始授权」→ 浏览器打开官方登录页完成登录/扫码 →
自动取回 refreshToken 与账号资料，免去手工运行导出脚本。

协议（与官方插件一致，社区实现见 workbuddy2api-hub / wk-switch）：
  1. POST {base}/v2/plugin/auth/state?platform=CLI  → data.state + data.authUrl
  2. 浏览器打开 authUrl 完成授权
  3. GET  {base}/v2/plugin/auth/token?state=...     → code=11217 等待；code=0 给令牌
  4. GET  {base}/v2/plugin/login/account?state=...  → 账号资料（Bearer accessToken）

渠道头必须按版本原样发送：网关按 User-Agent / Origin 校验渠道，
用 curl 或 python 默认 UA 会被拒。会话只放内存，TTL 600 秒；
status=ok 之前不写任何本地账号数据。
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

REALMS = {
    "cn": {
        "base": "https://copilot.tencent.com",
        "origin": "https://www.codebuddy.cn",
        "ua": "WorkBuddy/5.8.3",
        "domain": "copilot.tencent.com",
    },
    "intl": {
        "base": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "ua": "WorkBuddy/5.9.4",
        "domain": "www.workbuddy.ai",
    },
}

TTL_SECONDS = 600
# 上游版本之间 code 可能是整数或字符串，两种都要认；否则等待中的授权会被误判为失败。
WAIT_CODES = {11217, 11218, "11217", "11218"}
_lock = threading.Lock()
_sessions = {}  # state -> {realm, created, authUrl, name, priority, credential?}


def _headers(cfg):
    return {
        "User-Agent": cfg["ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }


def _request(method, url, cfg, payload=None, token=None):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in _headers(cfg).items():
        req.add_header(k, v)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"code": -1, "msg": "HTTP %s" % e.code}


def _data(body):
    """上游有时把字段放在 data 里，有时平铺在响应根，统一取值。"""
    d = body.get("data") if isinstance(body, dict) else None
    return d if isinstance(d, dict) else (body if isinstance(body, dict) else {})


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return ""


def _purge():
    now = time.time()
    for k in [k for k, v in _sessions.items() if now - v["created"] > TTL_SECONDS]:
        _sessions.pop(k, None)


def start(realm="cn", name="", priority=100):
    """申请一次授权，返回 state 与官方登录页地址。"""
    cfg = REALMS.get(realm)
    if not cfg:
        raise ValueError("realm 只能是 cn（国内版）或 intl（国际版）")
    body = _request("POST", cfg["base"] + "/v2/plugin/auth/state?platform=CLI", cfg, {})
    if body.get("code") not in (0, "0", None):
        raise RuntimeError("申请授权被拒：code=%s %s" %
                           (body.get("code"), str(body.get("msg") or "")[:100]))
    d = _data(body)
    state = _pick(d, "state")
    auth_url = _pick(d, "authUrl", "auth_url")
    if not state or not auth_url:
        raise RuntimeError("上游未返回授权信息（code=%s msg=%s）" %
                           (body.get("code"), str(body.get("msg") or "")[:100]))
    with _lock:
        _purge()
        _sessions[state] = {
            "realm": realm, "created": time.time(), "authUrl": auth_url,
            "name": (name or "").strip(), "priority": priority, "credential": None,
        }
    return {"state": state, "authUrl": auth_url, "realm": realm,
            "domain": cfg["domain"], "expires_in": TTL_SECONDS}


def poll(state):
    """轮询授权结果。成功时把凭据留在会话里，由调用方取走并入库。"""
    with _lock:
        s = _sessions.get(state)
    if not s:
        return {"status": "expired", "message": "授权会话不存在或已过期"}
    if time.time() - s["created"] > TTL_SECONDS:
        finish(state)
        return {"status": "expired", "message": "授权超时，请重新开始"}
    cfg = REALMS[s["realm"]]

    if s.get("credential"):
        return _ok_payload(s)

    try:
        body = _request("GET", cfg["base"] + "/v2/plugin/auth/token?state=" +
                        urllib.parse.quote(state), cfg)
    except Exception as e:
        # 网络抖动不应中断授权流程，下一轮继续轮询
        return {"status": "pending", "message": "网络重试中：%s" % str(e)[:80]}

    code = body.get("code")
    if code in WAIT_CODES:
        return {"status": "pending", "message": "等待浏览器完成授权…"}
    # 成功响应可能是 code=0、"0"，也可能不带 code 只给 data（上游版本差异）。
    # 若把 None 一律当 pending，这类成功会被永久跳过，永远拿不到凭据。
    if code not in (0, "0", None):
        return {"status": "error", "message": "上游返回 code=%s %s" %
                (code, str(body.get("msg") or "")[:100])}

    d = _data(body)
    at = _pick(d, "accessToken", "access_token")
    if not at:
        return {"status": "pending", "message": "等待上游下发 token…"}
    rt = _pick(d, "refreshToken", "refresh_token")
    if not rt:
        return {"status": "error", "message": "上游未返回 refreshToken，无法入库"}

    profile = {}
    try:
        p = _request("GET", cfg["base"] + "/v2/plugin/login/account?state=" +
                     urllib.parse.quote(state), cfg, token=at)
        pd = _data(p)
        profile = {
            "nickname": _pick(pd, "nickname", "name", "userName"),
            "email": _pick(pd, "email", "mail"),
            "uid": str(_pick(pd, "uid", "userId", "id", "sub") or ""),
            "enterprise": str(_pick(pd, "enterpriseId", "enterprise_id") or ""),
            "tier": str(_pick(pd, "tier", "level") or ""),
        }
    except Exception:
        pass  # 资料接口失败不阻断入库，名称由调用方兜底

    with _lock:
        s["credential"] = {
            "realm": s["realm"], "name": s["name"], "priority": s["priority"],
            "refresh_token": rt, "expires_at": _pick(d, "expiresAt", "expires_at"),
            "domain": _pick(d, "domain") or cfg["domain"], "profile": profile,
        }
    return _ok_payload(s)


def _ok_payload(s):
    c = s["credential"]
    return {
        "status": "ok",
        "profile": c["profile"],
        "realm": c["realm"],
        "mask": c["refresh_token"][:8] + "…",
        "message": "授权成功，正在写入账号池…",
    }


def get_credential(state):
    """取成功凭据（不删除会话）：入库失败时可重试，入库由调用方保证幂等。"""
    with _lock:
        s = _sessions.get(state)
        return dict(s["credential"]) if s and s.get("credential") else None


def finish(state):
    with _lock:
        _sessions.pop(state, None)


def cancel(state):
    finish(state)
    return {"ok": True}
