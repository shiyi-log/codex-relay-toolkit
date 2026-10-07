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


def usage_urls(route):
    """Candidate usage endpoints for one relay, most specific first.

    A relay whose `prefix` is empty serves the endpoint at `/v1/usage`, not
    `/usage` - getting this wrong returns an HTML/404 body (which is exactly the
    bug that made the first version of this tool read no balances at all).
    """
    prefix = (route.get("prefix") or "").rstrip("/")
    paths = []
    for path in (prefix + "/usage", "/v1/usage"):
        if path not in paths:
            paths.append(path)
    return [route["upstream"].rstrip("/") + path for path in paths]


def fetch_balances(routes):
    """-> {relay_name: balance}; relays that do not report one are skipped."""
    out = {}
    for pid, route in routes.items():
        if route.get("auth_type") == "oauth":
            continue
        data = None
        for url in usage_urls(route):
            try:
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer " + (route.get("auth") or ""),
                                  "Accept": "application/json",
                                  "User-Agent": "curl/8.7.1"})
                with urllib.request.urlopen(req, timeout=15) as fh:
                    data = json.load(fh)
                break
            except Exception:
                data = None
        if not isinstance(data, dict):
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


def account_labels(routes):
    """-> {account_label: "pp plus 等 5 家"} for readable report lines."""
    by_host = {}
    for _pid, route in routes.items():
        host = urlsplit(route.get("upstream") or "").netloc
        if route.get("auth_type") == "oauth" or not host:
            continue
        by_host.setdefault(host, []).append(route.get("name") or _pid)
    out = {}
    for host, names in by_host.items():
        names.sort()
        out[names[0]] = "%s%s" % (names[0], (" 等 %d 家" % len(names)) if len(names) > 1 else "")
    return out


def group_accounts(balances):
    """{relay_name: (value, host)} -> {account_label: value}.

    Relays that share one upstream host share one wallet (our `pp 特惠` / `pp plus`
    / `pp pro` / `pp 兜底` / `pp 不降智` are five API keys on one account). Counting
    them separately produced nonsense like "-$0.0121 spent" from read jitter, so
    group by host and take the median of the values seen for it. The label is a
    relay name, **not** the domain - the docs mask the provider URLs on purpose.
    """
    by_host = {}
    for name, (value, host) in balances.items():
        by_host.setdefault(host, []).append((name, value))
    out = {}
    for host, items in by_host.items():
        items.sort()
        values = sorted(v for _n, v in items)
        out[items[0][0]] = values[len(values) // 2]
    return out


def normalize_ledger(rows):
    """-> [{ts, accounts}] merging legacy per-relay samples and duplicates."""
    routes = {}
    if os.path.exists(ROUTES):
        try:
            routes = load_routes()
        except Exception:
            routes = {}
    label_of = {}
    for _pid, route in routes.items():
        host = urlsplit(route.get("upstream") or "").netloc
        label_of.setdefault(host, []).append(route.get("name") or _pid)
    for host in label_of:
        label_of[host] = sorted(label_of[host])[0]

    merged = {}
    for row in rows:
        ts = round(float(row.get("ts") or 0), 0)
        slot = merged.setdefault(ts, {"ts": ts, "accounts": {}, "raw": {}})
        accounts = row.get("accounts")
        if isinstance(accounts, dict) and accounts:
            slot["accounts"].update(accounts)
            continue
        for name, value in (row.get("balances") or {}).items():
            host = None
            for _pid, route in routes.items():
                if (route.get("name") or _pid) == name:
                    host = urlsplit(route.get("upstream") or "").netloc
                    break
            slot["raw"][label_of.get(host, name)] = value
    out = []
    for ts in sorted(merged):
        slot = merged[ts]
        accounts = dict(slot["accounts"])
        if slot["raw"]:
            accounts.update(slot["raw"])          # one relay per legacy line
        if len(accounts) >= 2:
            out.append({"ts": ts, "accounts": accounts})
    return out


def read_ledger():
    """Only samples that actually carry balances count.

    The launcher samples every 15 minutes; if the fetch failed (relay down,
    throttled) the sample must not poison the series - an empty sample once made
    the window report "$0.0000 spent" for hours.
    """
    rows = []
    if os.path.exists(LEDGER):
        for line in open(LEDGER, errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (row.get("accounts") or row.get("balances")):
                rows.append(row)
    return rows


def append_sample(balances, ts=None, source="fetch"):
    ts = ts or time.time()
    entry = {"ts": round(ts, 3), "source": source,
             "accounts": group_accounts(balances)}
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
    for name, value in last["accounts"].items():
        before = first["accounts"].get(name)
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
        seat = {}
        base_ts = 0.0
        for pid, bucket in (state.get("stats") or {}).items():
            value = bucket.get("balance")
            route = routes.get(pid) or {}
            ts = ((state.get("prices") or {}).get(pid) or {}).get("ts")
            if value is None or not ts or not route.get("name"):
                continue
            seat[route["name"]] = (value, urlsplit(route["upstream"]).netloc)
            base_ts = max(base_ts, ts)
        if seat:
            with open(LEDGER, "a") as fh:
                fh.write(json.dumps({"ts": round(base_ts, 3), "source": "bridge-state",
                                     "accounts": group_accounts(seat)},
                                    ensure_ascii=False) + "\n")
            seeded = len(seat)
        if seeded:
            print("（首次运行：用桥最近一次价格刷新带回的余额做了基线，%d 条）\n" % seeded)

    accounts_now = group_accounts(balances)
    if len(accounts_now) < 2:
        print("⚠ 只拿到 %d 个账号的余额，本次不写入台账（避免污染窗口）: %s"
              % (len(accounts_now), ", ".join(accounts_now) or "无"))
    else:
        append_sample(balances, now)
    if args.sample:
        print("已采样 %d 家中转余额 -> %s" % (len(balances), os.path.basename(LEDGER)))
        return 0

    samples = normalize_ledger(read_ledger())
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
    labels = account_labels(routes)
    for name, value in sorted(delta.items(), key=lambda kv: -kv[1]):
        print("    %-22s -$%.4f" % (labels.get(name, name), value))
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
