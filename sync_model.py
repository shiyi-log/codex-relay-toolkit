#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动跟随最新模型：不要写死模型名。

查询每个中转的 /v1/models，挑出「同一家族里版本最高、且被足够多中转支持」的模型，
然后写进两处，保证它不会被硬编码成旧版本：

  1. CC Switch 数据库里各供应商的 settings_config.config
  2. ~/.codex/config.toml

判断规则：

  * 家族（默认 `sol`，可用 BRIDGE_MODEL_FAMILY 改）取自当前正在用的模型，
    没有就取支持面最广的那个家族
  * 版本按 `gpt-<大版本>[.<小版本>]-<家族>` 排序，取最高
  * 支持率必须 >= BRIDGE_MODEL_MIN_SUPPORT（默认 0.5）。
    用「大多数」而不是「全部」，是因为个别中转会掉队（比如只有它没有 6.1），
    不应该让整个池子陪它退到旧版本
  * 一个模型都不支持的中转会被列出来——它们只会白白消耗轮询次数

用法：

    python3 sync_model.py            # 只看会选哪个，不改任何东西
    python3 sync_model.py --apply    # 真正写入（会先停 CC Switch）
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
ROUTES = os.path.join(BASE, "routes.json")
DB = os.path.expanduser("~/.cc-switch/cc-switch.db")
CONFIG = os.path.expanduser("~/.codex/config.toml")

FAMILY = os.environ.get("BRIDGE_MODEL_FAMILY", "")
MIN_SUPPORT = float(os.environ.get("BRIDGE_MODEL_MIN_SUPPORT", "0.5"))
VERSION_RE = re.compile(r"^gpt-(\d+)(?:\.(\d+))?-([a-z0-9.]+)$")


def relay_models():
    """{relay_name: set(model ids)} for every bearer-auth relay."""
    routes = json.load(open(ROUTES))["routes"]
    out = {}
    for pid, r in routes.items():
        if r.get("auth_type") == "oauth":
            continue
        url = r["upstream"] + (r.get("prefix") or "") + "/models"
        try:
            # some relays 403 on the default python-urllib UA
            req = urllib.request.Request(url, headers={
                "Authorization": "Bearer " + (r.get("auth") or ""),
                "User-Agent": "curl/8.7.1",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15) as fh:
                out[r["name"]] = {m.get("id") for m in (json.load(fh).get("data") or []) if m.get("id")}
        except Exception as exc:
            out[r["name"]] = set()
            print("  ! %-26s 查询失败: %s" % (r["name"][:26], exc))
    return out


def parse(mid):
    m = VERSION_RE.match(mid)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), m.group(3))


def current_model():
    try:
        with open(CONFIG) as fh:
            m = re.search(r'^model\s*=\s*"([^"]+)"', fh.read(), re.M)
            return m.group(1) if m else None
    except OSError:
        return None


def pick(models, family):
    """Highest version in `family` served by at least MIN_SUPPORT of the relays."""
    reachable = [n for n, ids in models.items() if ids]
    if not reachable:
        return None, {}
    counts = {}
    for name in reachable:
        for mid in models[name]:
            counts[mid] = counts.get(mid, 0) + 1
    fam = family
    if not fam:
        fam = (parse(current_model()) or (0, 0, ""))[2] or None
    cands = []
    for mid, n in counts.items():
        p = parse(mid)
        if not p:
            continue
        if fam and p[2] != fam:
            continue
        cands.append((p[0], p[1], n, mid))
    cands.sort(reverse=True)
    need = max(1, int(len(reachable) * MIN_SUPPORT + 0.999))
    for major, minor, n, mid in cands:
        if n >= need:
            return mid, counts
    return None, counts


def switch_off():
    if subprocess.run(["pgrep", "-f", "CC Switch.app/Contents/MacOS/cc-switch"],
                      capture_output=True).returncode == 0:
        subprocess.run(["pkill", "-TERM", "-f", "CC Switch.app/Contents/MacOS/cc-switch"])
        import time
        for _ in range(15):
            if subprocess.run(["pgrep", "-f", "CC Switch.app/Contents/MacOS/cc-switch"],
                              capture_output=True).returncode != 0:
                break
            time.sleep(1)
        return True
    return False


def apply(model, apply_db, apply_cfg):
    changed = []
    if apply_cfg:
        shutil.copy(CONFIG, CONFIG + ".bak-syncmodel")
        text = open(CONFIG).read()
        new, n = re.subn(r'^model\s*=\s*"[^"]+"', 'model = "%s"' % model, text, count=1, flags=re.M)
        if n:
            open(CONFIG, "w").write(new)
            changed.append(CONFIG)
    if apply_db:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT id, name, settings_config FROM providers WHERE app_type='codex'"
            " AND (category IS NULL OR category<>'official')").fetchall()
        for pid, name, raw in rows:
            cfg = json.loads(raw)
            conf = cfg.get("config") or ""
            new, n = re.subn(r'^model\s*=\s*"[^"]+"', 'model = "%s"' % model, conf, count=1, flags=re.M)
            if n and new != conf:
                cfg["config"] = new
                conn.execute("UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex'",
                             (json.dumps(cfg, ensure_ascii=False), pid))
                changed.append("CC Switch: " + name)
        conn.commit()
        conn.close()
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--family", default=FAMILY)
    args = ap.parse_args()

    if not os.path.exists(ROUTES):
        sys.exit("routes.json 不存在，先跑 setup.py")

    print("正在查询各中转的模型列表…")
    models = relay_models()
    cur = current_model()
    model, counts = pick(models, args.family)
    print("\n当前配置里的模型 : %s" % cur)
    if not model:
        sys.exit("没有找到满足支持率要求的候选模型")

    fam = parse(model)[2]
    print("家族              : %s" % fam)
    print("选定              : %s" % model)
    served = sum(1 for n in models if model in models[n])
    print("支持它的中转      : %d / %d" % (served, len(models)))
    print("\n  支持:")
    for n in sorted(models):
        if model in models[n]:
            print("    ✅ %s" % n)
    missing = [n for n in sorted(models) if model not in models[n]]
    if missing:
        print("  不支持（会白耗轮询次数，建议移出队列）:")
        for n in missing:
            print("    ❌ %s" % n)

    if model == cur:
        print("\n已经是最新，无需改动。")
        return
    if not args.apply:
        print("\n[dry-run] 会写入 %s（以及 CC Switch 各供应商配置）。加 --apply 执行。" % CONFIG)
        return

    stopped = switch_off()
    print("\nCC Switch %s" % ("已停止" if stopped else "本来就没运行"))
    for c in apply(model, True, True):
        print("  已更新 %s" % c)
    print("\n改完了。请重新打开 CC Switch，新会话即使用 %s 走中转。" % model)


if __name__ == "__main__":
    main()
