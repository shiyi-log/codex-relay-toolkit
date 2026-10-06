#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查中转站的真实余额和真实计费，并同步进 CC Switch。

大多数中转（one-api / new-api 系）的 `GET /v1/usage` 会返回：

    balance / remaining   余额
    model_stats[]         每个模型的用量，且带三个金额字段：
        cost          按标价计算
        actual_cost   实际计费
        account_cost  账户扣费

用法：

    python3 sync_usage.py --balance                # 余额查询（各中转真实余额 + 地址绑定）
    python3 sync_usage.py                          # 只看各中转的折算系数
    python3 sync_usage.py --write-pricing          # 只看折算后的单价
    python3 sync_usage.py --apply                  # 写 providers.cost_multiplier
    python3 sync_usage.py --write-pricing --apply  # 写 model_pricing（推荐）
    python3 sync_usage.py --field account_cost ... # 换用另一个字段

`--balance` 除了打印每个中转的真实余额，还会对比 CC Switch 里该供应商的
`usage_script.baseUrl`：**复制出来的中转会继承源中转的余额地址**，于是界面
上显示的是别家的余额。这一列直接把这种错误标出来（跑 setup.py 修）。

两种写法的区别（实测）：

  * `--write-pricing` 改的是 `model_pricing` 的单价，**CC Switch 确实会采用**，
    统计里的成本会直接跟着变。
  * `--apply` 改的是供应商的 `cost_multiplier`，但实测 CC Switch 记账时仍记 1.0，
    可能只认 `proxy_config.default_cost_multiplier`。保留它是为了完整性。

折算系数 = 中转报告的金额 / 按 `model_pricing` 标价算出的金额，按 token 量加权。
每个中转报告的金额一律取它**自己的**地址（routes.json 里的 upstream + 该中转的
prefix），所以某个中转 401 / 404 不会再被静默当成"没有数据"。
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROUTES = os.environ.get("BRIDGE_ROUTES", os.path.join(BASE, "routes.json"))
DB = os.environ.get("BRIDGE_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))
FIELDS = ("actual_cost", "account_cost", "cost")


def usage_urls(route):
    """Candidate usage endpoints for one relay, most specific first."""
    prefix = (route.get("prefix") or "").rstrip("/")
    paths = []
    for path in (prefix + "/usage", "/v1/usage"):
        if path not in paths:
            paths.append(path)
    return [route["upstream"] + path for path in paths]


def usage(route):
    """GET this relay's own usage endpoint.

    -> (status, data, note); status is an int or None for a transport error,
    data is the parsed JSON body when there is one. The relay's *own* key is
    used, and the URL follows that relay's real base path, so a relay that only
    serves /v1 (or only the site root) is queried correctly.
    """
    last = (None, None, "no attempt")
    for url in usage_urls(route):
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-m", "25", "-w", "\n%{http_code}",
                 "-H", "Authorization: Bearer " + (route.get("auth") or ""),
                 # some relays 403 the default curl-less UA / python-urllib
                 "-H", "User-Agent: curl/8.7.1",
                 url],
                capture_output=True, text=True)
        except Exception as exc:                       # pragma: no cover
            last = (None, None, str(exc))
            continue
        raw = proc.stdout or ""
        body, _, tail = raw.rpartition("\n")
        try:
            status = int(tail.strip())
        except ValueError:
            body, status = raw, None
        try:
            data = json.loads(body)
        except Exception:
            data = None
        note = (proc.stderr or "").strip().splitlines()[-1] if proc.stderr else ""
        last = (status, data, note[:90])
        if status == 200 and isinstance(data, dict):
            return last
    return last


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


def provider_usage_base():
    """provider id -> usage_script.baseUrl as currently stored in CC Switch."""
    out = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
        for pid, meta in conn.execute(
                "SELECT id, meta FROM providers WHERE app_type='codex'"):
            try:
                out[pid] = ((json.loads(meta) or {}).get("usage_script") or {}).get("baseUrl")
            except Exception:
                pass
        conn.close()
    except Exception:
        pass
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
    """-> (per_relay, per_model, pricing); per_relay rows carry a status note."""
    pricing = load_pricing()
    per_relay, per_model = [], {}
    for pid, route in routes.items():
        if route.get("auth_type") == "oauth":
            per_relay.append((route["name"], None, 0.0, 0.0, 0, "oauth 账号，跳过"))
            continue
        status, data, note = usage(route)
        if status is None:
            per_relay.append((route["name"], None, 0.0, 0.0, 0,
                              "查询失败 %s" % (note or "")))
            continue
        if status != 200 or not isinstance(data, dict):
            per_relay.append((route["name"], None, 0.0, 0.0, 0, "HTTP %s" % status))
            continue
        stats = data.get("model_stats")
        if not isinstance(stats, list):
            # relay only reports totals/daily breakdown (no per-model rows)
            total = (data.get("usage") or {}).get("total") or {}
            tok = total.get("total_tokens", 0)
            per_relay.append((route["name"], None, 0.0, 0.0, tok, "无 model_stats"))
            continue
        tl = tr = tok = 0
        for ms in stats:
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
        per_relay.append((route["name"], pid, tl, tr, tok, ""))
    return per_relay, per_model, pricing


def print_relays(per_relay, field):
    print("%-24s %12s %12s %12s %8s  %s"
          % ("中转", "token量", "标价", field, "系数", "备注"))
    print("-" * 84)
    for name, pid, tl, tr, tok, note in sorted(per_relay, key=lambda r: -r[4]):
        if tl <= 0:
            print("%-24s %12d %12s %12s  %s" % (name[:24], tok, "-", "-", note))
        else:
            print("%-24s %12d %12.2f %12.2f %8.3f  %s"
                  % (name[:24], tok, tl, tr, tr / tl, note))


def print_balances(routes):
    """余额查询 + 检查 CC Switch 里每个供应商查的是不是自己的地址。"""
    bound = provider_usage_base()
    print("%-24s %-6s %14s %-5s %-12s %s" % ("中转", "HTTP", "余额", "单位", "planName", "余额地址绑定"))
    print("-" * 100)
    misbound, failed = 0, 0
    for pid, route in routes.items():
        if route.get("auth_type") == "oauth":
            print("%-24s %-6s %14s %-5s %-12s %s"
                  % (route["name"][:24], "-", "-", "-", "-", "oauth 账号，CC Switch 自带"))
            continue
        status, data, note = usage(route)
        if status == 200 and isinstance(data, dict):
            bal = data.get("remaining")
            if bal is None:
                bal = data.get("balance")
            base = bound.get(pid)
            if base is None:
                check = "未配置"
                misbound += 1
            elif base.rstrip("/") == route["upstream"].rstrip("/"):
                check = "ok"
            else:
                check = "错: %s（应 %s）" % (base, route["upstream"])
                misbound += 1
            print("%-24s %-6s %14s %-5s %-12s %s"
                  % (route["name"][:24], status, bal, data.get("unit") or "USD",
                     (data.get("planName") or "")[:12], check))
        else:
            print("%-24s %-6s %14s %-5s %-12s %s"
                  % (route["name"][:24], status or "ERR", "-", "-", "-",
                     note or "查询失败"))
            failed += 1
    print()
    if misbound:
        print("有 %d 个中转的余额地址不是它自己：跑一次 setup.py 会按各自的中转重新对齐。" % misbound)
    if failed:
        print("有 %d 个中转查不到余额（HTTP/网络错误，见上表备注）；余额地址无关，需要查该中转本身。" % failed)
    if not misbound and not failed:
        print("全部中转都能查到自己的余额。")


def backup_pricing():
    """Save the current model_pricing values before rewriting them."""
    rows = {}
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=5)
    for row in conn.execute("SELECT model_id, input_cost_per_million,"
                            " output_cost_per_million, cache_read_cost_per_million,"
                            " cache_creation_cost_per_million FROM model_pricing"):
        rows[row[0]] = list(row[1:])
    conn.close()
    path = os.path.join(BASE, "model_pricing-bak-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    return path, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", action="store_true", help="余额查询（含地址绑定检查）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--write-pricing", action="store_true",
                    help="写 model_pricing 单价（CC Switch 确实会采用）")
    ap.add_argument("--field", default="actual_cost", choices=FIELDS)
    args = ap.parse_args()

    if not os.path.exists(ROUTES):
        sys.exit("缺 routes.json，先跑 setup.py")
    routes = json.load(open(ROUTES))["routes"]

    if args.balance:
        print_balances(routes)
        return

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
            path, _old = backup_pricing()
            conn = sqlite3.connect(DB)
            for mid, new in plan:
                conn.execute("UPDATE model_pricing SET input_cost_per_million=?,"
                             " output_cost_per_million=?, cache_read_cost_per_million=?,"
                             " cache_creation_cost_per_million=? WHERE model_id=?",
                             tuple("%.6f" % v for v in new) + (mid,))
            conn.commit()
            conn.close()
            print("\n已按真实计费折算 %d 个模型的单价。" % len(plan))
            print("旧单价已备份：%s（还原：把其中的值写回 model_pricing）" % path)
        elif plan:
            print("\n[dry-run] 加 --apply 才会写入。")
        return

    updates = [(n, pid, tr / tl) for n, pid, tl, tr, tok, note in per_relay
               if pid and tl > 0 and 0 < tr / tl < 100]
    if not args.apply:
        print("\n[dry-run] 会把上面各中转的系数写入 providers.cost_multiplier"
              "（%d 个）。加 --apply 执行。" % len(updates))
        print("提示：实测 CC Switch 记账仍记 1.0，推荐用 --write-pricing。")
        return
    conn = sqlite3.connect(DB)
    backup = {}
    for row in conn.execute("SELECT id, name, cost_multiplier FROM providers "
                            "WHERE app_type='codex'"):
        backup[row[0]] = list(row[1:])
    path = os.path.join(BASE, "cost_multiplier-bak-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(path, "w") as fh:
        json.dump(backup, fh, ensure_ascii=False, indent=1)
    for name, pid, mult in updates:
        conn.execute("UPDATE providers SET cost_multiplier=? WHERE id=? AND app_type='codex'",
                     ("%.4f" % mult, pid))
    conn.commit()
    conn.close()
    print("\n已更新 %d 个供应商的成本倍率。旧值已备份：%s" % (len(updates), path))


if __name__ == "__main__":
    main()
