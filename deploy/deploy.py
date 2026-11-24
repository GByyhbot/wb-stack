# -*- coding: utf-8 -*-
"""部署单容器版 WorkBuddy 服务栈（管理后台 8300 + 网关 8310）。

用法：
  python deploy.py --dry-run      # 仅列出将上传的文件，不连服务器
  python deploy.py                # 上传并启动（需 paramiko）

目标主机可用环境变量覆盖：ECS_HOST / ECS_USER / ECS_PASSWORD / REMOTE_DIR。
NAS 上 docker 需要提权时设置 REMOTE_SUDO=1。

设计要点：
  - 单容器：entrypoint.py 同时拉起 uvicorn 与 rotator，鉴权内置在应用里，无需独立 nginx。
  - 端口沿用项目原端口 8300 / 8310；公网由 frp 中继（ECS 只转发，不存令牌）。
  - 管理后台 HTTP Basic，网关固定 Bearer Key；密钥写在 deploy/.env，由 compose 读取。
  - 数据（wb_manager.db / accounts.json / pin.json / logs）挂在 deploy/data/，重建容器不丢。
  - 镜像需预先构建并以 wb-single:latest 载入目标主机；compose 使用 pull_policy: never。
"""
import argparse
import io
import os
import secrets
import shlex
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV_FILE = os.path.join(HERE, ".env")

DEFAULTS = {
    "ECS_HOST": "101.32.244.225",
    "ECS_USER": "root",
    "ECS_PASSWORD": "",
    "REMOTE_DIR": "/opt/wb-stack",
    "MANAGER_USER": "wbadmin",
    "MANAGER_PASSWORD": "",
    "ROTATOR_KEY": "",
}


def load_env():
    cfg = dict(DEFAULTS)
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    changed = False
    for key in ("MANAGER_PASSWORD", "ROTATOR_KEY"):
        if not cfg[key]:
            cfg[key] = secrets.token_urlsafe(24)
            changed = True
    # SSH 密码优先从进程环境变量读取，不落盘
    if not cfg.get("ECS_PASSWORD"):
        cfg["ECS_PASSWORD"] = os.environ.get("ECS_PASSWORD", "")
    for key in ("ECS_HOST", "ECS_USER", "REMOTE_DIR"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    if changed:
        # .env 只保存 compose 需要的鉴权变量，不写入任何主机/SSH 信息
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for k in ("MANAGER_USER", "MANAGER_PASSWORD", "ROTATOR_KEY"):
                f.write(f"{k}={cfg.get(k, '')}\n")
        print(f"已生成随机密钥并写入 {ENV_FILE}")
    return cfg


def build_archive():
    """白名单打包；.env 含鉴权密钥需上传，但被 .dockerignore 排除在镜像构建上下文之外。"""
    files = [
        ".dockerignore",
        "wb-manager/requirements.txt",
        "wb-rotator/rotator.py",
        "wb-rotator/quota_engine.py",
        "deploy/Dockerfile",
        "deploy/Dockerfile.hotfix",
        "deploy/docker-compose.yml",
        "deploy/entrypoint.py",
        "deploy/healthcheck.py",
        "deploy/restore_drill.py",
        "deploy/.env.example",
    ]
    if os.path.exists(ENV_FILE):
        files.append("deploy/.env")
    for sub in ("app", "static"):
        base = os.path.join(ROOT, "wb-manager", sub)
        for dirpath, _, names in os.walk(base):
            for name in names:
                if name.endswith((".pyc", ".db")):
                    continue
                full = os.path.join(dirpath, name)
                files.append(os.path.relpath(full, ROOT).replace("\\", "/"))

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel in files:
            full = os.path.join(ROOT, rel)
            if os.path.isfile(full):
                tf.add(full, arcname=rel)
    buf.seek(0)
    return buf.getvalue(), files


def deploy(cfg, hotfix=False):
    try:
        import paramiko
    except ImportError:
        print("缺少 paramiko，请先 pip install paramiko", file=sys.stderr)
        return 1

    host, user, pwd = cfg["ECS_HOST"], cfg["ECS_USER"], cfg["ECS_PASSWORD"]
    if not pwd:
        print("ECS_PASSWORD 未配置，拒绝部署。", file=sys.stderr)
        return 1
    remote = cfg["REMOTE_DIR"].rstrip("/")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, 22, username=user, password=pwd, timeout=20)
    sftp = cli.open_sftp()

    payload, files = build_archive()
    with sftp.open("/tmp/wb-stack.tgz", "wb") as f:
        f.write(payload)
    print(f"已上传部署包（{len(files)} 个文件，{len(payload)} 字节）")

    use_sudo = os.environ.get("REMOTE_SUDO") == "1"
    # 增量构建：以上一版镜像为基底只覆盖代码层，不拉镜像、不装依赖，秒级完成。
    # 构建上下文必须是 $REMOTE_DIR（脚手架代码所在地），这里用绝对路径避免受 cd 影响。
    build_cmd = (
        f"docker build -f {shlex.quote(remote)}/deploy/Dockerfile.hotfix "
        f"-t wb-single:latest {shlex.quote(remote)}"
    )
    up_cmd = "docker compose up -d --force-recreate --remove-orphans"
    ps_cmd = "docker compose ps"

    steps = ([build_cmd] if hotfix else []) + [up_cmd, ps_cmd]
    if use_sudo:
        docker_up = " && ".join(
            f"echo {shlex.quote(pwd)} | sudo -S -p '' {s}" for s in steps
        )
    else:
        docker_up = "; ".join(steps)

    obsolete = " ".join(
        f"{shlex.quote(remote)}/deploy/{name}" for name in
        ("nginx.conf", "nginx.conf.template", "htpasswd",
         "Dockerfile.manager", "Dockerfile.rotator")
    )
    script = (
        f"set -e; "
        f"mkdir -p {shlex.quote(remote)}/deploy/data/manager {shlex.quote(remote)}/deploy/data/rotator; "
        f"tar -xzf /tmp/wb-stack.tgz -C {shlex.quote(remote)}; "
        f"if [ ! -f {shlex.quote(remote)}/deploy/data/rotator/accounts.json ]; then "
        f"printf '%s\\n' '{{\"accounts\": []}}' > {shlex.quote(remote)}/deploy/data/rotator/accounts.json; fi; "
        f"rm -f {obsolete}; "
        f"chmod -R 700 {shlex.quote(remote)}/deploy/data; "
        f"chmod 600 {shlex.quote(remote)}/deploy/.env; "
        f"cd {shlex.quote(remote)}/deploy && {docker_up}"
    )
    _in, out, err = cli.exec_command(script, timeout=600)
    text = out.read().decode("utf-8", "replace")
    error = err.read().decode("utf-8", "replace")
    if text:
        print(text)
    if error.strip():
        print("stderr:", error[-2000:], file=sys.stderr)
    rc = out.channel.recv_exit_status()
    sftp.close()
    cli.close()

    if rc == 0:
        print("\n=== 访问方式（端口沿用原 8300 / 8310）===")
        print(f"管理后台: http://{host}:8300/  用户 {cfg['MANAGER_USER']} / 密码见 deploy/.env")
        print(f"网关地址: http://{host}:8310/v1  Bearer Key 见 deploy/.env 的 ROTATOR_KEY")
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="仅列出将上传的文件")
    ap.add_argument("--hotfix", action="store_true",
                    help="只改动了代码时使用：以现网镜像为基底增量重建（无需拉基础镜像）")
    args = ap.parse_args()
    cfg = load_env()
    payload, files = build_archive()
    print("将上传 %d 个文件（%d 字节）：" % (len(files), len(payload)))
    for rel in files:
        print("  " + rel)
    if args.dry_run:
        return 0
    return deploy(cfg, hotfix=args.hotfix)


if __name__ == "__main__":
    sys.exit(main())
