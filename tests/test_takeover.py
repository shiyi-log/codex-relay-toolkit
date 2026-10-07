#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for takeover.py - the tool that undoes CC Switch handing Codex back to
the official account (which silently takes the bridge out of the path).

    python3 tests/test_takeover.py
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import takeover as tk                                        # noqa: E402

BRIDGED = ('model_provider = "custom"\n[model_providers.custom]\n'
           'base_url = "http://127.0.0.1:15888/p/5fee0521-be88-4400-b9e9-65d6b64a6219/v1"\n')
OFFICIAL_URL = ('model_provider = "cc-switch-official"\n[model_providers.cc-switch-official]\n'
                'base_url = "http://127.0.0.1:15721/v1"\n')


class DecisionTest(unittest.TestCase):
    def test_is_bridged(self):
        self.assertTrue(tk.is_bridged(BRIDGED))
        self.assertFalse(tk.is_bridged('base_url = "https://api.deepseek.com/v1"'))

    def test_pick_provider_follows_the_bridge_ranking(self):
        routes = [{"id": "b", "name": "B"}, {"id": "a", "name": "A"},
                  {"id": "codex-official", "name": "acct", "auth_type": "oauth"},
                  {"id": "sidecar-deepseek", "name": "DS"}]
        provs = {"a": {"name": "A", "config": BRIDGED},
                 "b": {"name": "B", "config": BRIDGED},
                 "codex-official": {"name": "acct", "config": "no base_url"}}
        pid, name = tk.pick_provider(routes, provs)
        self.assertEqual((pid, name), ("b", "B"))       # ranking wins

    def test_pick_provider_skips_unbridged_and_sidecars(self):
        routes = [{"id": "sidecar-x", "name": "S"}, {"id": "official", "name": "O",
                                                     "auth_type": "oauth"}]
        provs = {"official": {"name": "O", "config": OFFICIAL_URL},
                 "unbridged": {"name": "U", "config": 'base_url = "https://x/v1"'}}
        self.assertEqual(tk.pick_provider(routes, provs), (None, None))

    def test_pick_provider_falls_back_when_the_ranking_is_empty(self):
        provs = {"a": {"name": "A", "config": BRIDGED}}
        self.assertEqual(tk.pick_provider([], provs), ("a", "A"))

    def test_takeover_active_reads_the_client_config(self):
        tmp = tempfile.mkdtemp(prefix="takeover-")
        path = os.path.join(tmp, "config.toml")
        old = tk.CODEX_CONFIG
        try:
            tk.CODEX_CONFIG = path
            open(path, "w").write(OFFICIAL_URL)
            self.assertTrue(tk.takeover_active())        # 15721 = CC Switch proxy
            open(path, "w").write('base_url = "https://chatgpt.com/backend-api/codex"\n')
            self.assertFalse(tk.takeover_active())       # straight to the account
            open(path, "w").write(BRIDGED)
            self.assertTrue(tk.takeover_active())
        finally:
            tk.CODEX_CONFIG = old

    def test_set_current_provider_preserves_everything_else(self):
        tmp = tempfile.mkdtemp(prefix="takeover-")
        path = os.path.join(tmp, "settings.json")
        with open(path, "w") as fh:
            json.dump({"currentProviderCodex": "codex-official", "language": "zh",
                       "nested": {"keep": True}}, fh)
        old = tk.SETTINGS
        try:
            tk.SETTINGS = path
            tk.set_current_provider("abc-123")
        finally:
            tk.SETTINGS = old
        data = json.load(open(path))
        self.assertEqual(data["currentProviderCodex"], "abc-123")
        self.assertEqual(data["language"], "zh")
        self.assertEqual(data["nested"], {"keep": True})
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")


class FixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="takeover-fix-")
        self.saved = (tk.SETTINGS, tk.CODEX_CONFIG, tk.DISABLED, tk.bridge_status,
                      tk.providers, tk.restart_app, tk.log)
        self.settings = os.path.join(self.tmp, "settings.json")
        self.config = os.path.join(self.tmp, "config.toml")
        with open(self.settings, "w") as fh:
            json.dump({"currentProviderCodex": "codex-official"}, fh)
        with open(self.config, "w") as fh:
            fh.write('base_url = "https://chatgpt.com/backend-api/codex"\n')
        tk.SETTINGS = self.settings
        tk.CODEX_CONFIG = self.config
        tk.DISABLED = os.path.join(self.tmp, "TAKEOVER.DISABLED")
        tk.log = lambda *a, **k: None
        tk.restart_app = lambda: None
        self.restarted = []
        tk.restart_app = lambda: self.restarted.append(1)

    def tearDown(self):
        (tk.SETTINGS, tk.CODEX_CONFIG, tk.DISABLED, tk.bridge_status,
         tk.providers, tk.restart_app, tk.log) = self.saved

    def args(self, **kw):
        class A:
            quiet = False
            no_restart = False
        for k, v in kw.items():
            setattr(A, k, v)
        return A()

    def test_disabled_marker_means_hands_off(self):
        open(tk.DISABLED, "w").close()
        tk.bridge_status = lambda: (True, [{"id": "a", "name": "A"}])
        tk.providers = lambda: {"a": {"name": "A", "config": BRIDGED}}
        self.assertEqual(tk.fix(self.args()), 0)
        self.assertEqual(json.load(open(self.settings))["currentProviderCodex"],
                         "codex-official")

    def test_unhealthy_bridge_is_left_alone(self):
        tk.bridge_status = lambda: (False, [])
        tk.providers = lambda: {"a": {"name": "A", "config": BRIDGED}}
        self.assertEqual(tk.fix(self.args()), 0)
        self.assertEqual(json.load(open(self.settings))["currentProviderCodex"],
                         "codex-official")

    class FakeTime:
        """A clock we control: fake sleep still advances it, so the grace
        windows actually expire."""
        def __init__(self):
            self.t = 0.0
        def time(self):
            self.t += 1.0
            return self.t
        def sleep(self, _seconds):
            pass

    def test_handoff_is_undone(self):
        tk.bridge_status = lambda: (True, [{"id": "a", "name": "A"}])
        tk.providers = lambda: {"a": {"name": "A", "config": BRIDGED}}
        real_time, real_active = tk.time, tk.takeover_active
        tk.time = self.FakeTime()
        tk.takeover_active = lambda: tk.time.t > 1           # reacts after one poll
        try:
            self.assertEqual(tk.fix(self.args()), 0)
        finally:
            tk.time, tk.takeover_active = real_time, real_active
        self.assertEqual(json.load(open(self.settings))["currentProviderCodex"], "a")
        self.assertEqual(self.restarted, [])                 # no app restart needed

    def test_restart_is_used_when_cc_switch_ignores_the_change(self):
        tk.bridge_status = lambda: (True, [{"id": "a", "name": "A"}])
        tk.providers = lambda: {"a": {"name": "A", "config": BRIDGED}}
        real_time, real_active, real_restart = tk.time, tk.takeover_active, tk.restart_app
        real_cooldown = tk.RESTART_COOLDOWN
        tk.RESTART_COOLDOWN = 0            # the cooldown has its own guard below
        state = {"restarted": False}
        tk.time = self.FakeTime()
        tk.takeover_active = lambda: state["restarted"]

        def do_restart():
            self.restarted.append(1)
            state["restarted"] = True

        tk.restart_app = do_restart
        try:
            rc = tk.fix(self.args())
        finally:
            (tk.time, tk.takeover_active, tk.restart_app,
             tk.RESTART_COOLDOWN) = (real_time, real_active, real_restart, real_cooldown)
        self.assertEqual(self.restarted, [1])                # it restarted the app
        self.assertEqual(rc, 0)


class CooldownTest(unittest.TestCase):
    def test_restart_is_skipped_inside_the_cooldown(self):
        tmp = tempfile.mkdtemp(prefix="takeover-cd-")
        saved = (tk.STATE, tk.RESTART_COOLDOWN)
        tk.STATE = os.path.join(tmp, "state.json")
        tk.RESTART_COOLDOWN = 900
        try:
            tk.note_restart()
            waited = tk.time.time() - tk.last_restart()
            self.assertLess(waited, tk.RESTART_COOLDOWN)      # just restarted
        finally:
            tk.STATE, tk.RESTART_COOLDOWN = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
