#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 本机多账号自动轮换代理 (wb-rotator)

作用：在本机 127.0.0.1 起 OpenAI 兼容网关，用账号池里的 WorkBuddy 账号
调用 glm-5.3-flash / deepseek-V4.1-flash。当前账号每日额度耗尽时，
自动切换到下一个账号继续，直到所有账号耗尽。

用法：
  python rotator.py serve --port 8310            # 启动代理
  python rotator.py add --name 备用1 --rt '<refresh_token>'
  python rotator.py list                         # 查看账号池与今日状态
  python rotator.py remove --name 备用1          # 移除账号
  python rotator.py reset [--name xx]            # 重置今日耗尽标记
  python rotator.py check                        # 立即查询所有账号积分余额

调用示例（任何 OpenAI SDK / curl）：
  curl http://127.0.0.1:8310/v1/chat/completions \\
    -H "Content-Type: application/json" \\
    -d '{"model":"glm-5.3-flash","messages":[{"role":"user","content":"hi"}]}'

接口约定：
  - 官方接口仅支持流式；代理对 stream=false 的请求内部聚合后返回完整 JSON。
  - 账号耗尽判定：调用返回 402/429 或错误体含额度关键字 → 查积分确认 →
    余额为 0 则标记当日耗尽并自动切换下一账号重试。
  - refresh token 为轮换型：每次刷新获得新令牌，代理自动回写 accounts.json。
  - 账号状态按「自然日」记录，跨天自动恢复，无需手动重置。
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 数据目录可通过环境变量外置（Docker 部署时指向挂载卷）；未设置时保持原有行为
HERE = os.environ.get("WB_ROTATOR_HOME") or os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")

# ---- 官方接口（2026-11-06 实测验证）----
REFRESH_URL = "https://copilot.tencent.com/v2/plugin/auth/token/refresh"
CHAT_URL = "https://copilot.tencent.com/v2/chat/completions"
STATUS_URL = "https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status"
# 真实可用积分接口（不带 /v2 前缀）。注意：WAF 会拦截 python/Node 默认 UA，
# 必须带浏览器形态的 User-Agent，否则返回 403 code=10085。
SUMMARY_URL = "https://www.codebuddy.cn/billing/meter/get-user-resource-summary"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) WorkBuddy/5.9.3 Chrome/136.0.0.0 Safari/537.36")

# 公网部署鉴权：设置 WB_ROTATOR_KEY 后，所有请求必须携带 Authorization: Bearer <key>。
# 未设置时不启用，保持原有局域网直连行为。
AUTH_KEY = os.environ.get("WB_ROTATOR_KEY", "")


def _require_auth(handler):
    if not AUTH_KEY or handler.headers.get("Authorization", "") == "Bearer " + AUTH_KEY:
        return True
    handler._json(401, {"error": {"message": "unauthorized"}})
    return False


# 模型白名单（可留空表示不限制）
MODEL_WHITELIST = {"glm-5.3-flash", "deepseek-v4.1-flash", "hy4free", "hy4flash-free"}

# 支持图片输入的模型（实测：构造 64x64 PNG 走 image_url 请求，两者均能正确识别）。
# /v1/models 必须显式声明，否则客户端按纯文本模型处理、直接禁用图片入口。
# hy4free 取自 WorkBuddy 产品配置：supportsImages=True，故同样声明视觉能力。
# hy4flash-free（Hy4 flash）产品配置 supportsImages=True，同入视觉集。
VISION_MODELS = {"glm-5.3-flash", "deepseek-v4.1-flash", "hy4free", "hy4flash-free"}

# 上下文窗口与最大输出。/v1/models 显式返回，供 OpenAI 兼容客户端读取；
# 拿不到这两个值的客户端会用自己的默认值（常见 1000k / 128k），
# 要么低估导致过早压缩上下文，要么高估导致请求超限。
# 数值取自官方产品配置中同名内置模型的定义；WorkBuddy 桌面客户端的三档
# 上下文选择器读的是本地 models.json 的 contextWindow 字段，不是这里的接口。
# 实测经 copilot.tencent.com/v2 入口：glm-5.3-flash 最大 prompt 629k、
# deepseek-v4.1-flash 最大 381k，均正常返回，与官方标称 1M 相符。
# hy4free 取自产品配置：contextWindow={300000,600000,960000}、
# maxInputTokens=960000、maxOutputTokens=64000（免费档上下文比旗舰档小）。
# hy4flash-free（Hy4 flash）取自产品配置：contextWindow={300000,600000,960000}、
# maxInputTokens=960000、maxOutputTokens=64000、credits=x0.00（免费档）、
# supportsImages/ToolCall/Reasoning=True、onlyReasoning=True（纯推理，思考不可关）。
MODEL_CONTEXT_WINDOW = 1000000
MODEL_CONTEXT_LENGTHS = [300000, 600000, 1000000]
MODEL_MAX_OUTPUT = {
    "glm-5.3-flash": 131072,
    "deepseek-v4.1-flash": 128000,
    "hy4free": 64000,
    "hy4flash-free": 64000,
}
# 按模型区分的上下文窗口（/v1/models 用，避免 hy4free 被错误标成 1M）
MODEL_CONTEXT_WINDOW_BY = {
    "hy4free": 960000,
    "hy4flash-free": 960000,
    "glm-5.3-flash": MODEL_CONTEXT_WINDOW,
    "deepseek-v4.1-flash": MODEL_CONTEXT_WINDOW,
}
MODEL_CONTEXT_LENGTHS_BY = {
    "hy4free": [300000, 600000, 960000],
    "hy4flash-free": [300000, 600000, 960000],
}

# 模型名标准化：官方 ID 大小写敏感，这里把常见写法归一到官方 ID
MODEL_ALIAS = {
    "glm-5.3-flash": "glm-5.3-flash",
    "deepseek-v4.1-flash": "deepseek-v4.1-flash",
    "deepseek-v4.1": "deepseek-v4.1-flash",
    # hy4 免费档（WorkBuddy 官方模型 id 大小写敏感，归一到 hy4free）
    "hy4free": "hy4free",
    "hy4-free": "hy4free",
    "hy4-free-trial-202611": "hy4free",
    # Hy4 flash：官方 id=hy4flash-free（产品配置 isDefault=true，免费档）；
    # 官方 aliases 含 hy4flash-dev，这里另收 hy4flash/hy4-flash 常见写法。
    "hy4flash-free": "hy4flash-free",
    "hy4flash": "hy4flash-free",
    "hy4-flash": "hy4flash-free",
    "hy4flash-dev": "hy4flash-free",
}

# 额度耗尽的错误特征
EXHAUST_HTTP_CODES = {402, 429}
EXHAUST_KEYWORDS = ("credit", "insufficient", "quota", "余额", "额度", "耗尽", "用尽")

MAX_SWITCH_RETRY = 5  # 单次请求最多切换账号次数

_lock = threading.RLock()


# ----------------------------------------------------------------------------
# 跨进程文件锁（A2）：accounts.json 的写者不止本进程——同容器的 wb-manager、
# 以及手动运行的 rotator CLI 都会整表读-改-写。线程锁只管本进程；
# 整表写回若基于旧快照，会把别的进程刚轮换的新令牌覆盖回旧值（令牌链断裂）。
# 所有「读-改-写」序列必须持有这把文件锁；单纯读取靠 tmp+replace 原子写保证不读半截。
# ----------------------------------------------------------------------------
import contextlib


@contextlib.contextmanager
def _accounts_file_lock():
    lock_path = ACCOUNTS_FILE + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


# ----------------------------------------------------------------------------
# 账号池配置管理
# ----------------------------------------------------------------------------
def load_accounts():
    with _lock:
        if not os.path.isfile(ACCOUNTS_FILE):
            return []
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            accounts = data.get("accounts", []) if isinstance(data, dict) else data
        except Exception:
            return []
        # B2: 选号顺序以 priority 字段为准，不依赖文件物理顺序。
        # 此前只有 rotator 自己的 add/serve 会把文件排好序，面板 OAuth 入库是
        # append 到末尾，新账号 priority 再高也排在最后被选。
        accounts = [a for a in accounts if isinstance(a, dict)]
        accounts.sort(key=lambda a: (a.get("priority", 100), a.get("name") or ""))
        return accounts


def save_accounts(accounts):
    with _lock:
        tmp = ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)


def today():
    return datetime.now().strftime("%Y-%m-%d")


def mask(t):
    return (t[:8] + "...") if t else "****"


def find_account(name):
    for a in load_accounts():
        if a.get("name") == name:
            return a
    return None


# ----------------------------------------------------------------------------
# 官方 API 封装
# ----------------------------------------------------------------------------
def refresh_access_token(rt):
    """长效令牌换接口令牌。返回 (ok, access_token, new_rt, msg)"""
    ok, at, new_rt, msg, _kind = refresh_access_token_ex(rt)
    return ok, at, new_rt, msg


class TokenAuthError(RuntimeError):
    """上游明确拒绝（令牌失效/被轮换）：账号需标记耗尽并等待人工更新令牌"""


class TokenNetworkError(RuntimeError):
    """网络异常/上游 5xx：临时故障，不应把健康账号标记耗尽"""


def refresh_access_token_ex(rt):
    """同 refresh_access_token，额外返回失败类型 kind ∈ {ok, auth, network}。

    B4: 此前调用方分不清「令牌死了」和「网络抖了一下」，一律整号标记当日耗尽，
    一次网络抖动就能让健康账号下线一整天。这里把判定收敛到一处：
    上游给出明确业务 code 的拒绝 = auth；连不上/超时/5xx 无业务码 = network。
    返回 (ok, access_token, new_rt, msg, kind)
    """
    req = urllib.request.Request(REFRESH_URL, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            kind = "network" if e.code >= 500 else "auth"
            return False, "", "", "HTTP %s" % e.code, kind
    except Exception as e:
        return False, "", "", str(e)[:120], "network"
    if body.get("code") == 0 and isinstance(body.get("data"), dict):
        d = body["data"]
        at = d.get("accessToken") or ""
        if at:
            return True, at, d.get("refreshToken") or "", "ok", "ok"
    return False, "", "", "code=%s %s" % (body.get("code"), (body.get("msg") or "")[:100]), "auth"


def query_credits(rt):
    """
    查询账号【可用积分】（真实剩余额度，而非签到接口的累计总积分）。
    接口: POST /billing/meter/get-user-resource-summary
    返回 data.TotalCredits = 各套餐 CycleRemainCapacity 之和（2026-11-06
    与客户端"可用积分"展示实测一致）。
    返回 (ok, credits, new_rt)；查询失败时 ok=False，调用方按未知处理。
    """
    ok, at, new_rt, msg = refresh_access_token(rt)
    if not ok:
        return False, None, ""
    req = urllib.request.Request(SUMMARY_URL, data=b"{}", method="POST")
    req.add_header("Authorization", "Bearer " + at)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", BROWSER_UA)  # 缺失会被 WAF 403
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception:
        return False, None, new_rt
    if body.get("code") == 0 and isinstance(body.get("data"), dict):
        avail = body["data"].get("TotalCredits")
        try:
            return True, float(avail), new_rt
        except (TypeError, ValueError):
            return False, None, new_rt
    return False, None, new_rt


def persist_rotation(acc, new_rt):
    """令牌轮换后回写配置文件"""
    if not new_rt or new_rt == acc.get("refresh_token"):
        return
    with _lock, _accounts_file_lock():
        accounts = load_accounts()
        for a in accounts:
            if a.get("name") == acc.get("name"):
                a["refresh_token"] = new_rt
                break
        save_accounts(accounts)
        acc["refresh_token"] = new_rt
    _sync_panel_db(acc.get("name"), new_rt)


def _sync_panel_db(name, new_rt):
    """A1: 网关轮换出的新令牌同步回面板 DB（同名精确匹配）。

    背景：OAuth 入库把同一把轮换型 RT 同时写进面板 DB 与网关 accounts.json，
    两边各自刷新、各自回写自己那份的话，必然有一边攥着上一代令牌互相刷死。
    约定：谁刷新谁负责把新令牌同步到另一份存储。本函数处理「网关 → 面板」方向
    （面板 → 网关方向在 wb-manager 的 app/gateway_sync.py）。
    仅在 WB_DB_PATH 指向真实文件时生效（Docker 单容器部署）；本机单跑网关时
    没有面板 DB，静默跳过。同步失败只记日志，不影响本次调用。
    """
    db_path = os.environ.get("WB_DB_PATH", "")
    if not name or not db_path or not os.path.isfile(db_path):
        return
    try:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            conn.execute("UPDATE accounts SET token=? WHERE name=?", (new_rt, name))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        sys.stderr.write("[rotator] 同步新令牌到面板 DB 失败(%s): %s\n" % (name, str(e)[:100]))


# ----------------------------------------------------------------------------
# 账号会话：缓存 access token
# ----------------------------------------------------------------------------
class AccountSession:
    """
    每个账号一个会话：只固定 name，其余字段（refresh_token / 状态）
    每次从 accounts.json 实时读取——保证 mark_exhausted / 手改配置立即生效。
    """

    def __init__(self, name):
        self.name = name
        self._at = ""
        self._at_time = 0

    def current(self):
        """实时获取该账号的最新配置条目；已被删除返回 None"""
        return find_account(self.name)

    def get_access_token(self):
        # 简单缓存 30 分钟，避免每请求都刷新
        if self._at and time.time() - self._at_time < 1800:
            return self._at
        acc = self.current()
        if not acc:
            raise RuntimeError("账号已被移除(%s)" % self.name)
        ok, at, new_rt, msg, kind = refresh_access_token_ex(acc["refresh_token"])
        if not ok:
            # B4: 只有上游明确拒绝才按令牌失效处理；网络故障单独抛型，
            # 由 _handle 决定「只跳过本次、不标记耗尽」。
            if kind == "auth":
                raise TokenAuthError("令牌刷新被拒(%s): %s" % (self.name, msg))
            raise TokenNetworkError("令牌刷新网络失败(%s): %s" % (self.name, msg))
        self._at = at
        self._at_time = time.time()
        persist_rotation(acc, new_rt)
        return at


def is_exhausted_response(http_code, body_text):
    """判断响应是否为额度耗尽类错误"""
    if http_code in EXHAUST_HTTP_CODES:
        return True
    low = (body_text or "").lower()
    return any(k in low for k in EXHAUST_KEYWORDS) and http_code >= 400


def confirm_exhausted(session):
    """二次确认：查积分余额，为 0 才标记耗尽（区分限流与真耗尽）"""
    try:
        acc = session.current()
        if not acc:
            return True  # 账号已删除，视作不可用
        ok, total, _ = query_credits(acc["refresh_token"])
        return ok and total is not None and total <= 0
    except Exception:
        # 查询失败时保守处理：不标记耗尽
        return False


def mark_exhausted(name):
    with _lock, _accounts_file_lock():
        accounts = load_accounts()
        for a in accounts:
            if a.get("name") == name:
                a["exhausted_date"] = today()
                break
        save_accounts(accounts)


def _sync_sessions():
    """让网关实时感知 accounts.json 里新增/删除的账号，无需重启容器。

    cmd_serve 启动时把账号池快照进 ProxyHandler.sessions；面板「官网授权登录」
    写入 accounts.json 后，若不同步，新账号要重启容器才会进入选号列表。
    仅在名字集合变化时重建，避免每请求都丢掉 access token 缓存。
    """
    try:
        accounts = load_accounts()
    except Exception:
        return
    names = [a.get("name") for a in accounts if a.get("name")]
    with _lock:
        if {s.name for s in ProxyHandler.sessions} == set(names):
            return
        ProxyHandler.sessions = [AccountSession(n) for n in names]
        sys.stderr.write("[rotator] 账号池已同步(%d): %s\n" %
                         (len(names), ", ".join(names)))


def pick_available_session(sessions, model=None, exclude=None):
    """
    选一个可用账号会话（状态实时读文件）。
    P2: model 传入时做「账号×模型」级过滤——某模型当日耗尽/限流冷却中的账号被跳过，
    不整号拉黑（借鉴 icebears111/workbuddy2api 的配额感知切号）。
    手动置顶（pin.json）的账号优先选用。
    exclude: 本次请求内已试过且失败的账号名集合，不再重复选（见 _handle）。
    """
    exclude = set(exclude or ())
    td = today()
    accounts = {a.get("name"): a for a in load_accounts()}
    pinned = get_pinned()

    def usable(s):
        acc = accounts.get(s.name)
        if not acc:
            return False  # 已被移除
        if s.name in exclude:
            return False  # 本次请求内已试过
        if acc.get("disabled"):
            return False
        if acc.get("exhausted_date") == td:
            return False
        if model and model_blocked(s.name, model):
            return False
        return True

    # 置顶优先（B3: 名字先经 resolve_name 唯一解析，前缀多命中时不置顶，防串号）
    if pinned:
        pin_name = resolve_name(pinned, [s.name for s in sessions])
        if pin_name:
            for s in sessions:
                if s.name == pin_name and usable(s):
                    return s
    for s in sessions:
        if usable(s):
            return s
    return None


# ---- P2: 账号×模型 独立状态（硬耗尽=当日模型额度尽；软冷却=429 限流临时避让） ----
MODEL_STATE_FILE = os.path.join(HERE, "model_state.json")
PIN_FILE = os.path.join(HERE, "pin.json")
SOFT_COOLDOWN_SECONDS = 600  # 429 限流软冷却 10 分钟
_ms_lock = threading.Lock()


def _load_model_state():
    try:
        with _ms_lock:
            return json.load(open(MODEL_STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def mark_model(name, model, kind):
    """kind='hard': 该账号该模型当日额度耗尽（跨自然日自动恢复）；
       kind='soft': 429 限流，冷却 SOFT_COOLDOWN_SECONDS 后自动恢复"""
    with _ms_lock:
        try:
            st = json.load(open(MODEL_STATE_FILE, encoding="utf-8"))
        except Exception:
            st = {}
        key = "%s|%s" % (name, model)
        if kind == "hard":
            st[key] = {"date": today(), "hard": True}
        else:
            st[key] = {"date": today(), "cooldown_until": time.time() + SOFT_COOLDOWN_SECONDS}
        # C3: 与 accounts.json 一致的原子写（tmp + replace），避免读到半截文件整表丢弃
        tmp = MODEL_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, MODEL_STATE_FILE)


def model_blocked(name, model):
    st = _load_model_state()
    e = st.get("%s|%s" % (name, model))
    if not e:
        return False
    if e.get("hard"):
        return e.get("date") == today()  # 跨日自动恢复
    return time.time() < e.get("cooldown_until", 0)


def get_pinned():
    """手动置顶的账号名（面板「设为当前」写入，持久生效直到再次设置）"""
    try:
        return (json.load(open(PIN_FILE, encoding="utf-8")) or {}).get("name")
    except Exception:
        return None


def _name_match(a, b):
    """面板名与池名宽容匹配：全等，或一方为另一方的前缀（如「主号(主人)」↔「主号」）"""
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def resolve_name(target, candidates):
    """B3: 在候选名集合中把 target 解析为唯一账号名。

    _name_match 的前缀规则在「备用1 / 备用10」这类命名下会同时命中多个账号，
    成对比较无法察觉。选号/置顶这类「按名找唯一账号」的路径必须先经这里：
    全等优先；前缀匹配仅在全局唯一命中时生效；多命中返回 None（宁可不匹配）。
    """
    if not target:
        return None
    cands = [c for c in candidates if c]
    if target in cands:
        return target
    hits = [c for c in cands if c.startswith(target) or target.startswith(c)]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------------------
# SSE 流转发与聚合
# ----------------------------------------------------------------------------
def forward_stream(at, payload_body):
    """转发到官方流式接口，返回 (http, raw_response_obj)"""
    req = urllib.request.Request(CHAT_URL, data=payload_body, method="POST")
    req.add_header("Authorization", "Bearer " + at)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    return urllib.request.urlopen(req, timeout=300)


def aggregate_stream(resp):
    """把官方 SSE 聚合为 OpenAI 非流式响应 JSON"""
    content_parts = []
    reasoning_parts = []
    tool_calls = {}
    finish_reason = "stop"
    usage = None
    model = ""
    cid = ""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        cid = cid or chunk.get("id", "")
        model = model or chunk.get("model", "")
        for ch in chunk.get("choices", []):
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            # C1: 工具调用分片按 index 拼装（id 只在首片出现，参数是增量字符串）。
            # 此前完全丢弃，stream=false + 工具调用时客户端只会拿到空回复。
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]
        if chunk.get("usage"):
            usage = chunk["usage"]
    message = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {
        "id": cid or "rotator",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {},
    }


# P1 请求记账：JSONL 一调用一行，保留 30 天（借鉴 workbuddy2api 系列的审计日志）
LOG_DIR = os.path.join(HERE, "logs")
USAGE_RETENTION_DAYS = 30
_prune_state = {"last": ""}


def log_request(account, model, status, dur_ms, t_in=None, t_out=None, error=None):
    """每次代理调用追加一条记录：时间/账号/模型/状态码/耗时/token 数"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        rec = {"ts": datetime.now().strftime("%F %T"), "account": account or "",
               "model": model or "", "status": status, "duration_ms": int(dur_ms),
               "prompt_tokens": t_in, "completion_tokens": t_out,
               "error": (error or "")[:200]}
        with open(os.path.join(LOG_DIR, "requests-%s.jsonl" % datetime.now().strftime("%Y%m")),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _prune_old_logs()
    except Exception:
        pass  # 记账失败不影响主流程


def _prune_old_logs():
    """每天最多执行一次：删除超过保留期的月度日志文件"""
    today_key = datetime.now().strftime("%F")
    if _prune_state.get("last") == today_key:
        return
    _prune_state["last"] = today_key
    cutoff = time.time() - USAGE_RETENTION_DAYS * 86400
    for fn in os.listdir(LOG_DIR):
        p = os.path.join(LOG_DIR, fn)
        try:
            if fn.startswith("requests-") and fn.endswith(".jsonl") and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except Exception:
            pass


def cmd_usage(args):
    """汇总请求日志：按 天/账号/模型 统计调用量与 token 数"""
    import collections, glob, datetime as _dt
    days = args.days
    since = (_dt.datetime.now() - _dt.timedelta(days=days)).strftime("%F")
    agg = collections.Counter()
    if not os.path.isdir(LOG_DIR):
        print("（暂无请求日志）")
        return
    for fp in sorted(glob.glob(os.path.join(LOG_DIR, "requests-*.jsonl"))):
        for line in open(fp, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("ts", "")[:10] < since:
                continue
            key = (r.get("ts", "")[:10], r.get("account", "?"), r.get("model", "?"))
            agg[key] += 1
            agg[(key, "tin")] += r.get("prompt_tokens") or 0
            agg[(key, "tout")] += r.get("completion_tokens") or 0
    if not agg:
        print("近 %d 天暂无请求记录" % days)
        return
    print("近 %d 天请求统计（日期 / 账号 / 模型：次数, prompt+completion tokens）" % days)
    cur_day = None
    total_req = total_in = total_out = 0
    for (day, acc, model) in sorted({k for k in agg if isinstance(k, tuple) and len(k) == 3}):
        if day != cur_day:
            cur_day = day
            print("── %s ──" % day)
        n = agg[(day, acc, model)]
        ti, to = agg[((day, acc, model), "tin")], agg[((day, acc, model), "tout")]
        total_req += n; total_in += ti; total_out += to
        print("  %-14s %-22s %4d 次  %8d + %8d tk" % (acc, model, n, ti, to))
    print("合计: %d 次, %d + %d tokens" % (total_req, total_in, total_out))


# ----------------------------------------------------------------------------
# HTTP 代理服务
# ----------------------------------------------------------------------------
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sessions = []          # 类属性：账号会话列表（serve 启动时构建）

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # P1 记账：所有 JSON 响应统一在此落账（流式成功在 _stream_response 落账）
        ctx = getattr(self, "_ctx", None)
        if ctx:
            err = ""
            if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
                err = str(obj["error"].get("message") or "")
            # 非流式响应由 aggregate_stream 聚合，usage 已随响应体返回，这里一并落账。
            # 此前未取 usage，导致 stream=false 的调用 token 恒为 null，用量统计偏低。
            u = obj.get("usage") if isinstance(obj, dict) else None
            u = u if isinstance(u, dict) else {}
            log_request(ctx.get("account"), ctx.get("model"), code,
                        (time.time() - ctx["t0"]) * 1000,
                        t_in=u.get("prompt_tokens"),
                        t_out=u.get("completion_tokens"),
                        error=err)

    # ---- 路由 ----
    def do_GET(self):
        if not _require_auth(self):
            return
        if self.path in ("/health", "/v1/models"):
            models = []
            for m in sorted(MODEL_WHITELIST):
                vision = m in VISION_MODELS
                # 每个模型用各自的上下文窗口，hy4free 与旗舰两档不同
                ctx_len = MODEL_CONTEXT_WINDOW_BY.get(m, MODEL_CONTEXT_WINDOW)
                ctx_opts = MODEL_CONTEXT_LENGTHS_BY.get(m, MODEL_CONTEXT_LENGTHS)
                models.append({
                    "id": m,
                    "object": "model",
                    "owned_by": "workbuddy",
                    # 两种常见能力描述同时给出，兼容不同客户端
                    "capabilities": {"vision": vision, "tool_calls": True, "streaming": True},
                    "architecture": {
                        "input_modalities": ["text", "image"] if vision else ["text"],
                        "output_modalities": ["text"],
                    },
                    # 上下文窗口元数据：缺省会让客户端退回自己的兜底值（常见 1000k）
                    "context_length": ctx_len,
                    "context_window_options": ctx_opts,
                    "max_input_tokens": ctx_len,
                    "max_output_tokens": MODEL_MAX_OUTPUT.get(m, 65536),
                })
            self._json(200, {"object": "list", "data": models,
                             "pinned": get_pinned(),
                             "accounts": self._pool_status()})
            return
        self._json(404, {"error": {"message": "not found"}})

    def _pool_status(self):
        td = today()
        pinned = get_pinned()
        pin_name = resolve_name(pinned, [s.name for s in self.sessions]) if pinned else None
        out = []
        for s in self.sessions:
            acc = s.current() or {}
            out.append({
                "name": s.name,
                "priority": acc.get("priority", 100),
                "exhausted_today": acc.get("exhausted_date") == td,
                "disabled": bool(acc.get("disabled")),
                "credits": acc.get("last_credits"),
                "pinned": bool(pin_name) and s.name == pin_name,
            })
        return out

    def _client_wants_stream(self):
        # 客户端请求体里的 stream 字段在转发前被覆盖，这里还原客户端原始意图
        return getattr(self, "_client_stream", False)

    def _stream_response(self, resp):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # HTTP 头仅支持 ASCII：中文名 URL 编码后放入。
        # 账号名取本次请求自己的记账上下文（_ctx），不用类级共享状态——
        # 并发请求下共享 state 会让响应头串到别的请求的账号上。
        account = (getattr(self, "_ctx", None) or {}).get("account") or ""
        self.send_header("X-Rotator-Account", urllib.parse.quote(account))
        self.end_headers()
        usage = None
        try:
            for raw in resp:
                self.wfile.write(raw)
                self.wfile.flush()
                # P1 记账：从 SSE 数据帧里抓最终 usage（上游在收尾帧返回）
                if usage is None and b'"usage"' in raw:
                    try:
                        for seg in raw.split(b"data:"):
                            seg = seg.strip()
                            if not seg or seg == b"[DONE]":
                                continue
                            chunk = json.loads(seg)
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                    except Exception:
                        pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            ctx = getattr(self, "_ctx", None)
            if ctx:
                u = usage or {}
                log_request(ctx.get("account"), ctx.get("model"), 200,
                            (time.time() - ctx["t0"]) * 1000,
                            t_in=u.get("prompt_tokens"),
                            t_out=u.get("completion_tokens"))


# 为还原客户端原始 stream 意图，重写读取逻辑（do_POST 在下方统一挂载）


def do_POST_wrapper(self):
    if not _require_auth(self):
        return
    try:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        obj = json.loads(raw)
        self._client_stream = bool(obj.get("stream"))
        # 把已读的数据塞回处理函数（用缓存 body 重放）
        self._raw_body = raw
    except Exception:
        self._client_stream = False
        self._raw_body = b"{}"
    _patched(self)


def _patched(self):
    """用缓存的请求体重走主逻辑（避免 rfile 已被消费）"""
    try:
        req_body = json.loads(self._raw_body or b"{}")
    except Exception:
        self._json(400, {"error": {"message": "invalid JSON body"}})
        return
    ProxyHandler._handle(self, req_body)


def _handle(self, req_body):
    _sync_sessions()   # 面板新授权/删除账号后立即生效，不必重启容器
    model = (req_body.get("model") or "").strip()
    model_l = model.lower()
    # 先归一别名：白名单判定必须在归一之后，否则 hy4-free-trial-202611 这类
    # 带连字符的别名会在归一前就被 400 拒掉，别名表形同虚设。
    model_norm = MODEL_ALIAS.get(model_l, model_l)
    # P1 记账上下文：本次请求的模型与计时起点
    self._ctx = {"model": model_norm, "t0": time.time(), "account": None}
    if MODEL_WHITELIST and model_norm not in MODEL_WHITELIST:
        self._json(400, {"error": {"message":
            "model '%s' 不在白名单（仅支持: %s）" % (model, ", ".join(sorted(MODEL_WHITELIST)))}})
        return
    # 标准化为官方模型 ID（官方大小写敏感）
    req_body["model"] = model_norm
    req_body["stream"] = True
    payload = json.dumps(req_body).encode("utf-8")
    want_stream = bool(getattr(self, "_client_stream", False))

    tried = []
    for _ in range(MAX_SWITCH_RETRY):
        session = pick_available_session(self.sessions, self._ctx["model"], exclude=tried)
        if session is None:
            self._json(503, {"error": {
                "type": "all_accounts_exhausted",
                "message": "所有账号今日额度已耗尽（已尝试: %s）。明日自动恢复，或执行 reset。" %
                           (", ".join(tried) or "无可用账号")}})
            return
        tried.append(session.name)
        self._ctx["account"] = session.name
        try:
            at = session.get_access_token()
        except TokenAuthError as e:
            # 令牌确已失效：标记耗尽（等人工更新令牌后 reset/check 恢复）
            sys.stderr.write("⚠️ %s，跳过该账号并标记耗尽\n" % e)
            mark_exhausted(session.name)
            continue
        except TokenNetworkError as e:
            # B4: 网络抖动只跳过本次请求，不把健康账号下线一整天。
            # 该账号名已在选号时记入 tried 并经 exclude 传回选号器，
            # 本次请求内不会再撞它（避免单账号池对着超时接口连撞数次才返回）。
            sys.stderr.write("⚠️ %s，本次跳过（不标记耗尽）\n" % e)
            continue
        except RuntimeError as e:
            sys.stderr.write("⚠️ %s，跳过该账号\n" % e)
            mark_exhausted(session.name)
            continue
        try:
            resp = forward_stream(at, payload)
        except urllib.error.HTTPError as e:
            err_text = ""
            try:
                err_text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if is_exhausted_response(e.code, err_text):
                cur = session.current() or {}
                if e.code == 429:
                    # P2 限流：只对（账号×模型）软冷却，本次请求切下一账号
                    mark_model(session.name, self._ctx["model"], "soft")
                    sys.stderr.write("⏳ %s/%s 限流，软冷却 %ds，切下一账号\n"
                                     % (session.name, self._ctx["model"], SOFT_COOLDOWN_SECONDS))
                    continue
                # 402/额度类错误：区分账号级（积分耗尽）与模型级（模型日额度耗尽）
                ok_c, credits, _ = query_credits(cur.get("refresh_token", ""))
                if ok_c and credits is not None and credits <= 0:
                    mark_exhausted(session.name)
                    sys.stderr.write("🔁 %s 可用积分耗尽（余额0确认），整号切换\n" % session.name)
                    continue
                if ok_c and credits is not None:
                    # 积分充足 → 是模型级日额度问题：仅标记该模型，不误伤同号其它模型
                    mark_model(session.name, self._ctx["model"], "hard")
                    sys.stderr.write("🔁 %s/%s 模型当日额度耗尽（积分尚余 %s），仅切该模型\n"
                                     % (session.name, self._ctx["model"], credits))
                    continue
                sys.stderr.write("⚠️ %s 返回 %s 但余额查询失败，保守返回错误\n" % (session.name, e.code))
            self._json(e.code, {"error": {"message": err_text[:500] or ("HTTP %s" % e.code)}})
            return
        except Exception as e:
            # 网络层瞬断（如大请求体 TLS EOF）：未向客户端输出任何内容，安全重试下一账号
            sys.stderr.write("⚠️ %s 网络错误: %s，自动重试下一账号\n" % (session.name, str(e)[:120]))
            self._last_net_err = str(e)[:200]
            continue

        if want_stream:
            self._stream_response(resp)
        else:
            try:
                result = aggregate_stream(resp)
            except Exception as e:
                self._json(502, {"error": {"message": "读取上游响应失败: %s" % str(e)[:150]}})
                return
            result["rotator"] = {"account": session.name}
            self._json(200, result)
        return

    self._json(503, {"error": {
        "type": "switch_limit_reached",
        "message": "连续切换 %d 个账号仍失败（已尝试: %s%s）" % (
            MAX_SWITCH_RETRY, ", ".join(tried),
            ("；最后网络错误: " + self._last_net_err) if getattr(self, "_last_net_err", None) else "")}})


ProxyHandler._handle = _handle
ProxyHandler.do_POST = do_POST_wrapper


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_serve(args):
    accounts = load_accounts()
    if not accounts:
        # 容器化部署时允许空池启动：健康检查仍可用，业务请求返回 503，
        # 导入账号后重启容器即可生效，避免 restart: always 陷入崩溃重启循环。
        print("⚠️ 账号池为空：服务已启动，业务请求将返回 503；导入账号后重启生效")
        accounts = []
    accounts.sort(key=lambda a: a.get("priority", 100))
    save_accounts(accounts)
    ProxyHandler.sessions = [AccountSession(a["name"]) for a in accounts]

    server = ThreadingHTTPServer((args.host, args.port), ProxyHandler)
    print("🚀 WorkBuddy 账号轮换代理已启动")
    print("   端点: http://%s:%d/v1/chat/completions" % (args.host, args.port))
    print("   模型: %s" % ", ".join(sorted(MODEL_WHITELIST)))
    print("   账号池(%d): %s" % (len(accounts),
          " → ".join(a["name"] + ("⭐" if a.get("priority", 100) == 1 else "") for a in accounts)))
    print("   切换策略: 额度耗尽自动切下一账号，跨日自动恢复")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def cmd_add(args):
    rt = (args.rt or "").strip()
    if not (len(rt) >= 200 and rt.count(".") == 2):
        print("❌ 内容不像有效的 refresh token（较长 base64 串，含两个点号）")
        sys.exit(1)
    with _lock, _accounts_file_lock():
        accounts = load_accounts()
        if any(a.get("name") == args.name for a in accounts):
            print("❌ 账号名已存在: %s" % args.name)
            sys.exit(1)
        # 新账号先验证可用性
        ok, at, new_rt, msg = refresh_access_token(rt)
        if not ok:
            print("❌ 令牌验证失败: %s" % msg)
            sys.exit(1)
        if new_rt:
            rt = new_rt  # 用轮换后的新令牌入库
        entry = {"name": args.name, "refresh_token": rt,
                 "priority": args.priority, "exhausted_date": ""}
        accounts.append(entry)
        accounts.sort(key=lambda a: a.get("priority", 100))
        save_accounts(accounts)
    print("✅ 账号已添加: %s (priority=%s) 令牌验证通过" % (args.name, args.priority))


def cmd_list(args):
    td = today()
    accounts = load_accounts()
    if not accounts:
        print("（账号池为空）")
        return
    print(f"{'优先级':<7}{'名称':<14}{'今日状态':<12}{'可用积分':<10}{'令牌'}")
    for a in accounts:
        st = "⛔ 已耗尽" if a.get("exhausted_date") == td else \
             ("🚫 已禁用" if a.get("disabled") else "✅ 可用")
        print(f"{a.get('priority', 100):<7}{a.get('name', ''):<14}{st:<12}"
              f"{str(a.get('last_credits', '?')):<10}{mask(a.get('refresh_token'))}")


def cmd_remove(args):
    with _lock, _accounts_file_lock():
        accounts = load_accounts()
        new_list = [a for a in accounts if a.get("name") != args.name]
        if len(new_list) == len(accounts):
            print("❌ 未找到账号: %s" % args.name)
            sys.exit(1)
        save_accounts(new_list)
    print("✅ 已移除: %s" % args.name)


def cmd_reset(args):
    with _lock, _accounts_file_lock():
        accounts = load_accounts()
        n = 0
        for a in accounts:
            if args.name and a.get("name") != args.name:
                continue
            if a.get("exhausted_date"):
                a["exhausted_date"] = ""
                n += 1
        save_accounts(accounts)
    print("✅ 已重置 %d 个账号的今日耗尽标记" % n)


def cmd_check(args):
    accounts = load_accounts()
    td = today()
    for a in accounts:
        if a.get("exhausted_date") == td and not args.force:
            print(f"⏭️ {a.get('name')}: 今日已标记耗尽（--force 可强制查询）")
            continue
        ok, total, new_rt = query_credits(a.get("refresh_token"))
        if new_rt:
            persist_rotation(a, new_rt)
        if ok:
            # 更新余额缓存（整段读-改-写持有跨进程文件锁，防与服务进程互相覆盖）
            with _lock, _accounts_file_lock():
                accs = load_accounts()
                for x in accs:
                    if x.get("name") == a.get("name"):
                        x["last_credits"] = total
                        if total is not None and total > 0 and x.get("exhausted_date") == td:
                            x["exhausted_date"] = ""  # 余额恢复则解除标记
                        break
                save_accounts(accs)
            state = "✅" if (total or 0) > 0 else "⛔ 耗尽"
            print(f"{state} {a.get('name')}: 可用积分 {total}")
            if total is not None and total <= 0:
                mark_exhausted(a.get("name"))
        else:
            print(f"❌ {a.get('name')}: 查询失败")


def main():
    parser = argparse.ArgumentParser(description="WorkBuddy 本机多账号自动轮换代理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="启动代理服务")
    p.add_argument("--port", type=int, default=8310)
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址（NAS 部署用 0.0.0.0 供局域网访问）")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("add", help="添加账号")
    p.add_argument("--name", required=True)
    p.add_argument("--rt", required=True, help="refresh token（长效令牌）")
    p.add_argument("--priority", type=int, default=100, help="数字越小越先用")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="查看账号池状态")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("remove", help="移除账号")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("reset", help="重置今日耗尽标记")
    p.add_argument("--name", default=None)
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("check", help="立即查询所有账号积分余额")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("usage", help="查看近 N 天请求用量统计（默认 7 天）")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_usage)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
