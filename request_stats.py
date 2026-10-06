#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按请求回答一个问题：换供应商是**主动动态调整**，还是**失败重连**？

数据来自桥的逐请求日志 `bridge-requests.jsonl`（每次尝试一行，含
`mount`＝CC Switch 发给谁、`relay`＝桥实际用了谁、`attempt`、`status`）。

判定：

  ① attempt=1 且 relay == mount   入口那家就是排序第一，没换
  ② attempt=1 且 relay != mount   **主动**：前面没有任何失败，桥就换了别家
  ③ attempt >= 2                  **失败重连**：第一次失败了才换

用法：

    python3 request_stats.py                     # 默认读 ~/.ccswitch-retry-bridge
    python3 request_stats.py --minutes 30        # 只看最近 30 分钟
    python3 request_stats.py --log <path>
"""

import argparse
import json
import os
import statistics
import time
from collections import Counter, defaultdict

DEFAULT_LOG = os.path.expanduser("~/.ccswitch-retry-bridge/bridge-requests.jsonl")
SERVED = ("ok", "pass")
# same relative weights the bridge uses to price a relay (see bridge.py)
TOKEN_WEIGHTS = {"input_tokens": 1.0, "output_tokens": 4.0,
                 "cache_read_tokens": 0.1, "cache_creation_tokens": 1.25}


def weighted_million(tok):
    return sum((tok.get(k) or 0) * w for k, w in (
        ("input", TOKEN_WEIGHTS["input_tokens"]),
        ("output", TOKEN_WEIGHTS["output_tokens"]),
        ("cache_read", TOKEN_WEIGHTS["cache_read_tokens"]),
        ("cache_creation", TOKEN_WEIGHTS["cache_creation_tokens"]))) / 1e6


def load(path, minutes=None):
    rows = []
    cutoff = time.time() - minutes * 60 if minutes else None
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if cutoff and (row.get("epoch") or 0) < cutoff:
                continue
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--minutes", type=float, default=None,
                    help="只看最近 N 分钟（默认全部）")
    args = ap.parse_args()
    if not os.path.exists(args.log):
        raise SystemExit("没有请求日志：%s（桥从 v1.3 起会写这个文件）" % args.log)

    rows = load(args.log, args.minutes)
    if not rows:
        raise SystemExit("窗口内没有记录")
    served = [r for r in rows if r.get("result") in SERVED]
    attempts = len(rows)
    first = [r for r in served if r.get("attempt") == 1]
    same = [r for r in first if r.get("relay") == r.get("mount")]
    pro = [r for r in first if r.get("relay") != r.get("mount")]
    fb = [r for r in served if (r.get("attempt") or 1) > 1]

    t0 = min(r.get("epoch") or 0 for r in rows)
    t1 = max(r.get("epoch") or 0 for r in rows)
    print("窗口      : %s → %s（%.1f 分钟）"
          % (time.strftime("%H:%M:%S", time.localtime(t0)),
             time.strftime("%H:%M:%S", time.localtime(t1)), (t1 - t0) / 60))
    print("尝试次数  : %d   成功服务: %d   平均尝试/请求: %.2f"
          % (attempts, len(served), attempts / max(1, len(served))))
    print()

    def pct(part):
        return 100.0 * part / max(1, len(served))

    print("① attempt=1 且 relay == mount  : %5d  (%4.0f%%)  入口那家就是第一名" % (len(same), pct(len(same))))
    print("② attempt=1 且 relay != mount  : %5d  (%4.0f%%)  ★主动切换，前面没有任何失败"
          % (len(pro), pct(len(pro))))
    print("③ attempt >= 2                 : %5d  (%4.0f%%)  失败重连（真正的 failover）"
          % (len(fb), pct(len(fb))))
    print()

    print("主动切换换到了谁 :", dict(Counter(r["relay"] for r in pro).most_common()) or "—")
    print("失败重连后服务者 :", dict(Counter(r["relay"] for r in fb).most_common()) or "—")
    print("入口(mount)分布  :", dict(Counter(r.get("mount") for r in served).most_common()))
    print()

    print("%-22s %7s %7s %7s %10s %10s %12s" %
          ("中转", "服务", "主动", "重连", "首字节中位", "加权M tok", "估算花费$"))
    print("-" * 82)
    per = defaultdict(lambda: {"n": 0, "pro": 0, "fb": 0, "ttfb": [], "wm": 0.0, "cost": 0.0})
    for r in served:
        e = per[r["relay"]]
        e["n"] += 1
        e["pro"] += (r.get("attempt") == 1 and r.get("relay") != r.get("mount"))
        e["fb"] += (r.get("attempt") or 1) > 1
        if r.get("first_byte_ms") is not None:
            e["ttfb"].append(r["first_byte_ms"])
        t = r.get("tokens")
        if t:
            e["wm"] += weighted_million(t)
            e["cost"] += r.get("est_cost_usd") or 0.0
    for name, e in sorted(per.items(), key=lambda kv: -kv[1]["n"]):
        med = int(statistics.median(e["ttfb"])) if e["ttfb"] else None
        print("%-22s %7d %7d %7d %10s %10.3f %12.5f"
              % (name, e["n"], e["pro"], e["fb"], ("%dms" % med) if med else "-",
                 e["wm"], e["cost"]))
    print()

    tok = [r for r in served if r.get("tokens")]
    if tok:
        no_usage = [r for r in served if not r.get("tokens")]
        cut = [r for r in no_usage if r.get("stream_complete") is False]
        tot_wm = sum(weighted_million(r["tokens"]) for r in tok)
        tot_cost = sum(r.get("est_cost_usd") or 0 for r in tok)
        raw_in = sum(r["tokens"].get("input", 0) for r in tok)
        raw_cr = sum(r["tokens"].get("cache_read", 0) for r in tok)
        raw_out = sum(r["tokens"].get("output", 0) for r in tok)
        print("用量与花费（有 usage 的 %d/%d 个请求）" % (len(tok), len(served)))
        if no_usage:
            print("  没有 usage 的 %d 个里，%d 个是客户端中途断开（stream_complete=false，"
                  "中转根本没发 completed/usage）" % (len(no_usage), len(cut)))
        print("  未缓存输入 %-12d 输出 %-10d 缓存读取 %-12d 缓存命中率 %.0f%%"
              % (raw_in, raw_out, raw_cr, 100.0 * raw_cr / max(1, raw_in + raw_cr)))
        print("  加权百万 token: %.3f   估算总花费: $%.5f   平均 $%.4f/加权M"
              % (tot_wm, tot_cost, (tot_cost / tot_wm) if tot_wm else 0))
        print("  （估算 = 该请求的加权 token × **该中转实测单价**，按请求当时的价格）")
        print()
        by_model_cost = defaultdict(lambda: [0, 0.0, 0.0])
        for r in tok:
            m = r.get("model") or "?"
            by_model_cost[m][0] += 1
            by_model_cost[m][1] += weighted_million(r["tokens"])
            by_model_cost[m][2] += r.get("est_cost_usd") or 0
        print("%-20s %7s %12s %12s" % ("模型", "请求", "加权M tok", "估算花费$"))
        for m, (n, wm, c) in sorted(by_model_cost.items(), key=lambda kv: -kv[1][2]):
            print("%-20s %7d %12.3f %12.5f" % (m, n, wm, c))
        print()

    by_model = defaultdict(Counter)
    for r in served:
        by_model[r.get("model") or "?"]["_n"] += 1
        by_model[r.get("model") or "?"][r["relay"]] += 1
    print("按模型：")
    for model, c in sorted(by_model.items(), key=lambda kv: -kv[1]["_n"]):
        who = {k: v for k, v in c.items() if k != "_n"}
        print("  %-18s %4d 次  %s" % (model, c["_n"], dict(Counter(who).most_common(4))))


if __name__ == "__main__":
    main()
