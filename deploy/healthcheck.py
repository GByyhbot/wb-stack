# -*- coding: utf-8 -*-
"""单容器健康检查（批次4 升级）：校验管理后台与网关各自的独立健康端点。

不再只做二元 200 探活：manager 走 /api/health、rotator 走 /health，
均校验返回体 status 字段，能如实反映进程内部状态（DB 异常/网关不可达
时探活失败，而非仅端口通就放行）。自动带上配置的鉴权头。
"""
import base64
import json
import os
import urllib.request


def check_health(port, path, headers):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s" % resp.status)
        body = json.loads(resp.read().decode("utf-8"))
        if body.get("status") != "ok":
            raise RuntimeError("status=%s %s" % (body.get("status"),
                                                  str(body.get("error", ""))[:120]))


user = os.environ.get("WB_BASIC_USER", "")
password = os.environ.get("WB_BASIC_PASSWORD", "")
mgr_headers = {}
if user and password:
    mgr_headers["Authorization"] = "Basic " + base64.b64encode(
        ("%s:%s" % (user, password)).encode("utf-8")
    ).decode("ascii")

key = os.environ.get("WB_ROTATOR_KEY", "")
gw_headers = {"Authorization": "Bearer " + key} if key else {}

check_health(int(os.environ.get("WB_PORT_MANAGER", "8300")), "/api/health", mgr_headers)
check_health(int(os.environ.get("WB_PORT_ROTATOR", "8310")), "/health", gw_headers)
