#!/usr/bin/env python3
"""Start the original UI, mirror bridge and plain nginx in an isolated runtime."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

from setup import PRIVATE, ROOT, RUNTIME, atomic, host, json_file, port_list


def nginx_configuration(config, runtime, listen, secret):
    port = config["port"]
    bridge = config["bridge_port"]
    ui = config["ui_port"]
    frame_listens = "\n".join("listen %s:%d ssl;" % (listen, p) for p in port_list(config))
    connect = "proxy_pass http://127.0.0.1:%d;" % bridge
    general_proxy = """
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 660s;
        proxy_send_timeout 660s;
    """
    bridge_proxy = general_proxy + ('\nproxy_set_header X-Mirror-Bridge "%s";\n' % secret)
    tls = """
        ssl_protocols TLSv1.2 TLSv1.3;
        client_max_body_size 100m;
        add_header X-Robots-Tag "noindex, nofollow" always;
    """
    if '"' in str(runtime) or "\n" in str(runtime):
        raise ValueError("运行目录含有不支持的字符。")
    if not re.fullmatch(r"[0-9a-f]{64}", secret):
        raise ValueError("运行密钥格式错误。")
    return f"""
worker_processes 1;
pid "{runtime}/nginx.pid";
error_log stderr crit;
events {{ worker_connections 1024; }}
http {{
    access_log off;
    client_body_temp_path "{runtime}/client-body";
    proxy_temp_path "{runtime}/proxy";
    fastcgi_temp_path "{runtime}/fastcgi";
    uwsgi_temp_path "{runtime}/uwsgi";
    scgi_temp_path "{runtime}/scgi";
    map $http_upgrade $connection_upgrade {{
        default upgrade;
        '' close;
    }}
    server {{
        listen {listen}:{port} ssl;
        server_name {host(config["host"])};
        ssl_certificate "{runtime}/main.crt";
        ssl_certificate_key "{runtime}/main.key";
        {tls}
        if ($host != "{host(config["host"])}") {{ return 421; }}
        add_header Referrer-Policy strict-origin-when-cross-origin always;
        location ^~ /manage-api/ {{ return 404; }}
        location ^~ /__mirror_content/ {{ return 404; }}
        location = /login_token {{ return 404; }}
        location ~ ^/code/(artifact|frame)/ {{ {connect} {bridge_proxy} }}
        location = /api/frame/sync {{
            {connect} {bridge_proxy}
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
        }}
        location ~ ^/(api|edge-api|v1|claudeai-rpc)/ {{ {connect} {bridge_proxy} }}
        location ^~ /mirror/ {{ {connect} {bridge_proxy} }}
        location = /login {{ {connect} {bridge_proxy} }}
        location / {{
            proxy_pass http://127.0.0.1:{ui};
            {general_proxy}
            proxy_set_header Accept-Encoding "";
            proxy_set_header X-Mirror-Bridge "";
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_redirect https://claude.ai/ /;
            proxy_hide_header Alt-Svc;
            sub_filter_once on;
            sub_filter '</body>' '<script defer src="/mirror/ui.js"></script></body>';
        }}
    }}
    server {{
        {frame_listens}
        server_name {host(config["frame_host"])};
        ssl_certificate "{runtime}/frame.crt";
        ssl_certificate_key "{runtime}/frame.key";
        {tls}
        if ($host != "{host(config["frame_host"])}") {{ return 421; }}
        gzip on;
        gzip_min_length 1024;
        gzip_types application/javascript text/css application/json application/wasm;
        location / {{
            proxy_pass http://127.0.0.1:{bridge}/__mirror_content/$server_port$request_uri;
            {bridge_proxy}
        }}
    }}
}}
"""


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--nginx", default=shutil.which("nginx") or "/usr/sbin/nginx")
    args.add_argument("--runtime", type=Path, default=RUNTIME)
    args.add_argument("--container", action="store_true")
    options = args.parse_args()
    runtime = options.runtime.resolve()
    config = json.loads((runtime / "settings.json").read_text())
    bind = "0.0.0.0" if options.container else config["bind"]
    accounts = json.loads((runtime / "accounts.json").read_text())
    json_file(runtime / "config.json", {
        "bind": "127.0.0.1:" + str(config["ui_port"]),
        "timeout": 600,
        "proxy_url": "",
        "real_logout": False,
        "site_password": (runtime / "site-password").read_text().strip(),
        "cookie_secret": (runtime / "site-cookie-secret").read_text().strip(),
        "moderation_enabled": True,
        "signup_enabled": False,
        "show_session_key": False,
    })
    settings = nginx_configuration(config, runtime, bind, accounts["bridge_secret"])
    atomic(runtime / "nginx.conf", settings.encode())
    nginx = [options.nginx, "-p", str(runtime), "-c", str(runtime / "nginx.conf")]
    subprocess.run(nginx + ["-t"], check=True)
    env = dict(os.environ, MIRROR_STATE_DIR=str(runtime),
               MIRROR_PARENT_ORIGIN="https://%s:%d" % (config["host"], config["port"]),
               MIRROR_FRAME_HOST=config["frame_host"],
               MIRROR_FRAME_SHELL=str(PRIVATE / "frame-shell.js"),
               MIRROR_FRAME_FIRST=str(config["frame_first"]), MIRROR_FRAME_COUNT=str(config["frame_count"]),
               MIRROR_UI_PORT=str(config["ui_port"]), PYTHONDONTWRITEBYTECODE="1")
    procs = []

    def finish(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, finish)
    try:
        procs.append(subprocess.Popen([str(PRIVATE / "fuclaude")], cwd=runtime,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        procs.append(subprocess.Popen([sys.executable, "-m", "gunicorn", "--bind",
                                       "127.0.0.1:" + str(config["bridge_port"]),
                                       "--workers", "1", "--threads", "8", "--timeout", "660",
                                       "--no-control-socket",
                                       "bridge:app"], cwd=ROOT / "mirror", env=env))
        procs.append(subprocess.Popen(nginx + ["-g", "daemon off; master_process off;"]))
        while True:
            if any(proc.poll() is not None for proc in procs):
                return 1
            time.sleep(0.3)
    except KeyboardInterrupt:
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    sys.exit(main())
