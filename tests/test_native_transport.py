"""Transport bounds and cleanup use owned fixture processes, not coding agents."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from herdr_team.native_transport import JsonLines, NativeError, OpenCodeServer, _stop_owned, metadata_command
from support import requires_posix


class Lines(unittest.TestCase):
    def child(self, source, cwd, timeout=5):
        return JsonLines([sys.executable, "-u", "-c", source], cwd, dict(os.environ), timeout)

    def test_fixture_process_is_closed_after_metadata(self):
        source = 'import json,sys; frame=json.loads(sys.stdin.readline()); print(json.dumps({"id":frame["id"],"result":{"models":[]}})); sys.stdin.read()'
        with tempfile.TemporaryDirectory() as cwd:
            with self.child(source, cwd) as stream:
                self.assertEqual(stream.rpc("model/list", {}), {"models": []})
            self.assertIsNotNone(stream.proc.poll())
            self.assertTrue(stream.proc.stdout.closed)

    def test_native_requests_cannot_authorize_commands_or_tools(self):
        source = 'import json,sys; f=json.loads(sys.stdin.readline()); print(json.dumps({"id":99,"method":"command/exec","params":{}})); denial=json.loads(sys.stdin.readline()); print(json.dumps({"id":f["id"],"result":{"denied":denial["error"]["code"]}}))'
        with tempfile.TemporaryDirectory() as cwd, self.child(source, cwd) as stream:
            self.assertEqual(stream.rpc("initialize", {}), {"denied": -32601})

    def test_malformed_eof_and_oversized_frames_are_not_metadata(self):
        for source, expected in (('print("not json")', "native_malformed"), ('pass', "native_eof"), ('print("a" * (1024*1024+1))', "native_frame_too_large")):
            with tempfile.TemporaryDirectory() as cwd, self.child(source, cwd) as stream:
                with self.assertRaisesRegex(NativeError, expected):
                    stream.receive()

    def test_expired_deadline_is_deterministic_without_a_timing_assertion(self):
        with tempfile.TemporaryDirectory() as cwd, self.child('import sys; sys.stdin.read()', cwd) as stream:
            stream.deadline = 0
            with self.assertRaisesRegex(NativeError, "native_timeout"):
                stream.send({"method": "initialize"})


    # The product's own kill seam is ``kill_process_group`` - it exists on
    # both platforms (the unix branch calls ``os.killpg``; the Windows branch
    # kills the tree with ``taskkill /T``).  Patching ``os.killpg`` itself
    # is POSIX-only: on Windows the attribute does not exist, so the patch
    # raised AttributeError before the test ran (2026-10-22, W-46).
class CommandBounds(unittest.TestCase):
    def test_permission_error_before_exit_status_settles_reaps_only_owned_group(self):
        proc = mock.Mock(pid=12345)
        proc.poll.side_effect = [None, None, 0, 0]
        proc.wait.return_value = 0
        with mock.patch("herdr_team.native_transport.kill_process_group", side_effect=PermissionError) as kill:
            _stop_owned(proc)
        self.assertEqual([call.args[0] for call in kill.call_args_list], [12345, 12345])
        proc.wait.assert_called()

    def test_permission_error_with_still_live_parent_is_not_suppressed(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired("owned-child", 2)
        with mock.patch("herdr_team.native_transport.kill_process_group", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                _stop_owned(proc)

    def test_metadata_command_bounds_output_before_buffering_everything(self):
        with tempfile.TemporaryDirectory() as cwd:
            # The CHILD writes the platform's newline: its stdout is a pipe in text
            # mode, so on Windows it is "metadata\\r\\n".  The product reads
            # the bytes faithfully - it must not translate, because the
            # output may be binary - so the expectation is the platform's
            # newline (2026-10-22, W-47).
            self.assertEqual(
                metadata_command([sys.executable, "-c", 'print("metadata")'], cwd,
                                dict(os.environ), time.monotonic() + 10),
                "metadata" + os.linesep)
            with self.assertRaisesRegex(NativeError, "native_frame_too_large"):
                metadata_command([sys.executable, "-c", 'import sys; sys.stdout.write("x" * (9 * 1024 * 1024))'], cwd, dict(os.environ), time.monotonic() + 10)

    def test_exited_parent_does_not_skip_private_group_cleanup(self):
        proc = mock.Mock(pid=12345, returncode=0)
        with mock.patch("herdr_team.native_transport.kill_process_group") as kill:
            _stop_owned(proc)
        self.assertEqual([call.args[0] for call in kill.call_args_list], [12345, 12345])

    def test_darwin_exited_group_permission_error_is_reaped_not_retried_elsewhere(self):
        proc = mock.Mock(pid=12345, returncode=0)
        proc.poll.return_value = 0
        with mock.patch("herdr_team.native_transport.kill_process_group", side_effect=PermissionError):
            _stop_owned(proc)
        proc.wait.assert_called()
        proc.poll.return_value = None
        with mock.patch("herdr_team.native_transport.kill_process_group", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                _stop_owned(proc)

    def test_descendant_holding_pipe_after_parent_exit_is_closed(self):
        # The child script FORKS, which is POSIX-only: Windows has no fork,
        # so the descendant that holds the pipe open cannot be created there.
        # The check is for the PLATFORM, so the unix run still exercises it
        # (2026-10-22, W-47).
        requires_posix("the child script forks, and Windows has no fork")
        source = 'import os,sys,signal,json; pid=os.fork();\nif pid: os._exit(0)\nsignal.signal(signal.SIGTERM, signal.SIG_IGN); print(json.dumps({"child":os.getpid()}), flush=True); sys.stdin.read()'
        with tempfile.TemporaryDirectory() as cwd:
            stream = JsonLines([sys.executable, "-u", "-c", source], cwd, dict(os.environ))
            try:
                self.assertIn("child", stream.receive())
                stream.proc.wait(timeout=5)
            finally:
                stream.close()
            self.assertFalse(stream.reader.is_alive())
            stream.close()  # repeated close must not signal a recycled process-group id


class PrivateHttp(unittest.TestCase):
    def test_metadata_helper_is_loopback_authenticated_and_owns_cleanup(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        http = mock.Mock()
        response = io.BytesIO(b'{"all":[],"connected":[]}')
        http.open.side_effect = [urllib.error.HTTPError("http://local", 401, "auth required", {}, None), response]
        with mock.patch("herdr_team.native_transport.subprocess.Popen", return_value=proc) as launch, mock.patch("herdr_team.native_transport.urllib.request.build_opener", return_value=http), mock.patch("herdr_team.native_transport.kill_process_group") as stop:
            with OpenCodeServer("fixture", "/tmp", {}) as server:
                self.assertEqual(server.request("/provider"), {"all": [], "connected": []})
                with self.assertRaisesRegex(NativeError, "metadata_method_refused"):
                    server.request("/session")
            argv = launch.call_args.args[0]
            self.assertEqual(argv[1:4], ["serve", "--hostname", "127.0.0.1"])
            self.assertTrue(launch.call_args.kwargs["env"]["OPENCODE_SERVER_PASSWORD"])
            self.assertEqual(stop.call_args.args[0], proc.pid)
            proc.wait.assert_called()

    def test_accepted_password_flag_without_authentication_is_refused(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        http = mock.Mock()
        http.open.return_value = io.BytesIO(b"unauthenticated response")
        with mock.patch("herdr_team.native_transport.subprocess.Popen", return_value=proc), mock.patch("herdr_team.native_transport.urllib.request.build_opener", return_value=http), mock.patch("herdr_team.native_transport.kill_process_group"):
            with OpenCodeServer("fixture", "/tmp", {}) as server:
                with self.assertRaisesRegex(NativeError, "native_authentication_unsupported"):
                    server.request("/provider")


if __name__ == "__main__":
    unittest.main()
