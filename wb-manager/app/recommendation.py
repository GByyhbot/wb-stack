# -*- coding: utf-8 -*-
"""
每日用量推荐引擎（纯计算，无 IO，便于单测）。

目标（到期优先）：积分会过期，先烧快到期的；同时别把量压到不健康的账号上。
输入：
  accounts: [{name, available, days_left, expire_at, status,
              in_gateway, gateway_disabled, gateway_exhausted}]
            available/days_left 为 None 表示未知（未查积分/无到期时间/仅代理账号）
  usage:    {(account, model): {requests, tokens, failed, limited}}  近 window_days 天
  limits:   {"账号|模型": {"requests": int, "tokens": int}}           每日限量（0=不限）
输出见 build_recommendation 的 return 结构。

口径说明（不编造数据）：
- 用量日志只记请求数/token，不记积分消耗，因此「建议量」以请求数为单位；
  积分只做预算约束：每账号每日积分目标 = 可用积分 / 剩余天数（到期摊派）。
- 模型的日均量 = 近 window_days 天总请求 / window_days。
"""
import statistics


def _budget_weight(acc, fallback):
    """账号的日预算权重：有到期时间按摊派，无到期按可用积分/30 天平滑，
    未知预算用全体中位数（中性）。"""
    avail, dl = acc.get("available"), acc.get("days_left")
    if avail is not None and dl is not None:
        return avail / max(dl, 1)
    if avail is not None:
        return avail / 30.0
    return fallback


def _is_candidate(acc):
    """能承接流量的账号：在网关池内、网关未禁用、今日未耗尽、面板未异常、
    已知可用积分不为 0。"""
    if not acc.get("in_gateway"):
        return False
    if acc.get("gateway_disabled") or acc.get("gateway_exhausted"):
        return False
    if acc.get("status") in ("error", "disabled"):
        return False
    if acc.get("available") is not None and acc["available"] <= 0:
        return False
    return True


def _unhealthy(usage, name, model):
    """近窗口内该账号×模型失败率过高（样本 >=5 且失败 >30%）视为不健康。"""
    u = usage.get((name, model)) or {}
    req = u.get("requests", 0)
    return req >= 5 and (u.get("failed", 0) / req) > 0.3


def _allocate(expected, weighted, caps):
    """把 expected 按权重分配给 [(name, weight)]，单账号不超过 caps[name]（>0 才生效），
    溢出量迭代再分配给未触顶账号。返回 {name: suggested_int}。"""
    result = {n: 0.0 for n, _ in weighted}
    remaining = float(expected)
    active = [(n, w) for n, w in weighted if w > 0]
    for _ in range(4):
        if remaining <= 0 or not active:
            break
        wsum = sum(w for _, w in active) or 1.0
        nxt = []
        for n, w in active:
            give = remaining * w / wsum
            cap = caps.get(n) or 0
            room = (cap - result[n]) if cap > 0 else float("inf")
            take = min(give, max(room, 0.0))
            result[n] += take
            remaining -= take
            if cap <= 0 or result[n] < cap:
                nxt.append((n, w))
        active = nxt
    # 取整并把舍入差补给建议量最大的账号，保证总量贴近日均量
    out = {n: int(round(v)) for n, v in result.items()}
    drift = int(round(expected)) - sum(out.values())
    if drift and out:
        top = max(out, key=lambda k: out[k])
        out[top] = max(0, out[top] + drift)
    return out


def build_recommendation(accounts, usage, limits, window_days=7):
    accounts = [a for a in accounts if a.get("name")]
    by_name = {a["name"]: a for a in accounts}
    known_weights = []
    for a in accounts:
        avail, dl = a.get("available"), a.get("days_left")
        if avail is not None:
            known_weights.append(avail / max(dl, 1) if dl is not None else avail / 30.0)
    fallback = statistics.median(known_weights) if known_weights else 1.0
    weights = {a["name"]: _budget_weight(a, fallback) for a in accounts}

    models = sorted({m for (_a, m) in usage.keys()},
                    key=lambda m: -sum(v.get("requests", 0)
                                       for (a2, m2), v in usage.items() if m2 == m))
    risks = []
    model_recs = []
    per_account = {a["name"]: {"account": a["name"], "models": [],
                               "suggested_requests": 0}
                   for a in accounts}

    for model in models:
        total_req = sum(v.get("requests", 0)
                        for (a2, m2), v in usage.items() if m2 == model)
        expected = total_req / float(window_days)
        cands = [a for a in accounts if _is_candidate(a)]
        cands.sort(key=lambda a: (
            _unhealthy(usage, a["name"], model),
            a.get("days_left") is None,
            a.get("days_left") if a.get("days_left") is not None else 0,
            -weights[a["name"]],
        ))
        if not cands:
            risks.append({"type": "no_candidate", "model": model,
                          "message": "模型 %s 今天没有可用账号承接" % model})
            model_recs.append({"model": model, "expected_daily_requests": round(expected, 1),
                               "primary": None, "backup": None, "shares": []})
            continue
        if len(cands) == 1:
            risks.append({"type": "single_account", "model": model,
                          "account": cands[0]["name"],
                          "message": "模型 %s 只有账号「%s」可承接，无备份" % (model, cands[0]["name"])})
        caps = {}
        for a in cands:
            lim = (limits or {}).get("%s|%s" % (a["name"], model)) or {}
            caps[a["name"]] = lim.get("requests") or 0
        alloc = _allocate(expected, [(a["name"], weights[a["name"]]) for a in cands], caps)

        def _reasons(a, lead):
            rs = []
            dl = a.get("days_left")
            if dl is not None and dl <= 7:
                rs.append("积分 %s 天后到期，优先消耗" % dl)
            if a.get("available") is not None and a["available"] >= max(
                    (x.get("available") or 0) for x in cands):
                rs.append("可用积分最充裕")
            u = usage.get((a["name"], model)) or {}
            if u.get("requests", 0) >= 3 and not u.get("failed"):
                rs.append("近 %s 天该模型零失败" % window_days)
            if _unhealthy(usage, a["name"], model):
                rs.append("注意：近 %s 天该模型失败率偏高" % window_days)
            if not rs:
                rs.append("优先级与预算综合最优" if lead else "作为备选承接溢出")
            return rs

        shares = []
        for a in cands:
            sug = alloc.get(a["name"], 0)
            if sug <= 0 and expected > 0:
                continue
            shares.append({"account": a["name"], "suggested_requests": sug,
                           "share_pct": round(100.0 * sug / expected, 1) if expected else 0,
                           "limit_requests": caps.get(a["name"]) or 0})
            per_account[a["name"]]["models"].append(
                {"model": model, "suggested_requests": sug})
            per_account[a["name"]]["suggested_requests"] += sug
        primary = {"account": cands[0]["name"], "reasons": _reasons(cands[0], True)} \
            if cands else None
        backup = {"account": cands[1]["name"], "reasons": _reasons(cands[1], False)} \
            if len(cands) > 1 else None
        model_recs.append({"model": model, "expected_daily_requests": round(expected, 1),
                           "primary": primary, "backup": backup, "shares": shares})

    account_recs = []
    for a in accounts:
        name = a["name"]
        avail, dl = a.get("available"), a.get("days_left")
        rec = dict(per_account[name])
        rec.update({
            "available_credits": avail,
            "days_left": dl,
            "expire_at": a.get("expire_at"),
            "daily_credits_target": round(avail / dl, 1) if (avail is not None and dl) else None,
            "candidate": _is_candidate(a),
        })
        if not rec["candidate"]:
            if not a.get("in_gateway"):
                rec["excluded_reason"] = "未同步到网关池"
            elif a.get("gateway_disabled"):
                rec["excluded_reason"] = "网关侧已禁用"
            elif a.get("gateway_exhausted"):
                rec["excluded_reason"] = "网关侧今日已标记耗尽"
            elif a.get("status") == "error":
                rec["excluded_reason"] = "账号异常（令牌失效）"
            elif avail is not None and avail <= 0:
                rec["excluded_reason"] = "可用积分已耗尽"
            else:
                rec["excluded_reason"] = "已停用"
        account_recs.append(rec)
        if dl is not None and dl <= 3 and avail:
            risks.append({"type": "expiring", "account": name,
                          "message": "账号「%s」的 %.2f 可用积分 %s 天后到期，建议优先消耗"
                                     % (name, avail, dl)})
        if avail is not None and avail <= 0:
            risks.append({"type": "exhausted", "account": name,
                          "message": "账号「%s」可用积分已耗尽" % name})
        if a.get("status") == "error":
            risks.append({"type": "error", "account": name,
                          "message": "账号「%s」令牌异常，需要重新授权" % name})
    account_recs.sort(key=lambda r: (r["days_left"] is None,
                                     r["days_left"] if r["days_left"] is not None else 0,
                                     -r["suggested_requests"]))
    return {
        "goal": "expiry_first",
        "window_days": window_days,
        "models": model_recs,
        "accounts": account_recs,
        "risks": risks,
    }
