#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把中转站的真实计费同步进 CC Switch，让成本统计不再靠猜。

大多数中转（one-api / new-api 系）的 `GET /v1/usage` 会返回 `model_stats`，
每个模型都带：

    cost          按标价计算
    actual_cost   实际计费
    account_cost  账户扣费

用法：

    python3 sync_usage.py                          # 只看各中转的折算系数
    python3 sync_usage.py --write-pricing          # 只看折算后的单价
    python3 sync_usage.py --apply                  # 写 providers.cost_multiplier
    python3 sync_usage.py --write-pricing --apply  # 写 model_pricing（推荐）
    python3 sync_usage.py --field account_cost ... # 换用另一个字段

两种写法的区别（实测）：

  * `--write-pricing` 改的是 `model_pricing` 的单价，**CC Switch 确实会采用**，
    统计里的成本会直接跟着变。
  * `--apply` 改的是供应商的 `cost_multiplier`，但实测 CC Switch 记账时仍记 1.0，
    可能只认 `proxy_config.default_cost_multiplier`。保留它是为了完整性。

折算系数 = 中转报告的金额 / 按 `model_pricing` 标价算出的金额，按 token 量加权。
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROUTES = os.path.join(BASE, "routes.json")
DB = os.path.expanduser("~/.cc-switch/cc-switch.db")
FIELDS = ("actual_cost", "account_cost", "cost")


def usage(route):
    try:
        r = subprocess.run(["curl", "-sS", "-m", "25",
                            "-H", "Authorization: Bearer " + (route.get("auth") or ""),
                            route["upstream"] + "/v1/usage"],
                           capture_output=True, text=True)
        return json.loads(r.stdout)
    except Exception:
        return None


def load_pricing():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    out = {}
    for mid, i, o, cr, cc in conn.execute(
            "SELECT model_id, input_cost_per_million, output_cost_per_million,"
            " cache_read_cost_per_million, cache_creation_cost_per_million FROM model_pricing"):
        try:
            out[mid] = (float(i), float(o), float(cr), float(cc))
        except ValueError:
            pass
    conn.close()
    return out


def list_cost(model, ms, pricing):
    p = pricing.get(model)
    if not p:
        return None
    pin, pout, pcrd, pcrt = p
    return (ms.get("input_tokens", 0) / 1e6 * pin
            + ms.get("output_tokens", 0) / 1e6 * pout
            + ms.get("cache_read_tokens", 0) / 1e6 * pcrd
            + ms.get("cache_creation_tokens", 0) / 1e6 * pcrt)


def collect(routes, field):
    """-> (per_relay, per_model) aggregates of list price vs reported cost."""
    pricing = load_pricing()
    per_relay, per_model = [], {}
    for pid, route in routes.items():
        if route.get("auth_type") == "oauth":
            continue
        data = usage(route)
        if not data or not isinstance(data.get("model_stats"), list):
            per_relay.append((route["name"], None, 0.0, 0.0, 0))
            continue
        tl = tr = tok = 0
        for ms in data["model_stats"]:
            lc = list_cost(ms.get("model"), ms, pricing)
            if lc is None or lc <= 0:
                continue
            tl += lc
            tr += float(ms.get(field) or 0)
            tok += ms.get("total_tokens", 0)
            a = per_model.setdefault(ms["model"], {"list": 0.0, "real": 0.0, "tok": 0})
            a["list"] += lc
            a["real"] += float(ms.get(field) or 0)
            a["tok"] += ms.get("total_tokens", 0)
        per_relay.append((route["name"], pid, tl, tr, tok))
    return per_relay, per_model, pricing


def print_relays(per_relay, field):
    print("%-24s %12s %12s %12s %8s" % ("中转", "token量", "标价", field, "系数"))
    print("-" * 76)
    for name, pid, tl, tr, tok in sorted(per_relay, key=lambda r: -r[4]):
        if tl <= 0:
            print("%-24s %12d %12s" % (name[:24], tok, "无标价可比"))
        else:
            print("%-24s %12d %12.2f %12.2f %8.3f" % (name[:24], tok, tl, tr, tr / tl))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--write-pricing", action="store_true",
                    help="写 model_pricing 单价（CC Switch 确实会采用）")
    ap.add_argument("--field", default="actual_cost", choices=FIELDS)
    args = ap.parse_args()

    if not os.path.exists(ROUTES):
        sys.exit("缺 routes.json，先跑 setup.py")
    routes = json.load(open(ROUTES))["routes"]
    per_relay, per_model, pricing = collect(routes, args.field)

    print_relays(per_relay, args.field)

    if args.write_pricing:
        print("\n%-18s %14s %14s %8s   %s" % ("模型", "标价合计", args.field, "系数", "折算后 $/M"))
        print("-" * 88)
        plan = []
        for mid, a in sorted(per_model.items(), key=lambda kv: -kv[1]["tok"]):
            if a["list"] <= 0 or a["real"] <= 0:
                continue
            k = a["real"] / a["list"]
            if not (0 < k < 5):
                print("%-18s 系数异常 %.3f，跳过" % (mid, k))
                continue
            new = tuple(v * k for v in pricing[mid])
            plan.append((mid, new))
            print("%-18s %14.2f %14.2f %8.3f   in=%.4f out=%.4f cr=%.4f"
                  % (mid, a["list"], a["real"], k, new[0], new[1], new[2]))
        if args.apply and plan:
            conn = sqlite3.connect(DB)
            for mid, new in plan:
                conn.execute("UPDATE model_pricing SET input_cost_per_million=?,"
                             " output_cost_per_million=?, cache_read_cost_per_million=?,"
                             " cache_creation_cost_per_million=? WHERE model_id=?",
                             tuple("%.6f" % v for v in new) + (mid,))
            conn.commit()
            conn.close()
            print("\n已按真实计费折算 %d 个模型的单价。" % len(plan))
        elif plan:
            print("\n[dry-run] 加 --apply 才会写入。")
        return

    updates = [(n, pid, tr / tl) for n, pid, tl, tr, tok in per_relay
               if pid and tl > 0 and 0 < tr / tl < 100]
    if not args.apply:
        print("\n[dry-run] 会把上面各中转的系数写入 providers.cost_multiplier"
              "（%d 个）。加 --apply 执行。" % len(updates))
        print("提示：实测 CC Switch 记账仍记 1.0，推荐用 --write-pricing。")
        return
    conn = sqlite3.connect(DB)
    for name, pid, mult in updates:
        conn.execute("UPDATE providers SET cost_multiplier=? WHERE id=? AND app_type='codex'",
                     ("%.4f" % mult, pid))
    conn.commit()
    conn.close()
    print("\n已更新 %d 个供应商的成本倍率。" % len(updates))


if __name__ == "__main__":
    main()
