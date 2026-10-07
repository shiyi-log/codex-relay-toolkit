#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run one `codex-relay` sidecar per chat-completions-only upstream.

Why: our bridge speaks the OpenAI **Responses** API (what Codex uses). Plenty of
cheap providers (DeepSeek, Kimi, Qwen, GLM, OpenRouter-backed ones …) only speak
**Chat Completions**. `codex-relay` translates one direction (Responses in, chat
completions out) and has no retry/failover of its own — which is exactly the
division of labour: sidecars translate, the bridge does pricing/latency/failover.

Workflow:

    python3 sidecar.py --install          # fetch the codex-relay binary from PyPI
    $EDITOR sidecars.json                 # declare upstreams (+ model_map, price)
    python3 sidecar.py --start            # spawn the sidecars
    python3 setup.py                      # merge them into routes.json
    python3 sidecar.py --status
    python3 sidecar.py --stop

Requires: nothing but Python 3 and curl to install; `codex-relay` itself is a
Rust binary (no Rust toolchain needed, the wheel ships it).
"""

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import zipfile
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("BRIDGE_SIDECARS", os.path.join(BASE, "sidecars.json"))
BIN_DIR = os.path.join(BASE, "bin")
BIN = os.environ.get("BRIDGE_CODEX_RELAY", os.path.join(BIN_DIR, "codex-relay"))
PID_DIR = os.path.join(BASE, "run")
RUN_LOG_DIR = BASE
PYPI_JSON = "https://pypi.org/pypi/codex-relay/json"


def platform_tag():
    """The wheel tag for this machine, e.g. macosx_11_0_arm64."""
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "macosx_11_0_arm64" if machine in ("arm64", "aarch64") else "macosx_10_12_x86_64"
    if sys.platform.startswith("linux"):
        return "manylinux_2_17_aarch64" if machine in ("arm64", "aarch64") else "manylinux_2_17_x86_64"
    return "win_arm64" if machine in ("arm64", "aarch64") else "win_amd64"


def install(url=None, force=False):
    if os.path.exists(BIN) and not force:
        print("已存在：%s（要覆盖加 --force）" % BIN)
        return 0
    os.makedirs(BIN_DIR, exist_ok=True)
    if url:
        wheel_url = url
    else:
        tag = platform_tag()
        with urllib.request.urlopen(PYPI_JSON, timeout=30) as fh:
            meta = json.load(fh)
        files = meta["urls"]
        match = [f for f in files if f["filename"].endswith(tag + ".whl")]
        if not match:
            wheels = ", ".join(f["filename"] for f in files if f["filename"].endswith(".whl"))
            sys.exit("没有匹配 %s 的 wheel，可选：%s" % (tag, wheels))
        wheel_url = match[0]["url"]
        print("codex-relay %s (%s)" % (meta["info"]["version"], match[0]["filename"]))
    tmp = os.path.join(BIN_DIR, "codex-relay.whl")
    print("下载 %s" % wheel_url)
    urllib.request.urlretrieve(wheel_url, tmp)
    found = None
    with zipfile.ZipFile(tmp) as zf:
        for name in zf.namelist():
            if name.endswith(".data/scripts/codex-relay") or name.endswith("codex-relay"):
                if "/scripts/" in name:
                    zf.extract(name, BIN_DIR)
                    found = os.path.join(BIN_DIR, name)
                    break
    os.remove(tmp)
    if not found:
        sys.exit("wheel 里没找到 codex-relay 可执行文件")
    os.rename(found, BIN)
    os.chmod(BIN, 0o755)
    # clean the empty scaffolding the extract left behind
    shutil.rmtree(os.path.join(BIN_DIR, os.path.dirname(found).split("/")[0]), ignore_errors=True)
    print("已安装：%s" % BIN)
    return 0


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("没有 %s —— 先写一个（参考 sidecars.example.json）" % CONFIG)
    with open(CONFIG) as fh:
        data = json.load(fh)
    relays = data.get("relays") or []
    if not isinstance(relays, list):
        sys.exit("%s 里的 relays 必须是数组" % CONFIG)
    return relays


def pid_file(relay_id):
    return os.path.join(PID_DIR, "sidecar-%s.pid" % relay_id)


def running(relay_id):
    path = pid_file(relay_id)
    try:
        with open(path) as fh:
            pid = int(fh.read().strip())
    except Exception:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def start(args):
    if not os.path.exists(BIN):
        sys.exit("缺 codex-relay 二进制：先跑 python3 sidecar.py --install")
    os.makedirs(PID_DIR, exist_ok=True)
    started = 0
    for relay in load_config():
        rid = str(relay.get("id") or "").strip()
        if not rid or not relay.get("port") or not relay.get("upstream"):
            print("  跳过配置不完整的条目：%r" % relay)
            continue
        if running(rid):
            print("  已在运行：%-14s pid %s" % (rid, running(rid)))
            continue
        cmd = [BIN, "--port", str(relay["port"]), "--upstream", relay["upstream"]]
        if relay.get("api_key"):
            cmd += ["--api-key", relay["api_key"]]
        if relay.get("bind"):
            cmd += ["--bind", relay["bind"]]
        if relay.get("extra_params"):
            cmd += ["--upstream-extra-params", json.dumps(relay["extra_params"])]
        if relay.get("drop_params"):
            cmd += ["--drop-upstream-params", json.dumps(relay["drop_params"])]
        log_path = os.path.join(RUN_LOG_DIR, "sidecar-%s.log" % rid)
        log = open(log_path, "a")
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        with open(pid_file(rid), "w") as fh:
            fh.write(str(proc.pid))
        started += 1
        print("  启动 %-14s pid %-7s port %-6s -> %s（日志 %s）"
              % (rid, proc.pid, relay["port"], relay["upstream"], os.path.basename(log_path)))
    time.sleep(1.0)
    return status(args, quiet=False) if started else 0


def stop(args):
    stopped = 0
    for relay in load_config():
        rid = str(relay.get("id") or "").strip()
        pid = running(rid)
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
            print("  停止 %-14s pid %s" % (rid, pid))
        except OSError as exc:
            print("  停止 %-14s 失败：%s" % (rid, exc))
        try:
            os.remove(pid_file(rid))
        except OSError:
            pass
    if not stopped:
        print("  没有在运行的 sidecar")
    return 0


def probe(port):
    """Ask the sidecar what it is (it answers /v1/models)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % port, timeout=3) as fh:
            data = json.load(fh)
        ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
        return ("%d models" % len(ids)) if ids else "reachable"
    except Exception as exc:
        return "unreachable (%s)" % str(exc)[:40]


def status(args, quiet=True):
    relays = load_config()
    print("%-16s %-8s %-8s %-30s %s" % ("sidecar", "pid", "port", "upstream", "状态"))
    print("-" * 92)
    for relay in relays:
        rid = str(relay.get("id") or "?")
        pid = running(rid)
        port = relay.get("port")
        state = "running" if pid else "stopped"
        if pid and not quiet:
            state += " · " + probe(port)
        elif pid:
            state += " · " + probe(port)
        print("%-16s %-8s %-8s %-30s %s"
              % (rid, pid or "-", port, str(relay.get("upstream"))[:30], state))
    print()
    print("配置：%s\n二进制：%s%s" % (CONFIG, BIN, "" if os.path.exists(BIN) else "  (未安装：--install)"))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", nargs="?", const="", metavar="WHEEL_URL",
                    help="安装 codex-relay 二进制（默认从 PyPI 取本平台 wheel）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.install is not None:
        return install(args.install or None, force=args.force)
    if args.stop:
        return stop(args)
    if args.start:
        return start(args)
    return status(args)


if __name__ == "__main__":
    sys.exit(main())
