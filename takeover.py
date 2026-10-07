#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep Codex traffic on the bridge: detect and undo CC Switch's "hand back to
the official account" takeover.

What happens without this: every time the bridge restarts (deploy, crash, sleep),
all of CC Switch's bridged providers fail at once. CC Switch then decides the
proxy has nothing healthy left, hands the client back to the **official ChatGPT
login**, and stays there. From that moment Codex talks to `chatgpt.com` directly
and the bridge is out of the loop - silently, because everything still works
(just on your subscription quota, with none of the pricing/failover logic).

This tool notices that state and restores the takeover:

    python3 takeover.py --status        # what is Codex actually talking to?
    python3 takeover.py --fix           # restore the bridge takeover
    python3 takeover.py --watch         # loop (run it from launchd)

Wanted to use your own account on purpose? Then park this tool:

    touch TAKEOVER.DISABLED             # in this directory; --fix does nothing
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "15888"))
BRIDGE_STATUS = os.environ.get("BRIDGE_STATUS",
                               "http://127.0.0.1:%d/__bridge/status" % BRIDGE_PORT)
SETTINGS = os.environ.get("CCSWITCH_SETTINGS", os.path.expanduser("~/.cc-switch/settings.json"))
DB = os.environ.get("BRIDGE_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))
CODEX_CONFIG = os.environ.get("CODEX_CONFIG", os.path.expanduser("~/.codex/config.toml"))
CC_LOG = os.environ.get("CCSWITCH_LOG", os.path.expanduser("~/.cc-switch/logs/cc-switch.log"))
DISABLED = os.path.join(BASE, "TAKEOVER.DISABLED")
LOG = os.path.join(BASE, "takeover.log")
APP = os.environ.get("CCSWITCH_APP", "CC Switch")
GRACE = float(os.environ.get("BRIDGE_TAKEOVER_GRACE", "15"))
RESTART_COOLDOWN = float(os.environ.get("BRIDGE_TAKEOVER_RESTART_COOLDOWN", "900"))
STATE = os.environ.get("BRIDGE_TAKEOVER_STATE", os.path.join(BASE, "takeover-state.json"))


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        with open(LOG, "a") as fh:
            fh.write(line)
    except Exception:
        pass


def bridge_status():
    """-> (healthy, routes) where routes is the bridge's own ranking."""
    try:
        with urllib.request.urlopen(BRIDGE_STATUS, timeout=5) as fh:
            data = json.load(fh)
        return True, data.get("routes") or []
    except Exception:
        return False, []


def providers():
    """-> {id: {"name", "config", "bridged"}} from CC Switch's database."""
    out = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        for pid, name, cfg in conn.execute(
                "SELECT id, name, settings_config FROM providers WHERE app_type='codex'"):
            try:
                data = json.loads(cfg)
            except Exception:
                continue
            out[pid] = {"name": name, "config": data.get("config") or ""}
        conn.close()
    except Exception as exc:
        log("读取 CC Switch 数据库失败：%s" % exc)
    return out


def is_bridged(config_text):
    return ("127.0.0.1:%d/p/" % BRIDGE_PORT) in (config_text or "")


def pick_provider(routes, provs):
    """The best bridge route that CC Switch also has as a provider.

    Skips the official account (CC Switch's handoff target) and sidecar routes
    (they have no CC Switch provider), and prefers the bridge's own ranking.
    """
    for row in routes:
        pid, name = row.get("id"), row.get("name")
        if not pid or row.get("auth_type") == "oauth" or pid.startswith("sidecar-"):
            continue
        entry = provs.get(pid)
        if entry and is_bridged(entry["config"]):
            return pid, entry["name"]
    for pid, entry in provs.items():
        if not pid.startswith("sidecar-") and is_bridged(entry["config"]):
            return pid, entry["name"]
    return None, None


def current_provider():
    try:
        with open(SETTINGS) as fh:
            return (json.load(fh) or {}).get("currentProviderCodex")
    except Exception:
        return None


def codex_base_url():
    try:
        with open(CODEX_CONFIG) as fh:
            text = fh.read()
    except Exception:
        return None
    urls = re.findall(r'base_url\s*=\s*"([^"]+)"', text)
    return urls[0] if urls else None


def set_current_provider(pid):
    with open(SETTINGS) as fh:
        data = json.load(fh)
    data["currentProviderCodex"] = pid
    tmp = SETTINGS + ".takeover-tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS)
    try:
        os.chmod(SETTINGS, 0o600)
    except OSError:
        pass


def log_size():
    try:
        return os.path.getsize(CC_LOG)
    except OSError:
        return 0


def last_restart():
    try:
        with open(STATE) as fh:
            return float((json.load(fh) or {}).get("last_restart") or 0)
    except Exception:
        return 0.0


def note_restart():
    try:
        with open(STATE, "w") as fh:
            json.dump({"last_restart": time.time()}, fh)
    except Exception:
        pass


def restart_app():
    note_restart()
    log("重启 %s 让它重新接管…" % APP)
    subprocess.run(["osascript", "-e", 'quit app "%s"' % APP], capture_output=True)
    for _ in range(20):
        time.sleep(0.5)
        if subprocess.run(["pgrep", "-f", APP], capture_output=True).returncode != 0:
            break
    subprocess.run(["pkill", "-f", "%s.app" % APP], capture_output=True)
    time.sleep(1)
    subprocess.run(["open", "-a", "/Applications/%s.app" % APP], capture_output=True)


def takeover_active():
    """Did CC Switch re-take-over the client config, i.e. is Codex on the proxy?"""
    base = codex_base_url() or ""
    return "127.0.0.1:%d" % 15721 in base or ("127.0.0.1:%d/p/" % BRIDGE_PORT) in base


def fix(args):
    if os.path.exists(DISABLED):
        log("存在 TAKEOVER.DISABLED，跳过（你自己想用官方账号）")
        return 0
    healthy, routes = bridge_status()
    provs = providers()
    pid = current_provider()
    entry = provs.get(pid or "", {})
    bridged_now = is_bridged(entry.get("config"))
    log("状态：桥%s，当前供应商 %s（%s），Codex base_url %s"
        % ("健康" if healthy else "不可用", entry.get("name") or pid,
           "走桥" if bridged_now else "非桥", codex_base_url()))
    if not healthy:
        log("桥不可用，先不动（等桥恢复）")
        return 0
    if bridged_now and takeover_active():
        if not args.quiet:
            log("已经在走桥，无需处理")
        return 0
    target, name = pick_provider(routes, provs)
    if not target:
        log("找不到已桥接的供应商，无法恢复")
        return 1
    before = log_size()
    set_current_provider(target)
    log("已把 currentProviderCodex 改成 %s（%s），等 CC Switch 反应…" % (name, target[:8]))
    deadline = time.time() + GRACE
    while time.time() < deadline:
        time.sleep(1)
        if takeover_active():
            log("CC Switch 已重新接管，Codex 现在走 %s" % codex_base_url())
            return 0
    if args.no_restart:
        log("CC Switch 没有自动反应；--no-restart 指定不重启，请手动在界面里选一次供应商")
        return 1
    waited = time.time() - last_restart()
    if waited < RESTART_COOLDOWN:
        log("CC Switch 没反应，但距上次重启只有 %.0fs（冷却 %.0fs）——只提示不重启，"
            "请手动在界面里选一次供应商" % (waited, RESTART_COOLDOWN))
        return 1
    restart_app()
    deadline = time.time() + GRACE
    while time.time() < deadline:
        time.sleep(1)
        if takeover_active():
            log("重启后已接管，Codex 现在走 %s" % codex_base_url())
            return 0
    log("仍未接管，请检查 %s" % CC_LOG)
    return 1


def status(args):
    healthy, routes = bridge_status()
    provs = providers()
    pid = current_provider()
    entry = provs.get(pid or "", {})
    print("桥             : %s（%d 条路由）" % ("健康" if healthy else "不可用", len(routes)))
    print("CC Switch 当前 : %s（%s）" % (entry.get("name") or pid, pid))
    print("  指向桥        : %s" % ("是" if is_bridged(entry.get("config")) else "否"))
    print("Codex base_url : %s" % codex_base_url())
    print("代理接管中     : %s" % ("是" if takeover_active() else "否 ← 流量没走桥"))
    if os.path.exists(DISABLED):
        print("TAKEOVER.DISABLED: 存在（--fix 不会动作）")
    target, name = pick_provider(routes, provs)
    print("建议切回       : %s" % (("%s（%s）" % (name, target)) if target else "无"))
    return 0


def watch(args):
    every = max(15.0, args.every)
    log("watch 启动：每 %.0fs 检查一次（Ctrl-C 退出）" % every)
    while True:
        try:
            fix(args)
        except Exception as exc:                                   # pragma: no cover
            log("检查出错：%r" % exc)
        time.sleep(every)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--every", type=float, default=60.0)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-restart", action="store_true",
                    help="不要重启 CC Switch，只改设置并提示")
    args = ap.parse_args()
    if args.watch:
        return watch(args)
    if args.fix:
        return fix(args)
    return status(args)


if __name__ == "__main__":
    sys.exit(main())
