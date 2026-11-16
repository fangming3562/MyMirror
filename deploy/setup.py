#!/usr/bin/env python3
"""Create independent runtime files and verify the pinned mirror dependencies."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "private"
RUNTIME = ROOT / "runtime"


def digest(file):
    value = hashlib.sha256()
    for part in iter(lambda: file.read(1024 * 1024), b""):
        value.update(part)
    return value.hexdigest()


def validate_file(path, expected):
    with path.open("rb") as file:
        if digest(file) != expected:
            raise ValueError("校验值不符: " + path.name)


def host(value):
    if not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise ValueError("请输入 IPv4 地址或 ASCII 域名。")
    return value.lower()


def port_list(config):
    main = [config[k] for k in ("port", "ui_port", "bridge_port")]
    count = config["frame_count"]
    if not 1 <= count <= 256:
        raise ValueError("文件预览来源数量应为 1 到 256。")
    frame = list(range(config["frame_first"], config["frame_first"] + count))
    ports = main + frame
    if any(not 1024 <= port <= 65535 for port in ports) or len(ports) != len(set(ports)):
        raise ValueError("端口必须在 1024 到 65535 之间，且不能相互重叠。")
    return frame


def atomic(path, data, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        filename = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            filename.chmod(mode)
            filename.replace(path)
        finally:
            filename.unlink(missing_ok=True)


def json_file(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def make_certs(config, args):
    for prefix, name, directory in (
        ("main", config["host"], args.tls_dir),
        ("frame", config["frame_host"], args.frame_tls_dir or args.tls_dir),
    ):
        cert = RUNTIME / (prefix + ".crt")
        key = RUNTIME / (prefix + ".key")
        if directory:
            atomic(cert, (directory / "fullchain.pem").read_bytes())
            atomic(key, (directory / "privkey.pem").read_bytes())
        elif prefix == "frame" and name == config["host"]:
            atomic(cert, (RUNTIME / "main.crt").read_bytes())
            atomic(key, (RUNTIME / "main.key").read_bytes())
        elif not (cert.is_file() and key.is_file()):
            try:
                ipaddress.IPv4Address(name)
                kind = "IP"
            except ValueError:
                kind = "DNS"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-days", "30", "-subj", "/CN=" + name,
                            "-addext", "subjectAltName=" + kind + ":" + name,
                            "-keyout", str(key), "-out", str(cert)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            key.chmod(0o600)
            cert.chmod(0o600)


def init(args):
    config = {k: getattr(args, k) for k in ("port", "ui_port", "bridge_port", "frame_first", "frame_count")}
    config["host"] = host(args.host)
    config["frame_host"] = host(args.frame_host or args.host)
    config["bind"] = str(ipaddress.IPv4Address(args.bind))
    frames = port_list(config)
    if not ipaddress.IPv4Address(config["bind"]).is_loopback:
        if not args.tls_dir or config["frame_host"] == config["host"]:
            raise ValueError("开放到非回环地址需要 HTTPS 证书，并须为文件预览设置独立域名。")
    if (RUNTIME / "settings.json").exists():
        previous = json.loads((RUNTIME / "settings.json").read_text())
        if previous != config:
            raise ValueError("此目录已初始化为不同设置，请使用新的工作目录。")
    else:
        print("初始化一个空白账号库。")
    reference = json.loads((ROOT / "manifest.json").read_text())
    if not (PRIVATE / "frame-shell.js").is_file():
        raise ValueError("请先将有权使用的原版文件预览资源放入 private/frame-shell.js。")
    validate_file(PRIVATE / "frame-shell.js", reference["overlay"]["frame-shell.js"])
    if not (PRIVATE / "fuclaude").is_file():
        raise ValueError("请将有权使用的 Linux x86-64 二进制放入 private/fuclaude。")
    validate_file(PRIVATE / "fuclaude", reference["binary"]["fuclaude"])
    (PRIVATE / "fuclaude").chmod(0o755)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    make_certs(config, args)
    account_file = RUNTIME / "accounts.json"
    if not account_file.exists():
        json_file(account_file, {"bridge_secret": secrets.token_hex(32), "default_account": None, "accounts": []})
    if not (RUNTIME / "site-password").exists():
        atomic(RUNTIME / "site-password", (secrets.token_urlsafe(24) + "\n").encode())
    if not (RUNTIME / "site-cookie-secret").exists():
        atomic(RUNTIME / "site-cookie-secret", (secrets.token_hex(32) + "\n").encode())
    json_file(RUNTIME / "settings.json", config)
    compose_env = {"MIRROR_BIND": config["bind"], "MIRROR_PORT": config["port"],
                   "MIRROR_FRAME_FIRST": frames[0], "MIRROR_FRAME_LAST": frames[-1]}
    atomic(RUNTIME / "compose.env", "".join("%s=%s\n" % item for item in compose_env.items()).encode())
    print("站点入口：https://%s:%s" % (config["host"], config["port"]))
    print("站点密码保存在 runtime/site-password。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--frame-host")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", default=9443, type=int)
    parser.add_argument("--ui-port", default=8181, type=int)
    parser.add_argument("--bridge-port", default=8183, type=int)
    parser.add_argument("--frame-first", default=11443, type=int)
    parser.add_argument("--frame-count", default=32, type=int)
    parser.add_argument("--tls-dir", type=Path)
    parser.add_argument("--frame-tls-dir", type=Path)
    try:
        init(parser.parse_args())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit("准备失败：" + str(exc))


if __name__ == "__main__":
    main()
