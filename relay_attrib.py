#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 CC Switch「请求日志」里的供应商改成**真正服务这次请求的中转**。

CC Switch 记的是它把请求发给了谁（桥的入口 mount），看不到桥在内部换了谁。
但它的行主键里带着中转返回的 response id：

    request_id = session:codex:<它发去的 provider>:resp_xxxx

而桥的 `bridge-requests.jsonl` 里既有 response_id，也有这次真正用的 relay_id，
所以可以**精确到行**地把归属改成真实值 —— CC Switch 的「请求日志 / Provider 统计 /
模型统计」就都是真的了。**不改任何路由行为**，只改归属数据。

用法：

    python3 relay_attrib.py --once            # 处理新写入的请求（推荐给 launchd 定时跑）
    python3 relay_attrib.py --once --dry-run  # 只看会改什么
    python3 relay_attrib.py --rollback        # 按变更记录还原
    python3 relay_attrib.py --status          # 看进度/偏移/已改多少行
"""

import argparse
import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
REQLOG = os.environ.get("BRIDGE_REQUEST_LOG", os.path.join(BASE, "bridge-requests.jsonl"))
DB = os.environ.get("BRIDGE_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))
STATE = os.environ.get("ATTRIB_STATE", os.path.join(BASE, "attribution-state.json"))
CHANGELOG = os.environ.get("ATTRIB_CHANGELOG",
                           os.path.join(BASE, "attribution-changelog.jsonl"))
SETTLED = ("ok", "pass")
# unmatched rows are retried for this long (CC Switch writes its log row a beat
# after the response), then dropped
PENDING_TTL = float(os.environ.get("ATTRIB_PENDING_TTL", "900"))
PENDING_MAX = int(os.environ.get("ATTRIB_PENDING_MAX", "500"))


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"offset": 0, "changed": 0, "skipped": 0}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)


def pending(state):
    """New request-log lines with both a response id and a relay id."""
    if not os.path.exists(REQLOG):
        return [], state
    size = os.path.getsize(REQLOG)
    offset = state.get("offset", 0)
    if size < offset:                      # rotated
        offset = 0
    out = []
    with open(REQLOG, errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (row.get("result") in SETTLED and row.get("response_id")
                    and row.get("relay_id") and row.get("mount_id")):
                out.append(row)
        state["offset"] = fh.tell()
    return out, state


def run_once(dry=False, quiet=False):
    state = load_state()
    rows, state = pending(state)
    # CC Switch writes its log row a moment after the response ends, so a row we
    # cannot find yet is kept and retried instead of being dropped forever
    seen = {r["response_id"] for r in rows}
    queue = [q for q in (state.get("pending") or []) if q.get("response_id") not in seen]
    todo = rows + queue
    changed = []
    unresolved = []
    already = 0
    conn = sqlite3.connect(DB, timeout=10)
    try:
        for row in todo:
            suffix = "%:" + row["response_id"]
            hit = conn.execute(
                "SELECT request_id, provider_id FROM proxy_request_logs "
                "WHERE request_id LIKE ? AND data_source='proxy' "
                "ORDER BY rowid DESC LIMIT 1", (suffix,)).fetchone()
            if not hit:
                unresolved.append(row)
                continue
            request_id, old = hit
            if old == row["relay_id"]:
                already += 1
                continue
            if not dry:
                cur = conn.execute(
                    "UPDATE proxy_request_logs SET provider_id=? "
                    "WHERE request_id=? AND provider_id=?", (row["relay_id"], request_id, old))
                if cur.rowcount != 1:
                    unresolved.append(row)
                    continue
            changed.append((request_id, old, row["relay_id"],
                            row.get("relay"), row.get("model")))
            if not dry:
                with open(CHANGELOG, "a") as fh:
                    fh.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                         "request_id": request_id, "from": old,
                                         "to": row["relay_id"]}, ensure_ascii=False) + "\n")
        if not dry:
            conn.commit()
    finally:
        conn.close()

    if not dry:
        now = time.time()
        unresolved = [r for r in unresolved
                      if now - (r.get("epoch") or now) < PENDING_TTL]
        state["pending"] = unresolved[-PENDING_MAX:]
        state["changed"] = state.get("changed", 0) + len(changed)
        state["skipped"] = state.get("skipped", 0) + already
        save_state(state)
    if not quiet or changed:
        print("%s %d/%d 行归属已修正（累计 %d，重试队列 %d）"
              % ("[dry-run] 会改" if dry else "已改", len(changed), len(todo),
                 state.get("changed", 0), len(state.get("pending") or [])))
        for request_id, old, new, relay, model in changed[-8:]:
            print("  %s  %s -> %s  (%s %s)"
                  % (request_id.rsplit(":", 1)[-1][:20], old[:8], new[:8], relay, model))
    return len(changed)


def rollback():
    if not os.path.exists(CHANGELOG):
        sys.exit("没有变更记录，无需还原")
    first = {}
    with open(CHANGELOG) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            first.setdefault(row["request_id"], row["from"])
    conn = sqlite3.connect(DB, timeout=10)
    n = 0
    try:
        for request_id, old in first.items():
            cur = conn.execute("UPDATE proxy_request_logs SET provider_id=? "
                               "WHERE request_id=?", (old, request_id))
            n += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    os.replace(CHANGELOG, CHANGELOG + ".rolled-back")
    print("已还原 %d 行的供应商归属（变更记录改名为 %s.rolled-back）" % (n, CHANGELOG))


def status():
    state = load_state()
    print("请求日志   : %s" % REQLOG)
    print("已读偏移   : %d" % state.get("offset", 0))
    print("累计修正   : %d 行（跳过 %d）" % (state.get("changed", 0), state.get("skipped", 0)))
    if os.path.exists(CHANGELOG):
        with open(CHANGELOG) as fh:
            n = sum(1 for l in fh if l.strip())
        print("变更记录   : %s（%d 条）" % (CHANGELOG, n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="处理新请求（默认动作）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="没有改动时不输出")
    ap.add_argument("--rollback", action="store_true", help="按变更记录还原归属")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        return status()
    if args.rollback:
        return rollback()
    if not os.path.exists(DB):
        sys.exit("找不到 CC Switch 数据库：%s" % DB)
    run_once(dry=args.dry_run, quiet=args.quiet)


if __name__ == "__main__":
    main()
