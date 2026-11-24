# -*- coding: utf-8 -*-
"""二期第 2 批：模型额度监控面板的状态合并（纯函数，只读、无副作用）。

优先级严格按施工令第 4 条：禁用 > 真耗尽 > 冷却中 > 达阈值 > 预警 > 未知额度 > 正常。
预警 80% 只是展示分级，绝不参与选号逻辑；未知额度不推算百分比。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

# 显示状态 -> 固定徽章文案（前端按 key 上色，不在这里写色值）
STATUS_LABELS = {
    "disabled": "禁用",
    "exhausted": "已耗尽",
    "cooldown": "冷却中",
    "threshold": "达阈值",
    "warning": "预警",
    "unknown": "未知额度",
    "normal": "正常",
}

WARNING_RATIO = 0.8  # 纯展示：达到安全阈值的 80% 标预警，不影响引擎


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def merge_cell_status(cell: Dict[str, Any], now_ts: Optional[float] = None) -> Dict[str, Any]:
    """把引擎快照格与现行 model_state 状态合并成面板展示状态。

    返回字段直接 update 到原格：status（展示 key）、status_label、remaining_seconds、
    percent（未知额度为 None）、engine_status（原引擎状态，保留便于排查）。
    不得对入参做写操作。
    """
    now = time.time() if now_ts is None else now_ts
    engine_status = cell.get("engine_status") or cell.get("status") or ""

    disabled = bool(cell.get("disabled")) or engine_status == "disabled"
    exhausted = (bool(cell.get("exhausted_today"))
                 or bool(cell.get("account_exhausted_today"))
                 or engine_status in ("exhausted", "credits_exhausted"))

    cooldown_until = _as_number(cell.get("soft_cooldown_until"))
    remaining = None
    if cooldown_until is not None and cooldown_until > now:
        remaining = int(math.ceil(cooldown_until - now))
    in_cooldown = remaining is not None or engine_status == "cooldown"

    safe_req = _as_number(cell.get("safe_request_limit"))
    safe_tok = _as_number(cell.get("safe_token_limit"))
    used_req = (_as_number(cell.get("success_count")) or 0) + (_as_number(cell.get("in_flight")) or 0)
    used_tok = _as_number(cell.get("tokens_used")) or 0
    limit_known = safe_req is not None or safe_tok is not None

    at_threshold = engine_status == "threshold_reached"
    warning = False
    if limit_known:
        if safe_req is not None and safe_req > 0 and used_req >= math.ceil(safe_req * WARNING_RATIO):
            warning = True
        if safe_tok is not None and safe_tok > 0 and used_tok >= math.ceil(safe_tok * WARNING_RATIO):
            warning = True

    if disabled:
        level = "disabled"
    elif exhausted:
        level = "exhausted"
    elif in_cooldown:
        level = "cooldown"
    elif at_threshold:
        level = "threshold"
    elif warning:
        level = "warning"
    elif not limit_known:
        level = "unknown"
    else:
        level = "normal"

    # 百分比只在限额已知时给出；请求限额优先，其次 token 限额
    percent = None
    if safe_req is not None and safe_req > 0:
        percent = round(used_req / safe_req * 100)
    elif safe_tok is not None and safe_tok > 0:
        percent = round(used_tok / safe_tok * 100)

    return {
        "engine_status": engine_status,
        "status": level,
        "status_label": STATUS_LABELS[level],
        "remaining_seconds": remaining,
        "percent": percent,
    }
