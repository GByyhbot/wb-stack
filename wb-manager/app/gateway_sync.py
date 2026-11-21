# -*- coding: utf-8 -*-
"""
面板 → 网关的账号文件同步（A1/A2 修复）。

背景（审计结论）：
- OAuth 入库把同一把【轮换型】refresh token 同时写进面板 DB 与网关
  accounts.json。两边各自刷新、各自只回写自己那份的话，必然有一边攥着
  上一代令牌，迟早互相刷死。约定：**谁刷新，谁负责把新令牌同步到另一份
  存储**。本模块负责「面板 → 网关」方向；反方向在 rotator 的
  _sync_panel_db（WB_DB_PATH 同机时生效）。
- accounts.json 的写者有多个进程（面板、网关、rotator CLI），整表
  读-改-写必须持有跨进程文件锁，否则旧快照会覆盖掉刚轮换的新令牌。
  锁文件与 rotator._accounts_file_lock 使用同一路径（accounts.json.lock），
  两边协议一致。
"""
import contextlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("wb-manager.gateway_sync")


def rotator_home():
    """rotator 数据目录：容器内由 WB_ROTATOR_HOME 指定，本地开发回退源码树"""
    home = os.environ.get("WB_ROTATOR_HOME", "").strip()
    if home:
        return Path(home)
    return Path(__file__).resolve().parent.parent.parent / "wb-rotator"


@contextlib.contextmanager
def gateway_file_lock():
    """持有 accounts.json 的跨进程锁，yield 账号文件路径。"""
    path = rotator_home() / "accounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


def _read_pool(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f) or {}
    if isinstance(raw, list):
        return raw, raw
    return raw.get("accounts") or [], raw


def _write_pool(path, accs, raw):
    out = accs if isinstance(raw, list) else {"accounts": accs}
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, str(path))


def find_pool_entry(accs, name):
    """按名找网关池条目：全等优先；前缀匹配仅在唯一命中时生效（B3 防串号）。"""
    if not name:
        return None
    exact = [a for a in accs if (a.get("name") or "") == name]
    if exact:
        return exact[0]
    hits = [a for a in accs if (a.get("name") or "") and
            ((a.get("name") or "").startswith(name) or name.startswith(a.get("name") or ""))]
    return hits[0] if len(hits) == 1 else None


def sync_gateway_rt(name, new_rt):
    """面板侧轮换出新令牌后，同步写入网关 accounts.json 的同名条目。

    返回 True 表示确实写入了新令牌。任何失败只记日志、不抛异常——
    同步是尽力而为，不能反过来把面板的签到/额度主流程带崩。
    """
    if not name or not new_rt:
        return False
    try:
        with gateway_file_lock() as path:
            if not path.is_file():
                return False
            accs, raw = _read_pool(path)
            target = find_pool_entry(accs, name)
            if target is None or target.get("refresh_token") == new_rt:
                return False
            target["refresh_token"] = new_rt
            _write_pool(path, accs, raw)
            log.info("新令牌已同步到网关池: %s", name)
            return True
    except Exception:
        log.exception("同步新令牌到网关池失败: %s", name)
        return False
