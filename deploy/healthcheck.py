# -*- coding: utf-8 -*-
"""单容器健康检查：同时验证管理后台与网关（自动带上配置的鉴权头）。"""
import base64
import os
import urllib.request


def check(port, path, headers):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError("HTTP %s" % resp.status)
        resp.read()


user = os.environ.get("WB_BASIC_USER", "")
password = os.environ.get("WB_BASIC_PASSWORD", "")
mgr_headers = {}
if user and password:
    mgr_headers["Authorization"] = "Basic " + base64.b64encode(
        ("%s:%s" % (user, password)).encode("utf-8")
    ).decode("ascii")

key = os.environ.get("WB_ROTATOR_KEY", "")
gw_headers = {"Authorization": "Bearer " + key} if key else {}

check(int(os.environ.get("WB_PORT_MANAGER", "8300")), "/api/settings", mgr_headers)
check(int(os.environ.get("WB_PORT_ROTATOR", "8310")), "/health", gw_headers)
