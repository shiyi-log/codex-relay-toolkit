#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Undo setup.py: put every provider's original base_url back."""

import json
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ORIGINALS_FILE = os.path.join(BASE_DIR, "originals.json")
DB_PATH = os.path.expanduser("~/.cc-switch/cc-switch.db")


def main():
    if not os.path.exists(ORIGINALS_FILE):
        sys.exit("nothing to restore: %s is missing" % ORIGINALS_FILE)
    originals = json.load(open(ORIGINALS_FILE))
    conn = sqlite3.connect(DB_PATH)
    restored = 0
    for pid, original in originals.items():
        row = conn.execute(
            "SELECT name, settings_config FROM providers WHERE id=? AND app_type='codex'",
            (pid,)).fetchone()
        if not row:
            print("  missing provider %s" % pid)
            continue
        name, cfg_raw = row
        cfg = json.loads(cfg_raw)
        conf = cfg.get("config") or ""
        match = re.search(r'base_url\s*=\s*"([^"]+)"', conf)
        if not match:
            continue
        if match.group(1) == original:
            print("  ok   %-38s already direct" % name[:38])
            continue
        cfg["config"] = conf.replace(
            'base_url = "%s"' % match.group(1), 'base_url = "%s"' % original)
        conn.execute(
            "UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex'",
            (json.dumps(cfg, ensure_ascii=False), pid))
        restored += 1
        print("  ok   %-38s -> %s" % (name[:38], original))
    conn.commit()
    conn.close()
    print("\n%d provider(s) restored. Restart CC Switch." % restored)


if __name__ == "__main__":
    main()
