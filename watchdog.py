#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Session watchdog: keep a Codex task going after an ABNORMAL end.

Codex already auto-continues threads that have an `active` goal (its goal engine
lives in ~/.codex/goals_1.sqlite). Measured over 48h on this machine: 22 of 24
abnormal endings were already resumed by it. This watchdog only covers the
remaining gap - a turn that died with an error, produced NO new task, and whose
goal is not active (or does not exist).

It resumes by queueing one message into the existing session:

    codex queue --thread <uuid> --message "..."

Safety rails, because this spends tokens automatically:

  * only threads whose last turn ended with an error AND started nothing after
  * only after the rollout has been idle for IDLE_SECONDS (never race Codex's
    own auto-continue)
  * never touch goals that are paused / complete / blocked / usage_limited /
    budget_limited
  * per-thread cooldown and a rolling hourly cap
  * global kill switch: create the file  DISABLED  next to this script
  * --dry-run prints what it would do and changes nothing
"""

import argparse
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state.json")
LOG = os.path.join(BASE, "watchdog.log")
DISABLED = os.path.join(BASE, "DISABLED")
CODEX = (os.environ.get("CODEX_BIN")
         or shutil.which("codex")
         or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
SESSIONS = os.path.expanduser("~/.codex/sessions")
GOALS_DB = os.path.expanduser("~/.codex/goals_1.sqlite")

IDLE_SECONDS = 120          # rollout must be quiet this long
COOLDOWN_SECONDS = 600      # between two resumes of the same thread
MAX_PER_HOUR = 3            # per thread
LOOKBACK_HOURS = 12         # only threads touched recently
SKIP_GOAL_STATUS = {"paused", "complete", "blocked", "usage_limited", "budget_limited"}

MESSAGE = ("上一次任务因「{err}」异常中断，没有正常结束。请从中断处继续完成原目标，"
           "不要重新开始。如果环境仍然不可用，请说明原因后停止。")


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG, "a") as fh:
        fh.write(line)
    sys.stdout.write(line)
    sys.stdout.flush()


def load_state():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=2)


def goal_status():
    out = {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % GOALS_DB, uri=True, timeout=3)
        for tid, st in conn.execute("SELECT thread_id, status FROM thread_goals"):
            out[tid] = st
        conn.close()
    except Exception as exc:
        log("goals db unreadable (%s) - treating every thread as goal-less" % exc)
    return out


def inspect(path):
    """Return (abnormal, already_continued, error_text) for a rollout file."""
    try:
        with open(path, errors="replace") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return False, False, ""
    last_idx, last_err = None, None
    for i, line in enumerate(lines):
        if '"type":"task_complete"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload", {})
        except Exception:
            continue
        if payload.get("type") == "task_complete":
            last_idx, last_err = i, payload.get("error")
    if last_idx is None or not last_err:
        return False, False, ""
    continued = any('"type":"task_started"' in l for l in lines[last_idx + 1:])
    msg = last_err.get("message") if isinstance(last_err, dict) else str(last_err)
    return True, continued, (msg or "unknown error")


def thread_uuid(filename):
    base = os.path.basename(filename)
    return base[:-6][-36:] if base.endswith(".jsonl") else ""


def recent_rollouts():
    out = []
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    for path in glob.glob(os.path.join(SESSIONS, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) >= cutoff:
                out.append(path)
        except OSError:
            pass
    out.sort(key=os.path.getmtime, reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if os.path.exists(DISABLED):
        log("watchdog DISABLED (remove %s to re-enable)" % DISABLED)
        return

    goals = goal_status()
    state = load_state()
    now = time.time()
    actions = 0

    for path in recent_rollouts():
        tid = thread_uuid(path)
        if not tid:
            continue
        abnormal, continued, err = inspect(path)
        if not abnormal:
            continue
        st = state.setdefault(tid, {"history": [], "last": 0})
        st["history"] = [t for t in st["history"] if now - t < 3600]

        if continued:
            continue                                  # Codex already resumed it
        idle = now - os.path.getmtime(path)
        if idle < IDLE_SECONDS:
            continue                                  # may still be running
        g = goals.get(tid)
        if g in SKIP_GOAL_STATUS:
            continue                                  # paused/blocked/... by design
        if now - st.get("last", 0) < COOLDOWN_SECONDS:
            continue
        if len(st["history"]) >= MAX_PER_HOUR:
            log("SKIP %s - hourly cap (%d) reached; not resuming again" % (tid[:8], MAX_PER_HOUR))
            continue

        msg = MESSAGE.format(err=err[:160])
        if args.dry_run:
            log("DRY-RUN would resume %s (goal=%s, idle=%.0fs, err=%s)"
                % (tid[:8], g or "none", idle, err[:70]))
            actions += 1
            continue

        try:
            r = subprocess.run([CODEX, "queue", "--thread", tid, "--message", msg],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            log("RESUME %s (goal=%s, idle=%.0fs) rc=%d %s"
                % (tid[:8], g or "none", idle, r.returncode,
                   (r.stdout or r.stderr or "").strip()[:120]))
        except Exception as exc:
            ok = False
            log("RESUME %s FAILED: %s" % (tid[:8], exc))

        if ok:
            st["last"] = now
            st["history"].append(now)
            actions += 1

    save_state(state)
    if actions:
        log("cycle done: %d resume(s)" % actions)


if __name__ == "__main__":
    main()
