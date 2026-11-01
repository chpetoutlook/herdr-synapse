"""Packaging checks: manifest grammar, launcher, hook gate, console wrapper, stubs."""

import json
import os
import re
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from herdr_team import VERSION
from support import PLUGIN_ROOT, symlink_or_skip, TempState, write_fake_herdr

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.9/3.10
    tomllib = None

MANIFEST = PLUGIN_ROOT / "herdr-plugin.toml"
LAUNCHER = PLUGIN_ROOT / "bin" / "herdr-synapse"
HOOK = PLUGIN_ROOT / "bin" / "hook"
CONSOLE_SH = PLUGIN_ROOT / "console.sh"


def clean_env(extra=None):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "HERDR_TEAM_PYTHON": sys.executable,
    }
    if extra:
        env.update(extra)
    return env


def run(cmd, env=None, timeout=20):
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env or clean_env(), cwd=os.fspath(PLUGIN_ROOT))



def posix_only(test):
    """The POSIX launcher, hook gate and console wrapper are not the shipped
    ones on Windows: the port ships the .cmd/.ps1 twins, and those are
    covered by their own tests (tests/test_launcher_guard.py and the W-6
    launcher work). So the POSIX scripts are exercised on unix only
    (2026-10-21 port fix, W-9)."""
    return unittest.skipIf(os.name == "nt",
                           "the POSIX launcher, hook gate and console wrapper are not the shipped ones on Windows")




class LauncherTests(unittest.TestCase):
    @posix_only
    def test_launcher_works_through_a_symlink(self):
        """``install-cli`` symlinks ~/.local/bin/herdr-synapse to bin/herdr-synapse; the launcher must follow the link."""
        import subprocess
        import sys
        import tempfile

        if os.name == "nt":
            # The Windows launcher is not a symlink: install-cli writes a
            # .cmd shim that resolves the plugin root from herdr's own
            # registry, and the .cmd resolves its own root from %~dp0 - so a
            # symlink to it does not lead back to the plugin. The
            # symlink-chain scenario is a POSIX one (2026-10-21 port fix,
            # W-9).
            self.skipTest("the Windows launcher is a .cmd shim, not a symlink; the symlink-chain scenario is POSIX-only")

        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "bin" / "herdr-synapse"
            link.parent.mkdir()
            symlink_or_skip(PLUGIN_ROOT / "bin" / "herdr-synapse", link)
            nested = Path(tmp) / "nested"
            symlink_or_skip(link, nested)  # a link to a link
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "HERDR_TEAM_PYTHON": sys.executable}
            for path in (link, nested):
                proc = subprocess.run([os.fspath(path), "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode())
                self.assertEqual(proc.stdout.decode().strip(), "herdr-synapse {}".format(VERSION))


class ManifestTests(unittest.TestCase):
    def test_manifest_is_regular_file_with_expected_entries(self):
        self.assertTrue(MANIFEST.is_file() and not MANIFEST.is_symlink())
        text = MANIFEST.read_text(encoding="utf-8")
        self.assertIn('id = "herdr-synapse"', text)
        self.assertIn('min_herdr_version = "0.8.2"', text)
        self.assertIn('version = "{}"'.format(VERSION), text)
        ons = re.findall(r'^on = "([^"]+)"', text, re.M)
        # the upstream hooks are all there; the port adds the windows twins
        for hook in ("pane.agent_detected", "pane.closed", "pane.exited"):
            self.assertIn(hook, ons)
        # The manifest is ONE file and the port registers each hook once per
        # PLATFORM - the unix command with platforms = [macos, linux] and the
        # windows one with platforms = [windows] - so the name appears twice
        # on every platform, and herdr picks the entry for the platform it
        # runs on. The upstream's rule (one entry per event) does not hold for
        # a port that ships both platforms in one manifest (2026-10-21 port
        # fix, W-22).
        for hook in set(ons):
            self.assertEqual(ons.count(hook), 2,
                             "the hook {} is registered once per platform".format(hook))
        action_ids = re.findall(r'^\[\[actions\]\]\nid = "([^"]+)"', text, re.M)
        pane_ids = re.findall(r'^\[\[panes\]\]\nid = "([^"]+)"', text, re.M)
        # The upstream entrypoints are all there, and the port added a
        # -windows twin for every one of them (2026-10-20 port change).
        for ident in ("team-up", "compose", "console", "who", "usage", "knowledge", "mission", "whiteboard", "toggle-view", "daemon-start"):
            self.assertIn(ident, action_ids)
        for ident in ("console", "compose", "picker", "asks", "usage", "knowledge", "mission", "whiteboard"):
            self.assertIn(ident, pane_ids)
        # the port registers a -windows twin for every one of them, in the one
        # manifest, on every platform (2026-10-20 port change)
        for ident in action_ids + pane_ids:
            if ident.endswith("-windows"):
                continue
            self.assertIn(ident + "-windows", action_ids + pane_ids,
                          "the port registers a -windows twin for {}".format(ident))
        # the port registers a windows startup beside the unix one
        self.assertEqual(text.count("[[startup]]"), 2)
        self.assertNotIn("[[build]]", text)
        for ident in action_ids + pane_ids:
            self.assertRegex(ident, r"^[a-z][a-z0-9-]*$")

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_manifest_parses_as_toml(self):
        doc = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(doc["id"], "herdr-synapse")
        # the port adds the windows scope and a windows startup beside the
        # unix one, in the one manifest, on every platform
        self.assertEqual(doc["platforms"], ["macos", "linux", "windows"])
        self.assertEqual(len(doc["startup"]), 2)
        self.assertEqual(doc["startup"][0]["command"], ["./bin/herdr-synapse", "daemon", "start"])
        self.assertEqual(doc["startup"][0]["platforms"], ["macos", "linux"])
        self.assertEqual(doc["startup"][1]["platforms"], ["windows"])
        contexts = [a["contexts"] for a in doc["actions"]]
        upstream_contexts = [["global"], ["pane"], ["global"], ["global"], ["global"], ["global"], ["global"], ["global"], ["global"], ["global"]]
        # the port adds a windows twin for every action, so the list is the
        # upstream one twice over, and the manifest alternates: the unix
        # action, then its windows twin
        self.assertEqual(len(contexts), 2 * len(upstream_contexts))
        self.assertEqual(contexts[0::2], upstream_contexts)
        # the manifest alternates: the unix pane, then its windows twin, so
        # the upstream panes are the even ones (2026-10-20 port change)
        panes = doc["panes"][0::2]
        self.assertEqual(panes[0]["command"], ["sh", "console.sh"])
        self.assertEqual(panes[1]["width"], "80%")
        self.assertEqual(panes[1]["height"], 12)
        self.assertEqual(panes[2]["height"], 28)
        self.assertEqual((panes[3]["id"], panes[3]["command"]), ("asks", ["./bin/herdr-synapse", "asks-pane"]))
        self.assertEqual((panes[4]["id"], panes[4]["command"]), ("usage", ["./bin/herdr-synapse", "usage-pane"]))
        for pane in panes[1:]:
            self.assertEqual(pane["placement"], "popup")
        self.assertEqual(panes[0]["placement"], "split")

    def test_manifest_commands_exist_and_are_executable(self):
        text = MANIFEST.read_text(encoding="utf-8")
        for rel in set(re.findall(r'"(\./bin/[a-z-]+)"', text)):
            path = PLUGIN_ROOT / rel
            self.assertTrue(path.is_file(), rel)
            self.assertTrue(os.access(path, os.X_OK), rel)
        self.assertTrue(CONSOLE_SH.is_file())


class LauncherTests(unittest.TestCase):
    @posix_only
    def test_shebang_and_exec_bit(self):
        for script in (LAUNCHER, HOOK, CONSOLE_SH):
            self.assertTrue(script.read_text().startswith("#!/bin/sh\n"), script)
            self.assertTrue(stat.S_IMODE(script.stat().st_mode) & stat.S_IXUSR, script)

    @posix_only
    def test_version_through_launcher(self):
        proc = run([os.fspath(LAUNCHER), "--version"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.decode().strip(), "herdr-synapse {}".format(VERSION))

    @posix_only
    def test_json_error_through_launcher(self):
        proc = run([os.fspath(LAUNCHER), "nope", "--json"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stderr.decode())["code"], "usage")

    @posix_only
    def test_refuses_old_python(self):
        with TempState() as ts:
            fake_py = ts.tmp / "python3"
            fake_py.write_text("#!/bin/sh\necho 'Python 3.8.19'\n")
            os.chmod(fake_py, 0o700)
            proc = run([os.fspath(LAUNCHER), "--version"], env=clean_env({"HERDR_TEAM_PYTHON": os.fspath(fake_py)}))
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(json.loads(proc.stderr.decode())["code"], "python_too_old")

    @posix_only
    def test_falls_back_to_path_python(self):
        env = clean_env()
        del env["HERDR_TEAM_PYTHON"]
        proc = run([os.fspath(LAUNCHER), "--version"], env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @posix_only
    def test_missing_python_message(self):
        env = clean_env({"PATH": "/nonexistent", "HERDR_TEAM_PYTHON": "/nonexistent/python3"})
        proc = run([os.fspath(LAUNCHER), "--version"], env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("python", proc.stderr.decode().lower())

    def test_pythonpath_is_plugin_root(self):
        proc = run([sys.executable, "-c", "import herdr_team, os; print(os.path.dirname(os.path.dirname(herdr_team.__file__)))"], env=clean_env({"PYTHONPATH": os.fspath(PLUGIN_ROOT)}))
        self.assertEqual(Path(proc.stdout.decode().strip()), PLUGIN_ROOT)


class HookGateTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.fake_herdr = write_fake_herdr(self.ts.tmp)

    def hook_env(self):
        return clean_env({
            "HERDR_TEAM_STATE_DIR": os.fspath(self.ts.state_root),
            "HERDR_SOCKET_PATH": os.fspath(self.ts.socket_path),
            "HERDR_PLUGIN_EVENT": "pane.agent_detected",
            "HERDR_PLUGIN_EVENT_JSON": json.dumps({"type": "pane_agent_detected", "pane_id": "w2:p1"}),
            "HERDR_PLUGIN_ID": "herdr-synapse",
            "HERDR_BIN_PATH": os.fspath(self.fake_herdr),
        })

    def start_sleeper(self):
        proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(proc.pid)], stdout=subprocess.PIPE).stdout.decode()
        self.assertTrue(start.strip())
        # Return it untrimmed on purpose: both the daemon and the sh gate must trim.
        return proc, start.rstrip("\n")

    def write_daemon_json(self, pid, start_time):
        self.ts.session.daemon_json.write_text(json.dumps({"pid": pid, "start_time": start_time, "beat_at": "now", "version": VERSION}) + "\n")

    @posix_only
    def test_live_daemon_short_circuits_fast(self):
        proc, start = self.start_sleeper()
        self.write_daemon_json(proc.pid, start)
        # warm-up run, then measure
        run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        t0 = time.monotonic()
        result = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        gate = time.monotonic() - t0
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        # A regression guard, not a wall-clock limit. A loaded machine slows the gate and the path it exists to avoid
        # alike, so the two are measured here, one after the other, and compared: falling through starts the
        # interpreter, which the short circuit never does. A fixed 0.2 s ceiling failed at load average 40 while the
        # gate was still doing its job.
        self.write_daemon_json(2 ** 22 - 1, "Thu Jan  1 00:00:00 2026")
        run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        t0 = time.monotonic()
        fell_through = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        through = time.monotonic() - t0
        self.assert_reconciler_ran(fell_through)
        self.assertLess(gate * 3, through, "gate {:.3f}s, fall-through {:.3f}s".format(gate, through))
        self.assertLess(gate, 2.0, "gate took {:.3f}s".format(gate))

    def assert_reconciler_ran(self, result):
        # The slow path runs `herdr-synapse hook-event`, which exits 0 in every non-bug case and prints one JSON line.
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.decode().strip().splitlines()[-1])
        self.assertEqual(payload["event"], "agent_detected")
        self.assertEqual(payload["pane_id"], "w2:p1")
        self.assertIn("skipped", payload)

    @posix_only
    def test_dead_pid_falls_through_to_python(self):
        self.write_daemon_json(2 ** 22 - 1, "Thu Jan  1 00:00:00 2026")
        result = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        self.assert_reconciler_ran(result)

    @posix_only
    def test_start_time_mismatch_falls_through(self):
        proc, _start = self.start_sleeper()
        self.write_daemon_json(proc.pid, "Thu Jan  1 00:00:00 2026")
        result = run([os.fspath(HOOK), "agent_detected"], env=self.hook_env())
        self.assert_reconciler_ran(result)

    @posix_only
    def test_named_session_slug_in_gate(self):
        ts = TempState(slug="rig-3")
        self.addCleanup(ts.cleanup)
        proc, start = self.start_sleeper()
        ts.session.daemon_json.write_text(json.dumps({"pid": proc.pid, "start_time": start}) + "\n")
        env = self.hook_env()
        env["HERDR_TEAM_STATE_DIR"] = os.fspath(ts.state_root)
        env["HERDR_SOCKET_PATH"] = os.fspath(ts.socket_path)
        result = run([os.fspath(HOOK), "pane_closed"], env=env)
        self.assertEqual((result.returncode, result.stderr), (0, b""))

    @posix_only
    def test_no_state_dir_falls_through(self):
        env = self.hook_env()
        del env["HERDR_TEAM_STATE_DIR"]
        result = run([os.fspath(HOOK), "agent_detected"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'"event"', result.stdout)


class ConsoleWrapperTests(unittest.TestCase):
    @posix_only
    def test_wrapper_prints_hint_on_failure(self):
        # From a plain shell (no HERDR_PLUGIN_ENTRYPOINT_ID) the console refuses with not_a_plugin_pane (exit 1)
        # and the wrapper reports it.
        proc = subprocess.run(["sh", os.fspath(CONSOLE_SH)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=clean_env(), cwd=os.fspath(PLUGIN_ROOT))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(b"not_a_plugin_pane", proc.stderr)
        out = proc.stdout.decode()
        self.assertIn("exited with status 1", out)
        self.assertIn("herdr-synapse ui console", out)


class StubModuleTests(unittest.TestCase):
    def test_every_convention_module_exists(self):
        expected = [
            "paths", "api", "errors", "store", "sanitize", "render", "identity", "roster", "charter", "cli",
            "cmd_board", "cmd_roster", "cmd_misc", "cmd_hooks", "cmd_skill", "cmd_daemon", "daemon", "gate",
            "nudge", "ledger", "hooks", "claude_settings", "console", "picker", "compose", "tui_model",
        ]
        for name in expected:
            self.assertTrue((PLUGIN_ROOT / "herdr_team" / (name + ".py")).is_file(), name)

    def test_no_fcntl_outside_store(self):
        for path in (PLUGIN_ROOT / "herdr_team").glob("*.py"):
            if path.name == "store.py":
                continue
            self.assertNotIn("import fcntl", path.read_text(encoding="utf-8"), path.name)

    def test_no_match_statement_or_runtime_union(self):
        for path in (PLUGIN_ROOT / "herdr_team").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"^\s*match .*:\s*$", path.name)
            if "from __future__ import annotations" not in text and path.name != "__init__.py":
                self.fail("{} lacks the annotations future import".format(path.name))


if __name__ == "__main__":
    unittest.main()
