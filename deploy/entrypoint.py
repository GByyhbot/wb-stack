# -*- coding: utf-8 -*-
"""单容器启动器：同时运行管理后台(8300)与账号轮换网关(8310)。

任一子进程异常退出时容器以非零码退出，交给 restart 策略拉起；
收到 SIGTERM(docker stop) 时优雅终止子进程并以 0 退出。
"""
import os
import signal
import subprocess
import sys
import time

WORKDIR = "/app"
_children = []


def spawn(label, cmd):
    print("[entrypoint] 启动 %s: %s" % (label, " ".join(cmd)), flush=True)
    p = subprocess.Popen(cmd, cwd=WORKDIR, env=os.environ.copy())
    _children.append((label, p))
    return p


def terminate_all():
    for _label, p in _children:
        if p.poll() is None:
            p.terminate()
    for _label, p in _children:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def on_signal(*_args):
    terminate_all()
    sys.exit(0)


def main():
    port_manager = os.environ.get("WB_PORT_MANAGER", "8300")
    port_rotator = os.environ.get("WB_PORT_ROTATOR", "8310")
    spawn("manager", [sys.executable, "-m", "uvicorn", "app.main:app",
                      "--host", "0.0.0.0", "--port", port_manager])
    spawn("rotator", [sys.executable, "rotator.py", "serve",
                      "--host", "0.0.0.0", "--port", port_rotator])

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    while True:
        for label, p in _children:
            rc = p.poll()
            if rc is not None:
                print("[entrypoint] %s 异常退出(code=%s)，容器退出等待重启" % (label, rc),
                      flush=True)
                terminate_all()
                sys.exit(1)
        time.sleep(2)


if __name__ == "__main__":
    main()
