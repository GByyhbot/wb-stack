# -*- coding: utf-8 -*-
"""WorkBuddy 多账号管理平台 - FastAPI 主应用"""
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from . import db
from . import gateway_sync
from . import scheduler
from . import workbuddy_client as wb
from . import oauth_login

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wb-manager")

# 日志同时落盘：容器重建会清空 stdout，历史故障将无现场可查。
# 仅在设置了 WB_DB_PATH（容器部署）时启用，写在数据库同目录。
_db_path = os.environ.get("WB_DB_PATH", "")
if _db_path:
    try:
        from logging.handlers import RotatingFileHandler
        _log_dir = os.path.dirname(_db_path) or "."
        os.makedirs(_log_dir, exist_ok=True)
        _fh = RotatingFileHandler(os.path.join(_log_dir, "wb-manager.log"),
                                  maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(_fh)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start_scheduler()
    yield
    scheduler.stop_scheduler()


app = FastAPI(title="WorkBuddy 多账号管理平台", lifespan=lifespan)

# 公网部署鉴权：设置 WB_BASIC_USER / WB_BASIC_PASSWORD 后启用 HTTP Basic。
# 未设置时不启用，保持原有局域网直连行为。
_basic_user = os.environ.get("WB_BASIC_USER", "")
_basic_password = os.environ.get("WB_BASIC_PASSWORD", "")
_basic_expected = ""
if _basic_user and _basic_password:
    _basic_expected = "Basic " + base64.b64encode(
        ("%s:%s" % (_basic_user, _basic_password)).encode("utf-8")
    ).decode("ascii")


@app.middleware("http")
async def basic_auth_guard(request, call_next):
    if _basic_expected and request.headers.get("Authorization") != _basic_expected:
        return Response("Unauthorized", status_code=401,
                        headers={"WWW-Authenticate": 'Basic realm="wb-manager"'})
    return await call_next(request)


# 响应压缩：首页 index.html 未压缩 27KB，gzip 后约 8.6KB（31%）。
# 链路带宽有限（跨网段经 SD-WAN 中转时实测仅 8~40KB/s），压缩能显著缩短首屏等待。
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ---------- 数据模型 ----------

class AccountIn(BaseModel):
    name: str
    user_id: str
    token: str
    priority: int = 100
    quota_total: float = 0
    quota_used: float = 0


class AccountUpdate(BaseModel):
    name: str | None = None
    user_id: str | None = None
    token: str | None = None
    priority: int | None = None
    quota_total: float | None = None
    quota_used: float | None = None
    status: str | None = None
    note: str | None = None              # 用户自定义备注


class SettingsIn(BaseModel):
    checkin_time: str | None = None      # "HH:MM"
    quota_min_percent: float | None = None
    switch_enabled: str | None = None    # "1" / "0"
    # B6: 原 api_base 字段已移除——客户端各接口地址是硬编码的不同域名，
    # 这个设置从未被任何逻辑读取，留在 UI 里只会误导。
    wecom_webhook: str | None = None     # 企业微信群机器人 Webhook 地址（异常告警）


class SwitchRequest(BaseModel):
    """额度切换调用请求：模拟业务方通过平台取可用账号"""
    exclude_account_ids: list[int] = []


# ---------- 页面 ----------

@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ---------- 用量统计（读取 rotator 的请求记账日志） ----------

@app.get("/api/usage")
def api_usage(days: int = 7):
    """聚合 wb-rotator 的 JSONL 请求日志：按日趋势 + 按模型 + 按账号×模型（含每日限额对比）"""
    import json, glob, collections, datetime as dt
    # rotator 的数据目录统一由 _rotator_home() 解析（容器内 = WB_ROTATOR_HOME）。
    # 不能只靠 __file__ 相对层级：容器里 /app/app/main.py 上溯三层是根目录，会找不到日志。
    log_dir = _rotator_home() / "logs"
    since = (dt.datetime.now() - dt.timedelta(days=max(1, min(days, 90)))).strftime("%F")
    per_day = collections.defaultdict(lambda: {"requests": 0, "tin": 0, "tout": 0})
    by_model = collections.defaultdict(lambda: {"requests": 0, "tin": 0, "tout": 0})
    by_acc_model = collections.defaultdict(
        lambda: {"requests": 0, "tin": 0, "tout": 0, "failed": 0, "limited": 0})
    # C2: recent 只保留最后 50 条（日志按月分文件、文件内按时间追加，
    # 迭代顺序即时间顺序，无需把窗口内全部记录读进内存再排序切片）
    recent = collections.deque(maxlen=50)
    files = sorted(glob.glob(str(log_dir / "requests-*.jsonl")))
    # C2: 先按文件名月份跳过整个窗口外的月文件，避免每次请求全量解析历史日志
    since_month = since[:7]
    def _file_month(fp):
        base = os.path.basename(fp)
        try:
            return base[9:13] + "-" + base[13:15]
        except Exception:
            return ""
    files = [fp for fp in files if _file_month(fp) >= since_month or not _file_month(fp)]
    if not files:
        # 静默返回空数组会让"路径配错"和"真的没有请求"无法区分，这里留一条告警。
        log.warning("用量统计未找到请求日志，log_dir=%s", log_dir)
    if files:
        for fp in files:
            try:
                for line in open(fp, encoding="utf-8"):
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    day = (r.get("ts") or "")[:10]
                    if not day or day < since:
                        continue
                    tin = r.get("prompt_tokens") or 0
                    tout = r.get("completion_tokens") or 0
                    per_day[day]["requests"] += 1
                    per_day[day]["tin"] += tin
                    per_day[day]["tout"] += tout
                    acc = r.get("account") or "?"
                    m = r.get("model") or "?"
                    by_model[m]["requests"] += 1
                    by_model[m]["tin"] += tin
                    by_model[m]["tout"] += tout
                    st = r.get("status")
                    err = (r.get("error") or "")
                    ak = by_acc_model[(acc, m)]
                    ak["requests"] += 1
                    ak["tin"] += tin
                    ak["tout"] += tout
                    if isinstance(st, int) and st >= 400:
                        ak["failed"] += 1
                    # 限流/额度耗尽特征：429 或错误文本含额度类关键词
                    if (st == 429 or any(
                            k in err for k in
                            ("429", "限流", "频率限制", "额度", "耗尽", "用尽",
                             "余额", "insufficient", "quota", "credit"))):
                        ak["limited"] += 1
                    recent.append(r)
            except Exception:
                continue

    # 合并每日限额配置，给出使用率（限额未设/0 表示不限，使用率返回 null）
    limits = _load_usage_limits()
    def _pct(used, lim):
        try:
            lim = float(lim)
        except (TypeError, ValueError):
            return None
        if not lim or lim <= 0:
            return None
        return min(100, int(round(used / lim * 100)))
    by_account_model = []
    for (acc, m), v in sorted(by_acc_model.items(),
                              key=lambda kv: (-kv[1]["requests"], kv[0][0])):
        lim = limits.get("%s|%s" % (acc, m)) or {}
        by_account_model.append({
            "account": acc,
            "model": m,
            "requests": v["requests"],
            "tin": v["tin"],
            "tout": v["tout"],
            "failed": v["failed"],
            "limited": v["limited"],
            "limit_requests": lim.get("requests", 0),
            "limit_tokens": lim.get("tokens", 0),
            "req_pct": _pct(v["requests"], lim.get("requests", 0)),
            "tok_pct": _pct(v["tin"] + v["tout"], lim.get("tokens", 0)),
        })
    return {
        "since": since,
        "per_day": [{"date": d, **v} for d, v in sorted(per_day.items())],
        "by_model": [{"model": m, **v} for m, v in
                     sorted(by_model.items(), key=lambda kv: -kv[1]["requests"])],
        "by_account_model": by_account_model,
        "limits": limits,
        "recent": list(recent)[::-1],
    }


@app.get("/api/usage/limits")
def api_get_usage_limits():
    return {"limits": _load_usage_limits()}


@app.post("/api/usage/limits")
def api_save_usage_limits(body: dict):
    """保存每账号×每模型的每日限量：{"limits": {"账号|模型": {"requests":N,"tokens":N}}}
    N=0 或缺省表示不限。"""
    lim = body.get("limits") if isinstance(body, dict) else None
    if not isinstance(lim, dict):
        raise HTTPException(400, "limits 必须是对象")
    clean = {}
    for k, v in lim.items():
        if not isinstance(k, str) or "|" not in k or not isinstance(v, dict):
            continue
        try:
            n_req = int(v.get("requests") or 0)
        except (TypeError, ValueError):
            n_req = 0
        try:
            n_tok = int(v.get("tokens") or 0)
        except (TypeError, ValueError):
            n_tok = 0
        clean[k] = {"requests": max(0, n_req), "tokens": max(0, n_tok)}
    if not _save_usage_limits(clean):
        raise HTTPException(500, "限额保存失败")
    return {"ok": True, "limits": clean}


# ---------- 账号管理 API ----------

@app.get("/api/accounts")
def api_accounts():
    accounts = db.list_accounts()
    for a in accounts:
        a["token"] = a["token"][:8] + "****" if len(a["token"]) > 8 else "****"  # 脱敏
        a["quota_percent"] = db.account_quota_percent(a)
        a["available_credits"] = db.account_available_credits(a)
        a["gateway_only"] = False

    # 合并网关池里尚未纳管的账号。
    # 这些账号只在 rotator 的 accounts.json 里，面板没有它们的独立 refresh token，
    # 因此不参与签到/额度刷新；但必须显示出来，否则"明明加进代理池了，面板却看不见"。
    known = [a["name"] for a in accounts]
    for g in _gateway_accounts():
        nm = (g.get("name") or "").strip()
        # B3: 与面板账号的同一性判定走唯一解析，前缀多命中时按未纳管显示，不误判
        if not nm or _resolve_name(nm, known) is not None:
            continue
        accounts.append({
            "id": None, "name": nm, "user_id": "", "priority": g.get("priority", 100),
            "quota_total": 0, "quota_used": 0, "status": "gateway", "token": "",
            "note": "", "last_checkin_at": None, "last_checkin_result": None,
            "checkin_streak": 0, "quota_checked_at": None, "quota_expire_at": None,
            "quota_percent": None, "available_credits": g.get("last_credits"),
            "gateway_only": True, "gateway_disabled": bool(g.get("disabled")),
        })
    return {"accounts": accounts}


@app.post("/api/accounts")
def api_add_account(acc: AccountIn):
    if not acc.name or not acc.token:
        raise HTTPException(400, "name 和 token 不能为空")
    aid = db.add_account(acc.name, acc.user_id, acc.token, acc.priority, acc.quota_total, acc.quota_used)
    return {"id": aid}


@app.put("/api/accounts/{account_id}")
def api_update_account(account_id: int, acc: AccountUpdate):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    fields = {k: v for k, v in acc.model_dump().items() if v is not None}
    db.update_account(account_id, **fields)
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
def api_delete_account(account_id: int):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    db.delete_account(account_id)
    return {"ok": True}


@app.post("/api/gateway-accounts/delete")
def api_delete_gateway_account(body: dict):
    """删除 rotator 账号池里的「仅代理」账号（按 name 匹配，不在面板数据库中）。"""
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(400, "name 不能为空")
    if not _remove_gateway_account(name):
        raise HTTPException(404, "代理池中未找到该账号")
    return {"ok": True}


@app.post("/api/accounts/{account_id}/checkin")
def api_checkin_one(account_id: int):
    """手动对单个账号签到。

    与定时签到共用 scheduler._checkin_one：此前这里另有一份签到后处理，
    与调度器版本逐渐漂移（不置 error 状态、不刷到期时间），且 CLI 还有第三份。
    统一后口径只有一处。
    """
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    r = scheduler._checkin_one(acc, "manual-one")
    return {"ok": r["ok"], "message": r["message"], "reward": r["reward"],
            "total_credits": r.get("total_credits"), "streak_days": r.get("streak_days"),
            "available_credits": r.get("available_credits")}


@app.post("/api/accounts/{account_id}/refresh-quota")
def api_refresh_quota_one(account_id: int):
    """手动刷新单个账号额度（真实可用积分 + 到期时间）"""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    r = wb.query_quota_full(acc["token"])
    fields = {"quota_checked_at": scheduler.time_str()}
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
    if r["new_rt"] and r["new_rt"] != acc["token"]:
        fields["token"] = r["new_rt"]
    db.update_account(account_id, **fields)
    # A1: 本侧轮换出的新令牌同步到网关池
    if fields.get("token") and fields["token"] != acc["token"]:
        gateway_sync.sync_gateway_rt(acc["name"], fields["token"])
    return {"ok": r["ok"], "quota_total": r["available"],
            "granted_total": r["granted"], "used_total": r["used"],
            "expire_at": r["expire_at"], "days_left": r["days_left"], "message": r["msg"]}


# ---------- 手动切换代理账号（置顶） ----------
#
# 置顶必须写网关真正读取的那份文件：rotator 的 PIN_FILE = WB_ROTATOR_HOME/pin.json。
# 早期这里用 __file__ 上溯三层推导，容器内会落到 /wb-rotator/pin.json（镜像根目录），
# 结果面板显示"已切换"、网关却纹丝不动。

def _rotator_home():
    """rotator 数据目录（与 app/gateway_sync.py 同一解析规则）"""
    return gateway_sync.rotator_home()


def _pin_file():
    return _rotator_home() / "pin.json"


def _limits_file():
    return _rotator_home() / "usage_limits.json"


def _load_usage_limits():
    """每账号×每模型的每日限量配置（{"账号|模型": {"requests":N,"tokens":N}}，0=不限）。

    官方产品配置只有积分单价、无每日限额字段，故限量为面板侧可配置项。
    """
    try:
        p = _limits_file()
        if p.is_file():
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("limits"), dict):
                return data["limits"]
    except Exception:
        pass
    return {}


def _save_usage_limits(limits):
    """原子写限额配置，避免并发/中途失败写坏文件"""
    try:
        p = _limits_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"limits": limits}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
        return True
    except Exception:
        return False


def _gateway_accounts():
    """网关账号池（rotator 的 accounts.json）。

    面板的 status 与网关的 disabled 是两套独立状态：账号可能在面板里正常，
    但网关侧被禁用而跳过。必须读出来才能解释"切过去却没用上"。
    B2: 返回前按 (priority, name) 排序——选号顺序以 priority 字段为准，
    不依赖文件物理顺序（OAuth 入库是 append，文件顺序≠优先级顺序）。
    """
    try:
        with open(_rotator_home() / "accounts.json", encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        return []
    accs = d if isinstance(d, list) else (d.get("accounts") or [])
    accs = [a for a in accs if isinstance(a, dict)]
    accs.sort(key=lambda a: (a.get("priority", 100), a.get("name") or ""))
    return accs


def _remove_gateway_account(name: str) -> bool:
    """从 rotator 的 accounts.json 删除指定 name 的「仅代理」账号。

    采用与 rotator 自身 save_accounts 一致的原子写（临时文件 + os.replace），
    且整段读-改-写持有跨进程文件锁（A2：网关进程可能同时在回写轮换令牌）。
    返回 True 表示确有删除；若池中不存在该账号返回 False。
    若被删账号恰是当前 pin（手动置顶）账号，顺手清理 pin.json，
    避免 rotator 在选号时仍指向已删除的账号。
    """
    target = (name or "").strip()
    if not target:
        return False
    with gateway_sync.gateway_file_lock() as path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            return False
        is_list = isinstance(raw, list)
        lst = raw if is_list else (raw.get("accounts") or [])
        new_lst = [a for a in lst if (a.get("name") or "").strip() != target]
        if len(new_lst) == len(lst):
            return False
        out = new_lst if is_list else {"accounts": new_lst}
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    # 清理 pin（若存在且指向被删账号）
    try:
        pf = _pin_file()
        if os.path.isfile(pf):
            with open(pf, encoding="utf-8") as f:
                pin = json.load(f) or {}
            if (pin.get("name") or "").strip() == target:
                os.remove(pf)
    except Exception:
        pass
    return True


def _rotator_pinned_name():
    """网关当前置顶的账号名（读 pin.json，与 rotator 同源）"""
    try:
        with open(_pin_file(), encoding="utf-8") as f:
            return (json.load(f) or {}).get("name") or None
    except Exception:
        return None


def _name_match(a, b):
    """与 rotator 一致的宽容匹配：全等，或一方为另一方的前缀。
    面板名「主号(主人)」/池名「主号」这类差异要视为同一账号。"""
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _resolve_name(target, candidates):
    """B3: 在候选名集合中把 target 解析为唯一账号名（与 rotator.resolve_name 同规则）。

    _name_match 是成对比较，「备用1」对「备用10」也算命中，多候选时会串号。
    按名找唯一账号的路径必须先经这里：全等优先，前缀仅唯一命中时生效。
    """
    if not target:
        return None
    cands = [c for c in candidates if c]
    if target in cands:
        return target
    hits = [c for c in cands if c.startswith(target) or target.startswith(c)]
    return hits[0] if len(hits) == 1 else None


@app.get("/api/pin-account")
def api_get_pin():
    """当前手动置顶的代理账号（读 rotator 的 pin.json）"""
    return {"name": _rotator_pinned_name()}


@app.post("/api/pin-account")
def api_set_pin(body: dict):
    """手动切换：把指定账号置顶为代理当前使用的账号（写 pin.json，rotator 实时读取）"""
    name = (body or {}).get("name")
    if name:
        # 允许置顶「仅存在于网关池」的账号（面板无独立令牌，但网关能直接用它）
        names = {a["name"] for a in db.list_accounts()}
        if name not in names:
            gw_names = [(g.get("name") or "") for g in _gateway_accounts()]
            if not any(_name_match(name, g) for g in gw_names if g):
                raise HTTPException(404, "账号不存在: %s" % name)
    pin_file = _pin_file()
    if name:
        pin_file.parent.mkdir(parents=True, exist_ok=True)
        # 原子写（tmp + replace），与本文件其他 JSON 写入口径一致：
        # rotator 每次选号都会立刻重新解析该文件，不能让它读到半截内容
        tmp = str(pin_file) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"name": name, "at": scheduler.time_str()}, f, ensure_ascii=False)
        os.replace(tmp, str(pin_file))
    elif pin_file.exists():
        pin_file.unlink()  # 清除置顶，恢复按优先级自动选择
    return {"ok": True, "pinned": name, "path": str(pin_file)}


@app.post("/api/accounts/{account_id}/validate")
def api_validate_token(account_id: int):
    """验证 token 有效性"""
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    ok, msg = wb.validate_token(acc["token"])
    if ok:
        db.update_account(account_id, status="active")
    return {"ok": ok, "message": msg}


# ---------- 签到 / 日志 / 调度 ----------

@app.post("/api/checkin/all")
def api_checkin_all():
    results = scheduler.run_checkin_all(trigger_source="manual-all")
    return {"results": results}


@app.get("/api/logs")
def api_logs(limit: int = Query(100, le=500), account_id: int | None = None):
    return {"logs": db.recent_logs(limit, account_id)}


@app.get("/api/settings")
def api_get_settings():
    return {"settings": db.get_settings()}


@app.put("/api/settings")
def api_set_settings(s: SettingsIn):
    changed = []
    for k, v in s.model_dump().items():
        if v is not None:
            db.set_setting(k, v)
            changed.append(k)
    if "checkin_time" in changed:
        scheduler.reschedule_checkin()
    return {"ok": True, "changed": changed}


# ---------- 额度自动切换 API（核心） ----------

def _gateway_state(name):
    """该账号在网关池里的记录；None 表示网关池中没有该账号（B3: 唯一解析）"""
    return gateway_sync.find_pool_entry(_gateway_accounts(), name)


def _pick_gateway_active():
    """按网关的真实规则选号：accounts.json 已按 priority 排序（_gateway_accounts 保证），
    跳过「网关侧禁用」和「额度耗尽」的账号，取第一个能用的。
    面板自己的 pick_active_account 不知道网关的 disabled，不能拿来替代。
    面板↔网关的名字配对走唯一解析（B3），前缀多命中时跳过该网关条目，不猜。
    """
    panel = db.list_accounts(include_disabled=False)
    panel_names = [a["name"] for a in panel]
    for g in _gateway_accounts():
        if g.get("disabled"):
            continue
        pname = _resolve_name(g.get("name") or "", panel_names)
        if not pname:
            continue
        a = next(x for x in panel if x["name"] == pname)
        avail = db.account_available_credits(a)
        if avail is not None and avail <= 0:
            continue
        return a
    return None


@app.get("/api/active-account")
def api_active_account():
    """返回当前实际在使用的账号。

    口径与网关一致：置顶账号优先，但它若在网关侧被禁用/耗尽，网关会跳过，
    此时必须按网关的选号规则回退，否则面板又会与实际不符。
    """
    pinned = _rotator_pinned_name()
    warning = ""
    acc = None

    if pinned:
        pin_gw = _gateway_state(pinned)
        if pin_gw and pin_gw.get("disabled"):
            warning = "置顶账号「%s」在网关侧已禁用" % pinned
        else:
            # B3: 置顶名先在面板账号里唯一解析，再取该账号（防前缀串号）
            panel = db.list_accounts(include_disabled=False)
            pname = _resolve_name(pinned, [a["name"] for a in panel])
            if pname:
                cand = next(a for a in panel if a["name"] == pname)
                avail = db.account_available_credits(cand)
                if avail is None or avail > 0:
                    acc = cand
            if acc is None and pin_gw:
                # 仅存在于网关池的账号（面板没有独立令牌）：用网关记录合成一条，
                # 否则置顶它之后横幅会错误地回退显示别的账号。
                acc = {
                    "id": None, "name": pin_gw.get("name") or pinned, "user_id": "",
                    "priority": pin_gw.get("priority", 100),
                    "quota_total": 0, "quota_used": 0, "quota_checked_at": None,
                    "status": "gateway", "token": "", "gateway_only": True,
                    "last_credits": pin_gw.get("last_credits"),
                }

    if acc is None:
        acc = _pick_gateway_active() or db.pick_active_account()
        if warning and acc:
            warning += "，请求实际顺延到「%s」" % acc["name"]

    if not acc:
        raise HTTPException(503, "没有可用账号：所有账号额度耗尽或已停用")

    if acc.get("gateway_only"):
        # 仅代理池账号没有独立令牌与周期额度，直接用网关缓存的可用积分展示
        acc["quota_percent"] = None
        acc["available_credits"] = acc.get("last_credits")
        acc["token"] = ""
    else:
        acc["quota_percent"] = db.account_quota_percent(acc)
        acc["available_credits"] = db.account_available_credits(acc)
        acc["token"] = acc["token"][:8] + "****"
    acc["pinned"] = bool(pinned) and _resolve_name(
        pinned, [acc["name"]]) == acc["name"]
    gw = _gateway_state(acc["name"])
    acc["gateway_disabled"] = bool(gw.get("disabled")) if gw else None
    return {"active": acc, "pinned": pinned, "warning": warning}


# ---------- 官方 OAuth 设备授权登录（免手工导出令牌） ----------
#
# 设计要点：
#  - 授权会话只放内存（TTL 600s），status=ok 之前不写任何账号数据；
#    refresh token 只经后端流转，返回前端的只有 8 位掩码。
#  - 一次授权同时写「面板 db」与「网关 accounts.json」：前者负责签到/额度，
#    后者负责代理调用。此前「添加账号」只写 db，需另行手工导入网关。
#  - accounts.json 用临时文件 + os.replace 原子替换，rotator 每次选号重读，
#    不会读到半截文件；配合 rotator 的会话动态同步，新账号无需重启容器。

class LoginStartIn(BaseModel):
    realm: str = "cn"            # cn=国内版 / intl=国际版
    name: str = ""               # 留空则用官方昵称 / 邮箱兜底
    priority: int = 100


def _write_gateway_account(name, rt, priority=100):
    """把账号写入 rotator 的 accounts.json；同名更新令牌，否则追加。

    整段读-改-写持有跨进程文件锁（A2）；同名判定走 gateway_sync 的
    全等优先/前缀唯一规则（B3），前缀多命中时按新账号追加，不误伤同名前缀的账号。
    """
    with gateway_sync.gateway_file_lock() as path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
        accs = raw if isinstance(raw, list) else (raw.get("accounts") or [])

        target = gateway_sync.find_pool_entry(accs, name)
        if target is None:
            accs.append({"name": name, "refresh_token": rt, "priority": priority,
                         "exhausted_date": "", "disabled": False})
            action = "added"
        else:
            target["refresh_token"] = rt
            target["disabled"] = False
            target["exhausted_date"] = ""
            action = "updated"

        out = accs if isinstance(raw, list) else {"accounts": accs}
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
        return action


def _import_oauth_account(cred):
    """授权成功后双写入库（幂等：同名账号更新令牌，不重复新增）。"""
    p = cred.get("profile") or {}
    name = ((cred.get("name") or "").strip() or (p.get("nickname") or "").strip()
            or (p.get("email") or "").strip()
            or ("授权账号-" + cred["refresh_token"][:8]))
    rt = cred["refresh_token"]
    uid = p.get("uid") or p.get("email") or ""
    priority = int(cred.get("priority") or 100)

    existing = next((a for a in db.list_accounts() if a["name"] == name), None)
    if existing:
        db.update_account(existing["id"], token=rt,
                          user_id=uid or existing["user_id"], status="active")
        panel, pid = "updated", existing["id"]
    else:
        pid = db.add_account(name, uid, rt, priority)
        panel = "added"
    gw = _write_gateway_account(name, rt, priority)
    # 只记名称与动作，绝不把令牌写进日志
    log.info("官方授权账号入库 name=%s realm=%s panel=%s gateway=%s",
             name, cred.get("realm"), panel, gw)
    return {"name": name, "uid": uid, "realm": cred.get("realm"),
            "panel": panel, "panel_id": pid, "gateway": gw}


@app.post("/api/login/start")
def api_login_start(body: LoginStartIn):
    try:
        return oauth_login.start(body.realm, body.name, body.priority)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("申请官方授权失败")
        raise HTTPException(502, "申请授权失败: %s" % str(e)[:150])


@app.get("/api/login/poll")
def api_login_poll(state: str):
    r = oauth_login.poll(state)
    if r.get("status") == "ok":
        cred = oauth_login.get_credential(state)
        if cred:
            try:
                r["import_result"] = _import_oauth_account(cred)
                oauth_login.finish(state)   # 入库成功后销毁内存里的凭据
                r["message"] = "授权成功，账号已写入面板与代理池"
            except Exception as e:
                log.exception("授权账号入库失败")
                r["status"] = "error"
                r["message"] = "入库失败: %s" % str(e)[:150]
    return r


@app.post("/api/login/cancel")
def api_login_cancel(body: dict):
    return oauth_login.cancel((body or {}).get("state") or "")


@app.post("/api/switch/consume")
def api_switch_consume(req: SwitchRequest):
    """
    业务方调用示例：先取当前可用账号 → 扣减额度 → 若耗尽自动顺延到下一个账号
    返回本次应使用的账号与切换信息
    """
    acc = db.pick_active_account(exclude_ids=req.exclude_account_ids)
    if not acc:
        raise HTTPException(503, "没有可用账号（可能全部额度耗尽或被排除）")
    return {"active": {"id": acc["id"], "name": acc["name"], "user_id": acc["user_id"],
                       "priority": acc["priority"],
                       "quota_percent": db.account_quota_percent(acc)},
            "token_preview": acc["token"][:8] + "****",
            "message": f"当前使用账号: {acc['name']} (priority={acc['priority']})"}


# ---------- 静态资源 ----------

# 静态目录按文件位置解析，不依赖启动时的工作目录（此前相对路径 "static"
# 要求进程必须从 wb-manager/ 启动，换个 CWD 直接 RuntimeError 起不来）
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
