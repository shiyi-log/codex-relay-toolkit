#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本口径对账：桥的估算 vs CC Switch 自己的表 vs **余额实际减少**。

为什么要它：桥的成本是按**各家中转自己报的账单单价**算的（`/v1/usage` 的 actual_cost），
CC Switch 的表用的是共享定价表，两者经常差一个数量级；而两者都可能有偏差，
**唯一的地面真相是中转余额的减少**。本工具：

    python3 reconcile.py            # 采样一次余额 + 输出对账（窗口 = 两次采样之间）
    python3 reconcile.py --hours 6  # 指定窗口（余额部分用窗口两端的采样）
    python3 reconcile.py --sample   # 只采样，不出报告（给 launchd 用）
    python3 reconcile.py --json

余额采样存在 `balance-history.jsonl`；第一次运行时如果账本为空，会先用
`bridge-state.json` 里最近一次价格刷新带回来的余额做基线（桥每 10 分钟刷一次价格，
顺带读余额），所以第一次就有窗口可比。
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from urllib.parse import urlsplit

BASE = os.path.dirname(os.path.abspath(__file__))
ROUTES = os.environ.get("BRIDGE_ROUTES", os.path.join(BASE, "routes.json"))
REQUESTS = os.environ.get("BRIDGE_REQUEST_LOG", os.path.join(BASE, "bridge-requests.jsonl"))
STATE = os.environ.get("BRIDGE_STATE", os.path.join(BASE, "bridge-state.json"))
LEDGER = os.environ.get("BRIDGE_BALANCE_LEDGER", os.path.join(BASE, "balance-history.jsonl"))
DB = os.environ.get("BRIDGE_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))
WEIGHTS = {"input": 1.0, "output": 4.0, "cache_read": 0.1, "cache_creation": 1.25}


def load_routes():
    with open(ROUTES) as fh:
        return json.load(fh).get("routes") or {}


def fetch_balances(routes):
    """-> {relay_name: balance}; relays that do not report one are skipped."""
    out = {}
    for pid, route in routes.items():
        if route.get("auth_type") == "oauth":
            continue
        url = route["upstream"].rstrip("/") + (route.get("prefix") or "") + "/usage"
        try:
            req = urllib.request.Request(
                url, headers={"Authorization": "Bearer " + (route.get("auth") or ""),
                              "Accept": "application/json", "User-Agent": "curl/8.7.1"})
            with urllib.request.urlopen(req, timeout=15) as fh:
                data = json.load(fh)
        except Exception:
            continue
        value = data.get("remaining")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = data.get("balance")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if float(value) > 1e6:            # prepaid/unlimited relays report nonsense
            continue
        out[route.get("name") or pid] = (float(value), urlsplit(route["upstream"]).netloc)
    return out


def read_ledger():
    rows = []
    if os.path.exists(LEDGER):
        for line in open(LEDGER, errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def append_sample(balances, ts=None, source="fetch"):
    ts = ts or time.time()
    entry = {"ts": round(ts, 3), "source": source,
             "balances": {name: value for name, (value, _host) in balances.items()}}
    with open(LEDGER, "a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def seed_from_state():
    """First run: use the balances the bridge already fetched with its price refresh."""
    try:
        state = json.load(open(STATE))
        prices = state.get("prices") or {}
        stats = state.get("stats") or {}
        seat = {}
        ts = 0.0
        for pid, bucket in stats.items():
            value = bucket.get("balance")
            if value is None:
                continue
            name = None
            ts = max(ts, (prices.get(pid) or {}).get("ts") or 0)
            seat[name] = value
        # names are not in the state file - map by order of routes with balances
        if not seat or not ts:
            return None
        return {"ts": round(ts, 3), "source": "bridge-state",
                "balances": "?"}                       # placeholder, fixed below
    except Exception:
        return None


def weighted_million(tokens):
    return sum((tokens.get(k) or 0) * w for k, w in WEIGHTS.items()) / 1e6


def bridge_window(start, end):
    cost = 0.0
    wm = 0.0
    rows = 0
    per_relay = {}
    if not os.path.exists(REQUESTS):
        return {"cost": 0.0, "wm": 0.0, "rows": 0, "per_relay": {}}
    for line in open(REQUESTS, errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        epoch = row.get("epoch") or 0
        if not (start <= epoch <= end) or row.get("result") not in ("ok", "pass"):
            continue
        tokens = row.get("tokens") or {}
        if not tokens:
            continue
        rows += 1
        cost += row.get("est_cost_usd") or 0.0
        w = weighted_million(tokens)
        wm += w
        ent = per_relay.setdefault(row.get("relay") or "?", {"n": 0, "wm": 0.0, "cost": 0.0})
        ent["n"] += 1
        ent["wm"] += w
        ent["cost"] += row.get("est_cost_usd") or 0.0
    return {"cost": cost, "wm": wm, "rows": rows, "per_relay": per_relay}


def ccswitch_window(start, end):
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=10)
    except Exception as exc:
        return {"cost": 0.0, "rows": 0, "error": str(exc)}
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CAST(total_cost_usd AS REAL)) FROM proxy_request_logs "
            "WHERE created_at >= ? AND created_at <= ? AND data_source='proxy'",
            (int(start), int(end))).fetchone()
        return {"cost": row[1] or 0.0, "rows": row[0] or 0}
    finally:
        conn.close()


def account_delta(samples, start, end):
    """Per account (= upstream host) balance drop between the window's samples."""
    first, last = samples[0], samples[-1]
    out = {}
    for name, value in last["balances"].items():
        before = first["balances"].get(name)
        if before is None:
            continue
        out[name] = before - value
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0, help="窗口小时数；0=用两次余额采样之间")
    ap.add_argument("--sample", action="store_true", help="只采样余额")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    routes = load_routes()
    balances = fetch_balances(routes)
    now = time.time()

    if not os.path.exists(LEDGER) or not read_ledger():
        state = json.load(open(STATE)) if os.path.exists(STATE) else {}
        seeded = 0
        with open(LEDGER, "a") as fh:
            for pid, bucket in (state.get("stats") or {}).items():
                value = bucket.get("balance")
                route = routes.get(pid) or {}
                ts = ((state.get("prices") or {}).get(pid) or {}).get("ts")
                if value is None or not ts or not route.get("name"):
                    continue
                fh.write(json.dumps({"ts": round(ts, 3), "source": "bridge-state",
                                     "balances": {route["name"]: value}},
                                    ensure_ascii=False) + "\n")
                seeded += 1
        if seeded:
            print("（首次运行：用桥最近一次价格刷新带回的余额做了基线，%d 条）\n" % seeded)

    append_sample(balances, now)
    if args.sample:
        print("已采样 %d 家中转余额 -> %s" % (len(balances), os.path.basename(LEDGER)))
        return 0

    samples = sorted(read_ledger(), key=lambda r: r["ts"])
    if args.hours > 0:
        start, end = now - args.hours * 3600, now
    else:
        start, end = samples[0]["ts"], samples[-1]["ts"]
        # merge the first/last few state samples so the start has all relays
        for row in samples:
            if abs(row["ts"] - start) < 900:
                start = min(start, row["ts"])
    window_first = min(samples, key=lambda r: abs(r["ts"] - start))
    window_last = samples[-1]

    bridge = bridge_window(start, end)
    ccsw = ccswitch_window(start, end)
    delta = account_delta([window_first, window_last], start, end)
    real = sum(v for v in delta.values() if v > 0)

    if args.json:
        print(json.dumps({"start": start, "end": end, "bridge": bridge, "ccswitch": ccsw,
                          "balance_delta": delta, "real": real}, ensure_ascii=False, indent=2))
        return 0

    span = (end - start) / 60.0
    print("窗口        : %s → %s（%.1f 分钟）"
          % (time.strftime("%H:%M:%S", time.localtime(start)),
             time.strftime("%H:%M:%S", time.localtime(end)), span))
    print("桥估算      : $%.4f    （%d 个请求有 usage，%.3f 加权M，平均 $%.4f/加权M）"
          % (bridge["cost"], bridge["rows"], bridge["wm"],
             (bridge["cost"] / bridge["wm"]) if bridge["wm"] else 0))
    print("CC Switch表 : $%.4f    （%d 行 proxy 日志，按共享定价表算）"
          % (ccsw["cost"], ccsw["rows"]))
    if bridge["cost"]:
        print("两者倍数    : %.2fx" % (ccsw["cost"] / bridge["cost"]))
    print("余额实际减少 : $%.4f    （%s → %s，中转自己扣的钱）"
          % (real, time.strftime("%H:%M:%S", time.localtime(window_first["ts"])),
             time.strftime("%H:%M:%S", time.localtime(window_last["ts"]))))
    for name, value in sorted(delta.items(), key=lambda kv: -kv[1]):
        print("    %-16s $%+.4f" % (name, -value))
    if bridge["cost"]:
        print("桥 / 余额    : %.2fx   （越接近 1.00，说明桥的单价越准）"
              % (real / bridge["cost"]))
    print()
    print("按中转（桥的估算）：")
    print("  %-16s %6s %10s %12s %12s" % ("中转", "请求", "加权M", "估算$", "平均$/加权M"))
    for name, ent in sorted(bridge["per_relay"].items(), key=lambda kv: -kv[1]["cost"]):
        print("  %-16s %6d %10.3f %12.5f %12.4f"
              % (name, ent["n"], ent["wm"], ent["cost"],
                 (ent["cost"] / ent["wm"]) if ent["wm"] else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
