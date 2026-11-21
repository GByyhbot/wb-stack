# -*- coding: utf-8 -*-
"""
WorkBuddy 成长中心自动化（B 包，2026-11-21）。

在每日签到之外把成长中心的积分也领满：Buddy 旅行礼物、任务接单与领奖、
断登补登、连登奖励兑换、盲盒抽奖、能量开 Buddy、派出 Buddy。

接口契约来源：88lin/workbuddy-auto-signin 的实测实现（其契约注释标注
「从桌面端成长中心 H5 读出并经真实日历核对」），关键契约包括：
  - 任务接单 POST /tasks/accept 的 body 是 {"task_codes": [...]}（复数数组，
    单数形式在新服务端一律 400）；领奖是 POST /tasks/{code}/claim（路径带 code）。
  - 连登兑换 tier 传档位标识 "7d"/"14d"/"28d"（传档位名/纯数字均被拒）；
    403「连登天数不足」是业务常态，必须先于认证失败判定，否则未解锁档位
    会被误判成权限拒绝、中止整轮。
  - 抽奖与兑换必须带 client_token（"u-<uuid>"），缺失直接 400。
  - 补登候选 = 当月、今天之前、活动上线后、活跃分数为 0、未补登过的日期，
    最近的断点优先；每轮最多补 1 天（MAKEUP_MAX_PER_RUN）。
  - 写操作一律不重试（超时可能发生在服务端处理完成之后，重试会重复提交）；
    只读查询可短重试。
执行顺序有讲究：先领旅行礼物（开 Buddy 会覆盖旅行记录）→ 任务领奖
（任务送的抽奖机会/能量后面马上用）→ 补登（改变连登天数）→ 连登兑换 →
抽奖 → 开 Buddy → 最后派出。任一子模块失败不影响其余模块领取；
认证被拒时结束本轮，但保留已到账奖励的汇报。

鉴权：与签到同一套——refresh_token 换 accessToken 后 Bearer 调用，
另带 X-User-Id（账号 user_id）。成长中心在 copilot.tencent.com 域，
与 billing 的 www.codebuddy.cn 不同域，不要混用。
"""
import json
import math
import re
import urllib.error
import urllib.request
import uuid
from datetime import date

from . import workbuddy_client as wb

GROWTH_BASE = "https://copilot.tencent.com/v2/activity/growth"
MAKEUP_MAX_PER_RUN = 1
REQUEST_TIMEOUT = 20
REDEEM_TIERS = (
    ("7d", "starter", "入门", 7),
    ("14d", "advanced", "进阶", 14),
    ("28d", "legendary", "巅峰", 28),
)


# ---------------------------------------------------------------------------
# 响应取值小工具（与参考实现同语义：兼容 data/result 信封）
# ---------------------------------------------------------------------------
def _dig(obj, key):
    if isinstance(obj, dict):
        if key in obj and obj[key] is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if k in obj and isinstance(obj[k], dict):
                r = _dig(obj[k], key)
                if r is not None:
                    return r
    return None


def _as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        return int(float(v))
    except (TypeError, ValueError, OverflowError):
        return default


def _api_success(code, body):
    bc = body.get("code") if isinstance(body, dict) else None
    return 200 <= code < 300 and (bc is None or (type(bc) is int and bc == 0))


def _is_hard_failure(code):
    return code == -1 or code >= 500


def _client_token():
    return "u-%s" % uuid.uuid4()


def _makeup_candidates(streak_body, heatmap_body):
    """从活跃日历选断登日期；结构异常抛 ValueError，调用方整段停手不猜测。"""
    def parse_date(value):
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError("补登日期格式无效")
        return date.fromisoformat(value)

    today_obj = _dig(heatmap_body, "today")
    if not isinstance(today_obj, dict):
        raise ValueError("活跃日历缺少服务端日期")
    today = parse_date(today_obj.get("date"))
    launch = parse_date(_dig(streak_body, "launch_date"))
    start = max(today.replace(day=1), launch, date(2026, 6, 17))
    streak = _dig(streak_body, "streak")
    if not isinstance(streak, dict):
        raise ValueError("连登状态格式无效")
    made_up_raw = streak.get("makeup_dates", [])
    if not isinstance(made_up_raw, list):
        raise ValueError("已补登记录格式无效")
    made_up = {parse_date(v) for v in made_up_raw}
    cells = _dig(heatmap_body, "cells")
    if not isinstance(cells, list):
        raise ValueError("活跃日历缺少日期列表")
    scores = {}
    for cell in cells:
        if not isinstance(cell, dict):
            raise ValueError("活跃日历记录格式无效")
        day = parse_date(cell.get("date"))
        score = cell.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
            raise ValueError("活跃日历分数无效")
        if day in scores and scores[day] != score:
            raise ValueError("活跃日历同一日期的记录冲突")
        scores[day] = score
    return sorted((d.isoformat() for d, s in scores.items()
                   if start <= d < today and s == 0 and d not in made_up),
                  reverse=True)


class _AuthStop(Exception):
    """认证/权限被拒：结束本轮成长中心（已到账奖励的汇报由调用方保留）"""


class _Growth:
    def __init__(self, access_token, user_id):
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer " + access_token,
            "User-Agent": "WorkBuddy",
        }
        if user_id:
            self.headers["X-User-Id"] = str(user_id)
        self.parts = []
        self.credits = 0
        self.failures = 0
        self.hard_failures = 0
        self.successes = 0

    # ---- HTTP：只读可短重试一次；写操作不重试 ----
    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(GROWTH_BASE + path, data=data,
                                     headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, json.loads(raw)
                except Exception:
                    return resp.status, {"raw": raw[:300]}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:300]}
        except Exception as e:
            return -1, {"error": str(e)}

    def get(self, path):
        code, body = self._request("GET", path)
        if code == -1 or code >= 500:
            code, body = self._request("GET", path)
        return code, body

    def post(self, path, payload=None):
        return self._request("POST", path, payload if payload is not None else {})

    # ---- 通用判定 ----
    @staticmethod
    def _check_auth(code):
        if code in (401, 403):
            raise _AuthStop()

    def _note_http(self, code, body, label, required=False):
        """前置查询失败记录；返回 True 表示跳过该段后续处理。"""
        if 200 <= code < 300 and (not required or _api_success(code, body)):
            return False
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("error") or body.get("msg") or "")
        reason = "网络不可达" if code == -1 else "HTTP %s" % code
        self.parts.append("%s失败：%s" % (label, detail or reason))
        if required or _is_hard_failure(code):
            self.failures += 1
            self.hard_failures += 1
        return True


def run_growth(refresh_token, user_id=None):
    """
    对单个账号执行一轮成长中心。返回 dict：
      {ok, report, credits_gained, energy, streak_days, idle,
       failures, auth_failed, new_rt}
    new_rt 为令牌轮换后的新 refresh_token（非空时调用方必须回写并同步网关）。
    """
    ok_r, at, new_rt, msg = wb.refresh_access_token(refresh_token)
    if not ok_r:
        return {"ok": False, "report": "成长中心跳过：令牌刷新失败（%s）" % msg,
                "credits_gained": 0, "energy": None, "streak_days": None,
                "idle": False, "failures": 1, "auth_failed": True, "new_rt": ""}

    g = _Growth(at, user_id)
    travel = None
    daily_limit = False
    streak_body = None
    streak_stale = False
    auth_failed = False

    try:
        # --- 1. Buddy 旅行：先领已有礼物，派出放到最后 ---
        try:
            scode, sbody = g.get("/buddy/travel/status")
            g._check_auth(scode)
            if not g._note_http(scode, sbody, "查旅行状态", required=True):
                state = _dig(sbody, "state")
                if state in ("idle", "traveling", "arrived"):
                    travel = state
                    daily_limit = bool(_dig(sbody, "daily_limit_reached"))
                else:
                    g.parts.append("旅行状态无法识别，跳过开 Buddy 与派出")
                    g.failures += 1
                    g.hard_failures += 1
            if travel == "arrived":
                ccode, cbody = g.post("/buddy/travel/claim",
                                      {"record_id": _dig(sbody, "record_id")})
                g._check_auth(ccode)
                if _api_success(ccode, cbody) and _dig(cbody, "reward_credit") is not None:
                    got = _as_int(_dig(cbody, "reward_credit"))
                    g.credits += got
                    g.parts.append("领旅行礼物 +%s 积分" % got)
                    g.successes += 1
                    travel = "idle"
                else:
                    m = (_dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                    g.parts.append("领旅行礼物失败：%s" % (m or "HTTP %s" % ccode))
                    g.failures += 1
                    g.hard_failures += _is_hard_failure(ccode)
            if travel == "idle" and daily_limit:
                g.parts.append("今日旅行名额已用完")
            elif travel == "traveling":
                loc = _dig(sbody, "location")
                loc_name = loc.get("name", "?") if isinstance(loc, dict) else "?"
                g.parts.append("Buddy 旅行中（%s）" % loc_name)
        except _AuthStop:
            raise
        except Exception as e:
            g.parts.append("旅行模块异常（%s）" % type(e).__name__)
            g.failures += 1
            g.hard_failures += 1

        # --- 2. 任务：接单（未接的先接，进度从接单起算）+ 领奖 ---
        try:
            tcode, tbody = g.get("/tasks")
            g._check_auth(tcode)
            if not g._note_http(tcode, tbody, "查任务列表"):
                tasks = _dig(tbody, "tasks") or []
                titles = {t.get("task_code"): t.get("title", t.get("task_code"))
                          for t in tasks if isinstance(t, dict)}
                pending = [t.get("task_code") for t in tasks
                           if isinstance(t, dict) and t.get("task_code")
                           and not t.get("locked")
                           and t.get("accept_status") == "not_accepted"]
                for i in range(0, len(pending), 20):
                    batch = pending[i:i + 20]
                    acode, abody = g.post("/tasks/accept", {"task_codes": batch})
                    g._check_auth(acode)
                    results = _dig(abody, "results")
                    if not isinstance(results, list):
                        results = [{"task_code": c,
                                    "status": "ok" if 200 <= acode < 300 else "error",
                                    "message": _dig(abody, "msg")} for c in batch]
                    for r in results:
                        title = titles.get(r.get("task_code"), r.get("task_code"))
                        if r.get("status") == "error":
                            g.parts.append("领取任务「%s」失败：%s"
                                           % (title, r.get("message") or "HTTP %s" % acode))
                            g.failures += 1
                            g.hard_failures += _is_hard_failure(acode)
                        else:
                            g.parts.append("领取任务「%s」（进度开始计）" % title)
                            g.successes += 1
                for t in tasks:
                    if not isinstance(t, dict) or t.get("locked") \
                            or t.get("accept_status") != "completed":
                        continue
                    code = t.get("task_code")
                    title = titles.get(code, code)
                    ccode, cbody = g.post("/tasks/%s/claim" % code, {})
                    g._check_auth(ccode)
                    if 200 <= ccode < 300 and not _dig(cbody, "already_claimed"):
                        rc = _as_int(_dig(cbody, "credit"), 0) \
                            if _dig(cbody, "credit") is not None \
                            else _as_int(t.get("reward_credit"))
                        re_ = _as_int(_dig(cbody, "energy"), 0) \
                            if _dig(cbody, "energy") is not None \
                            else _as_int(t.get("reward_energy"))
                        g.credits += rc
                        g.parts.append("领任务奖「%s」+%s 积分" % (title, rc))
                        g.successes += 1
                    elif 200 <= ccode < 300:
                        g.parts.append("任务奖「%s」已领过" % title)
                    else:
                        m = (_dig(cbody, "msg") or "") if isinstance(cbody, dict) else ""
                        g.parts.append("领任务奖「%s」失败：%s" % (title, m or "HTTP %s" % ccode))
                        g.failures += 1
                        g.hard_failures += _is_hard_failure(ccode)
        except _AuthStop:
            raise
        except Exception as e:
            g.parts.append("任务模块异常（%s）" % type(e).__name__)
            g.failures += 1
            g.hard_failures += 1

        # --- 3. 补登：断登自动补一张（每轮最多 1 张），保住连登 ---
        try:
            mcode, mbody = g.get("/streak")
            g._check_auth(mcode)
            if not g._note_http(mcode, mbody, "查连登状态", required=True):
                streak_body = mbody
                cards_obj = _dig(mbody, "makeup_cards")
                cards = _as_int(cards_obj.get("balance")) if isinstance(cards_obj, dict) \
                    else _as_int(cards_obj)
                dates = []
                if cards > 0:
                    hcode, hbody = g.get("/heatmap")
                    g._check_auth(hcode)
                    if not g._note_http(hcode, hbody, "查补登日历", required=True):
                        dates = _makeup_candidates(mbody, hbody)
                for d in dates[:min(cards, MAKEUP_MAX_PER_RUN)]:
                    ucode, ubody = g.post("/makeup-cards/use", {"target_date": d})
                    g._check_auth(ucode)
                    if _api_success(ucode, ubody):
                        streak_stale = True
                        left_obj = _dig(ubody, "makeup_cards")
                        left = _as_int(left_obj.get("balance"), cards - 1) \
                            if isinstance(left_obj, dict) else _as_int(left_obj, cards - 1)
                        g.parts.append("补登 %s（剩 %s 张卡）" % (d, left))
                        g.successes += 1
                    else:
                        m = (_dig(ubody, "msg") or "") if isinstance(ubody, dict) else ""
                        if ucode == 400 and str(m).strip().lower() == \
                                "date is not broken, no makeup needed":
                            g.parts.append("%s 已活跃或已补登，无需再次补登" % d)
                        else:
                            g.parts.append("补登 %s 失败：%s" % (d, m or "HTTP %s" % ucode))
                            g.failures += 1
                            g.hard_failures += 1
                        break
        except _AuthStop:
            raise
        except Exception as e:
            g.parts.append("补登模块异常（%s）" % type(e).__name__)
            g.failures += 1
            g.hard_failures += 1

        # --- 4. 连登奖励兑换（三档；未解锁的 403 是常态，先于认证判定）---
        try:
            rcode, rbody = g.get("/redeem/summary")
            g._check_auth(rcode)
            if not g._note_http(rcode, rbody, "查连登兑换"):
                for tier, status_key, label, days in REDEEM_TIERS:
                    status = _dig(rbody, status_key + "_status")
                    if not status or status in ("claimed", "locked"):
                        continue
                    c2code, c2body = g.post("/redeem", {"tier": tier,
                                                        "client_token": _client_token()})
                    # tier 标识被判未知 → 参数校验阶段的拒绝，未兑换任何东西，
                    # 退回天数形式重试一次不会重复领取（仅限明确的 unknown tier）
                    bm = str(_dig(c2body, "msg") or "").lower()
                    if c2code == 400 and "unknown tier" in bm:
                        c2code, c2body = g.post("/redeem", {"tier": days,
                                                            "client_token": _client_token()})
                        bm = str(_dig(c2body, "msg") or "").lower()
                    if c2code == 403 and ("insufficient" in bm or "不足" in bm
                                          or "not enough" in bm or "locked" in bm):
                        g.parts.append("连登兑换「%s」未解锁（连登天数不足）" % label)
                        continue
                    g._check_auth(c2code)
                    if 200 <= c2code < 300:
                        got = _as_int(_dig(c2body, "credit_granted"))
                        g.credits += got
                        g.parts.append("连登兑换「%s」成功 +%s 积分" % (label, got))
                        g.successes += 1
                    else:
                        m = (_dig(c2body, "msg") or "") if isinstance(c2body, dict) else ""
                        g.parts.append("连登兑换「%s」失败：%s" % (label, m or "HTTP %s" % c2code))
                        g.failures += 1
                        g.hard_failures += _is_hard_failure(c2code)
        except _AuthStop:
            raise
        except Exception as e:
            g.parts.append("连登兑换模块异常（%s）" % type(e).__name__)
            g.failures += 1
            g.hard_failures += 1

        # --- 5. 盲盒抽奖（有机会就开，一轮只开一次，剩余留下轮）---
        try:
            lcode, lbody = g.get("/lottery/chances")
            g._check_auth(lcode)
            chances = 0 if g._note_http(lcode, lbody, "查抽奖机会") \
                else _as_int(_dig(lbody, "balance"))
            if chances > 0:
                dcode, dbody = g.post("/lottery/draw",
                                      {"client_token": _client_token()})
                g._check_auth(dcode)
                if 200 <= dcode < 300:
                    prize = _dig(dbody, "prize_name") or _dig(dbody, "prize") or "未知"
                    if not isinstance(prize, str):
                        prize = str(prize)
                    if _dig(dbody, "need_address") or _dig(dbody, "require_address"):
                        prize += "（实物奖，需到成长中心填写收件信息）"
                    g.parts.append("开盲盒获得：%s" % prize)
                    g.successes += 1
                    if chances > 1:
                        g.parts.append("还剩 %s 次抽奖机会，下轮继续" % (chances - 1))
                else:
                    m = (_dig(dbody, "msg") or "") if isinstance(dbody, dict) else ""
                    g.parts.append("开盲盒失败：%s" % (m or "HTTP %s" % dcode))
                    g.failures += 1
                    g.hard_failures += _is_hard_failure(dcode)
        except _AuthStop:
            raise
        except Exception as e:
            g.parts.append("盲盒模块异常（%s）" % type(e).__name__)
            g.failures += 1
            g.hard_failures += 1

        # --- 6. 能量开 Buddy 盲盒（仅旅行 idle 时；开盒会覆盖旅行记录）---
        if travel != "idle":
            g.parts.append("Buddy 盲盒跳过：%s"
                           % ("旅行尚未结束或礼物未领到"
                              if travel in ("traveling", "arrived") else "旅行状态未知"))
        else:
            try:
                qcode, qbody = g.get("/buddy/quota")
                g._check_auth(qcode)
                if not g._note_http(qcode, qbody, "查 Buddy 能量"):
                    affordable = _as_int(_dig(qbody, "affordable"))
                    max_open = _as_int(_dig(qbody, "max_open_count"), 1) or 1
                    if affordable > 0:
                        count = min(affordable, max_open)
                        ocode, obody = g.post("/buddy/open",
                                              {"count": count,
                                               "client_token": _client_token()})
                        g._check_auth(ocode)
                        if 200 <= ocode < 300:
                            name = _dig(obody, "buddy") or _dig(obody, "name") \
                                or _dig(obody, "buddies")
                            if not isinstance(name, str):
                                name = "新 Buddy"
                            g.parts.append("开 Buddy 盲盒 ×%s（%s）" % (count, name))
                            g.successes += 1
                        else:
                            m = (_dig(obody, "msg") or "") if isinstance(obody, dict) else ""
                            g.parts.append("开 Buddy 盲盒失败：%s" % (m or "HTTP %s" % ocode))
                            g.failures += 1
                            g.hard_failures += _is_hard_failure(ocode)
            except _AuthStop:
                raise
            except Exception as e:
                g.parts.append("Buddy 盲盒模块异常（%s）" % type(e).__name__)
                g.failures += 1
                g.hard_failures += 1

        # --- 7. 最后派出 Buddy（不覆盖在途礼物，每日名额未用完才派）---
        if travel == "idle" and not daily_limit:
            try:
                ccode, cbody = g.get("/buddy/travel/config")
                g._check_auth(ccode)
                if not g._note_http(ccode, cbody, "查旅行配置", required=True):
                    locs = _dig(cbody, "locations")
                    if locs and isinstance(locs[0], dict):
                        dcode, dbody = g.post("/buddy/travel/depart",
                                              {"location_id": locs[0].get("id")})
                        m = str(_dig(dbody, "msg") or "")
                        if dcode == 403 and m.strip().lower() == "no active buddy":
                            g.parts.append("派 Buddy 跳过：尚未领养 Buddy")
                        else:
                            g._check_auth(dcode)
                            if _api_success(dcode, dbody):
                                loc = _dig(dbody, "location")
                                loc = loc if isinstance(loc, dict) else {}
                                dur = _dig(dbody, "duration_hours") \
                                    or loc.get("duration_hours", "?")
                                g.parts.append("派 Buddy 去%s（%s 小时后回）"
                                               % (loc.get("name", "?"), dur))
                                g.successes += 1
                            else:
                                g.parts.append("派 Buddy 失败：%s" % (m or "HTTP %s" % dcode))
                                g.failures += 1
                                g.hard_failures += _is_hard_failure(dcode)
            except _AuthStop:
                raise
            except Exception as e:
                g.parts.append("派 Buddy 模块异常（%s）" % type(e).__name__)
                g.failures += 1
                g.hard_failures += 1

    except _AuthStop:
        auth_failed = True
        g.parts.append("认证或权限被拒，成长中心中止（已到账奖励不受影响）")

    # --- 展示值：能量与连签天数（取不到不计失败）---
    energy = None
    try:
        ecode, ebody = g.get("/energy")
        if 200 <= ecode < 300:
            energy = _dig(ebody, "balance")
    except Exception:
        pass
    streak_days = None
    try:
        if streak_body is not None and not streak_stale:
            sobj = _dig(streak_body, "streak") or {}
            streak_days = sobj.get("days") if isinstance(sobj, dict) else None
        else:
            scode2, sbody2 = g.get("/streak")
            if 200 <= scode2 < 300:
                sobj = _dig(sbody2, "streak") or {}
                streak_days = sobj.get("days") if isinstance(sobj, dict) else None
    except Exception:
        pass

    report = "；".join(g.parts) if g.parts else "成长中心无可领取项"
    tail = []
    if energy is not None:
        tail.append("能量 %s" % energy)
    if streak_days is not None:
        tail.append("连签 %s 天" % streak_days)
    if g.credits:
        tail.append("本次 +%s 积分" % g.credits)
    if tail:
        report += "（%s）" % "，".join(tail)
    return {"ok": not g.hard_failures and not auth_failed,
            "report": report, "credits_gained": g.credits,
            "energy": energy, "streak_days": streak_days,
            "idle": g.successes == 0 and g.failures == 0,
            "failures": g.failures, "auth_failed": auth_failed,
            "new_rt": new_rt}
