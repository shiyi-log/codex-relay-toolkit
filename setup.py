#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Register every third-party Codex provider in CC Switch with the retry bridge.

Run this after adding, duplicating or renaming a relay in CC Switch:

    python3 ~/.ccswitch-retry-bridge/setup.py --dry-run   # show what would change
    python3 ~/.ccswitch-retry-bridge/setup.py

It is idempotent. For every third-party (non-official) Codex provider it:

  1. works out the provider's real upstream, even when the provider was
     *duplicated* in the CC Switch UI after a previous run (a duplicate copies
     the bridge URL of the provider it was cloned from, so it would otherwise
     keep pointing at someone else's route). For a copy the mount only says who
     it came from, so the real upstream is resolved from this provider's own
     recorded original or from CC Switch's `website_url`, and accepted only if
     this provider's own API key answers on it;
  2. rewrites base_url to
         http://127.0.0.1:<port>/p/<provider-id><original-path>
     so CC Switch hands the call to the bridge;
  3. points the provider's usage_script at its own relay (same duplicate trap);
     this is what makes CC Switch's balance query show the right relay;
  4. puts the provider into CC Switch's failover queue and gives it a
     sort_index if it has none.

Original base_urls are kept in originals.json; `restore.py` undoes everything.
Restart CC Switch afterwards so it reloads the URLs.
"""

import json
import os
import re
import sqlite3
import sys
import urllib.request
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROUTES_FILE = os.environ.get("BRIDGE_ROUTES", os.path.join(BASE_DIR, "routes.json"))
ORIGINALS_FILE = os.environ.get("BRIDGE_ORIGINALS",
                                os.path.join(BASE_DIR, "originals.json"))
DB_PATH = os.environ.get("BRIDGE_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))
PORT = int(os.environ.get("BRIDGE_PORT", "15888"))
ATTEMPTS = int(os.environ.get("BRIDGE_ATTEMPTS", "100"))
MARKER = "127.0.0.1:%d/p/" % PORT
# official ChatGPT Codex backend (subscription login, not api.openai.com)
OFFICIAL_UPSTREAM = "https://chatgpt.com/backend-api/codex"
AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
OFFICIAL_ATTEMPTS = int(os.environ.get("BRIDGE_OFFICIAL_ATTEMPTS", "10"))
MOUNT_RE = re.compile(r"/p/([0-9a-fA-F]{8}-[0-9a-fA-F-]{4,})")
BASE_URL_RE = re.compile(r'base_url\s*=\s*"([^"]+)"')
# how long to wait when probing a relay for this provider's own key
PROBE_TIMEOUT = float(os.environ.get("BRIDGE_PROBE_TIMEOUT", "10"))
DRY_RUN = "--dry-run" in sys.argv


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:
            pass
    return default


def upstream_root(url):
    parts = urlsplit(url)
    return "%s://%s" % (parts.scheme, parts.netloc)


def probe_prefix(root, key):
    """Find a path prefix under `root` that answers /models for this API key.

    Returns "" , "/v1" , ... or None. Relays are inconsistent: some serve the
    OpenAI-compatible API at the site root, some only under /v1. Probing the
    provider's *own* key is also how we tell a real upstream apart from a
    neighbouring relay that merely happens to answer.
    """
    if not key:
        return None
    for prefix in ("", "/v1"):
        try:
            req = urllib.request.Request(
                root + prefix + "/models",
                headers={"Authorization": "Bearer " + key,
                         # a few relays 403 the default python-urllib UA
                         "User-Agent": "curl/8.7.1",
                         "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as fh:
                if 200 <= fh.status < 300:
                    return prefix
        except Exception:
            continue
    return None


def resolve_original(pid, known, website, key):
    """Real upstream for a provider whose base_url is a bridge mount.

    A copied provider inherits the *source's* mount, so the mount only tells us
    who it was copied from - never what this provider actually is. Using the
    source's upstream is the trap (new relay silently sends its key to the old
    relay, which answers 401, and both the balance query and the real-price
    query fail for it).

    Ground truth, in order:
      1. this provider's own recorded original (originals.json / previous route)
      2. the site URL CC Switch stores on the provider (`website_url`)
    A candidate that carries no path is accepted only if this provider's own key
    answers on it, so we never point a provider at somebody else's relay.

    -> (original_base_url, source, verified) or (None, "", False)
    """
    candidates = []
    recorded = (known.get(pid) or "").strip().rstrip("/")
    if recorded:
        candidates.append((recorded, "originals.json"))
    site = (website or "").strip().rstrip("/")
    if site:
        site_root = upstream_root(site)
        if site_root and all(site_root != upstream_root(c) for c, _ in candidates):
            candidates.append((site_root, "CC Switch website_url"))
    if not candidates:
        return None, "", False

    unverified = None
    for candidate, source in candidates:
        root = upstream_root(candidate)
        prefix = urlsplit(candidate).path.rstrip("/")
        if prefix:
            # a real base_url path (e.g. https://relay/v1) is ground truth itself
            return root + prefix, source, True
        probed = probe_prefix(root, key)
        if probed is not None:
            return root + probed, source, True
        if unverified is None:
            unverified = (root, source, False)
    return unverified


def normalize_client_config(conf):
    """Give every relay provider the same client flavour.

    Uniform across providers so it does not matter which one CC Switch has
    selected (its failover switches between them):

      name                  -> "OpenAI"      (what the UI shows)
      requires_openai_auth  -> BRIDGE_REQUIRE_OPENAI_AUTH (default false)
                               false = log in with the relay's API key
                                       (`codex login --with-api-key`), the right
                                       choice when the ChatGPT account is out of
                                       quota or unavailable
                               true  = use auth.json's ChatGPT OAuth login
      supports_websockets   -> false         (the proxy/bridge only speaks HTTP)
      http_headers          -> removed       (the bridge re-signs per relay)
      thread_context        -> removed       (deprecated, Codex warns about it)

    The endpoint (base_url) still points at the proxy/bridge, so inference keeps
    going to the relays.
    """
    require_oauth = os.environ.get("BRIDGE_REQUIRE_OPENAI_AUTH", "0") == "1"
    out = []
    in_provider = False
    for line in conf.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_provider = stripped.startswith("[model_providers.")
            out.append(line)
            continue
        if in_provider:
            if stripped.startswith("name"):
                out.append('name = "OpenAI"')
                continue
            if stripped.startswith(("requires_openai_auth", "supports_websockets",
                                    "http_headers", "thread_context")):
                continue
            if stripped.startswith("wire_api"):
                out.append(line)
                out.append("supports_websockets = false")
                out.append("requires_openai_auth = %s"
                           % ("true" if require_oauth else "false"))
                continue
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def main():
    if not os.path.exists(DB_PATH):
        sys.exit("CC Switch database not found: %s" % DB_PATH)

    originals = load_json(ORIGINALS_FILE, {})
    routes = load_json(ROUTES_FILE, {}).get("routes", {})

    # ---- pass 1: learn every provider's real upstream --------------------
    known = {}
    for pid, url in originals.items():
        known[pid] = url
    for pid, entry in routes.items():
        if entry.get("original_base_url"):
            known.setdefault(pid, entry["original_base_url"])

    conn = sqlite3.connect(DB_PATH)
    rows = list(conn.execute(
        "SELECT id, name, settings_config, meta, in_failover_queue, sort_index, "
        "website_url FROM providers WHERE app_type='codex' "
        "AND (category IS NULL OR category<>'official') ORDER BY sort_index IS NULL, sort_index"))
    if not rows:
        sys.exit("no third-party codex providers found")

    for pid, name, cfg_raw, _meta, _fq, _si, _web in rows:
        conf = (json.loads(cfg_raw).get("config") or "")
        match = BASE_URL_RE.search(conf)
        # a *direct* base_url is the freshest ground truth: the user may have
        # re-pointed this provider in CC Switch since the last run
        if match and MARKER not in match.group(1):
            known[pid] = match.group(1)

    # ---- pass 2: rewrite each provider -----------------------------------
    max_index = max([r[5] for r in rows if r[5] is not None] or [0])
    new_routes = {}
    bridged = joined = 0

    for pid, name, cfg_raw, meta_raw, in_queue, sort_index, website_url in rows:
        cfg = json.loads(cfg_raw)
        conf = cfg.get("config") or ""
        match = BASE_URL_RE.search(conf)
        if not match:
            print("  skip %-40s (no base_url)" % name[:40])
            continue
        current = match.group(1)

        inherited_from = None
        unverified = False
        if MARKER in current:
            mount_match = MOUNT_RE.search(current)
            src = mount_match.group(1) if mount_match else None
            if src and src != pid:
                inherited_from = src
            key = (cfg.get("auth") or {}).get("OPENAI_API_KEY") or ""
            original, origin_source, verified = resolve_original(
                pid, known, website_url, key)
            unverified = bool(original) and not verified
        else:
            original, origin_source, verified = current, "base_url", True

        if not original:
            print("  skip %-40s (copied from %s, and this provider's own upstream "
                  "is unknown;\n       set its relay URL in CC Switch once, or fill "
                  "in website_url, then re-run)"
                  % (name[:40], (inherited_from or "another provider")[:8]))
            continue
        if unverified:
            print("  WARN %-40s upstream from %s did not answer /models for this "
                  "key; check it" % (name[:40], origin_source))

        original = original.rstrip("/")
        root = upstream_root(original)
        suffix = urlsplit(original).path
        mount = "/p/%s" % pid
        new_url = "http://127.0.0.1:%d%s%s" % (PORT, mount, suffix)

        changed = []
        new_conf = conf
        if current != new_url:
            new_conf = new_conf.replace('base_url = "%s"' % current,
                                        'base_url = "%s"' % new_url)
            changed.append("base_url")
            bridged += 1
        new_conf = normalize_client_config(new_conf)
        if new_conf != conf:
            if "base_url" not in changed:
                changed.append("official-flavor")
        cfg["config"] = new_conf

        # usage_script must query this provider's own relay (duplicates inherit
        # the source provider's value, which is what broke balance reporting)
        meta = json.loads(meta_raw) if meta_raw else {}
        usage = meta.setdefault("usage_script", {})
        if usage.get("baseUrl") != root:
            usage["baseUrl"] = root
            usage["enabled"] = True
            usage.setdefault("language", "javascript")
            usage.setdefault("timeout", 10)
            usage.setdefault("templateType", "general")
            usage.setdefault("autoQueryInterval", 5)
            changed.append("usage")
            cfg_meta = json.dumps(meta, ensure_ascii=False)
        else:
            cfg_meta = meta_raw

        if not in_queue:
            changed.append("failover-queue")
            joined += 1
        if sort_index is None:
            max_index += 1
            sort_index = max_index
            changed.append("sort_index")

        if not DRY_RUN:
            conn.execute(
                "UPDATE providers SET settings_config=?, meta=?, in_failover_queue=1, "
                "sort_index=? WHERE id=? AND app_type='codex'",
                (json.dumps(cfg, ensure_ascii=False), cfg_meta, sort_index, pid))

        new_routes[pid] = {
            "mount": mount,
            "prefix": suffix,
            "upstream": root,
            "name": name,
            "auth": (json.loads(cfg_raw).get("auth") or {}).get("OPENAI_API_KEY") or "",
            "original_base_url": original,
        }
        known[pid] = original
        originals[pid] = original

        note = ""
        if inherited_from:
            src_name = next((r[1] for r in rows if r[0] == inherited_from), inherited_from)
            note = ("  (copied from '%s' - mount no longer reused; own upstream "
                    "taken from %s)" % (src_name, origin_source))
        print("  P%-3s %-40s %s -> %s%s"
              % (sort_index, name[:40], original, new_url,
                 "  [" + ",".join(changed) + "]" if changed else "  [ok]"))
        if note:
            print("        " + note.strip())

    # ---- official ChatGPT subscription account (OAuth) --------------------
    # It cannot be a CC Switch provider mount (official providers have no
    # base_url and the client config drops the custom provider), but the bridge
    # can still use it as the last rotation target: it reads the OAuth token
    # straight out of Codex's auth.json on every attempt.
    official_pids = []
    if os.environ.get("BRIDGE_OFFICIAL", "1") != "0":
        for pid, name, cfg_raw in conn.execute(
                "SELECT id, name, settings_config FROM providers "
                "WHERE app_type='codex' AND category='official'"):
            auth = (json.loads(cfg_raw).get("auth") or {})
            if auth.get("auth_mode") != "chatgpt":
                continue
            new_routes[pid] = {
                "mount": "/p/%s" % pid,
                "prefix": "",
                "upstream": OFFICIAL_UPSTREAM,
                "name": "%s (我的账号)" % name,
                "auth_type": "oauth",
                "auth_file": AUTH_FILE,
                "max_attempts": OFFICIAL_ATTEMPTS,
                "original_base_url": OFFICIAL_UPSTREAM,
            }
            official_pids.append(pid)
            print("  LAST  %-40s %s  [oauth, max %d attempts]"
                  % (name[:40], OFFICIAL_UPSTREAM, OFFICIAL_ATTEMPTS))

    conn.commit()
    conn.close()

    # round-robin order == CC Switch's failover priority, official account last
    order = [r[0] for r in rows if r[0] in new_routes] + official_pids

    if DRY_RUN:
        print("\n[dry-run] routes.json / CC Switch not touched. Drop --dry-run to apply.")
        print("would write %d route(s): %s"
              % (len(new_routes), ", ".join(new_routes[p]["name"] for p in order)))
        return

    with open(ROUTES_FILE, "w") as fh:
        json.dump({"port": PORT, "attempts": ATTEMPTS, "order": order,
                   "routes": new_routes}, fh, ensure_ascii=False, indent=2)
    os.chmod(ROUTES_FILE, 0o600)          # routes.json carries the relay API keys
    with open(ORIGINALS_FILE, "w") as fh:
        json.dump(originals, fh, ensure_ascii=False, indent=2)

    print("\n%d provider(s) total, %d newly bridged, %d newly queued"
          % (len(new_routes), bridged, joined))
    print("round-robin order: %s" % ", ".join(
        new_routes[p]["name"] for p in order))
    print("attempts per request: %d" % ATTEMPTS)
    print("Restart CC Switch so it reloads the provider URLs.")


if __name__ == "__main__":
    main()
