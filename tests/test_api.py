import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from herdr_team import api
from herdr_team.errors import HerdrTeamError
from support import FakeApi, FakeError, FakeHerdrServer, write_fake_herdr


class SocketClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeHerdrServer().start()
        self.addCleanup(self.server.stop)
        self.client = api.HerdrApi(self.server.socket_path, timeout=2.0, env={})

    def test_ping(self):
        pong = self.client.ping()
        self.assertEqual(pong["type"], "pong")
        self.assertEqual(pong["protocol"], 20)
        req = self.server.requests[0]
        self.assertEqual(req["method"], "ping")
        self.assertEqual(req["params"], {})
        self.assertTrue(req["id"].startswith("herdr-synapse:"))

    def test_request_sends_params_and_returns_result(self):
        result = self.client.request("agent.get", {"target": "w2:p1"})
        self.assertEqual(result["type"], "agent_info")
        self.assertEqual(result["agent"]["name"], "alpha-reviewer")
        self.assertEqual(self.server.requests_for("agent.get")[0]["params"], {"target": "w2:p1"})

    def test_request_raw_returns_envelope(self):
        raw = self.client.request_raw("agent.list")
        self.assertIn("id", raw)
        self.assertEqual(raw["result"]["type"], "agent_list")

    def test_server_error_maps_to_herdr_team_error(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            self.client.request("agent.get", {"target": "nope"})
        err = ctx.exception
        self.assertEqual(err.code, "agent_not_found")
        self.assertEqual(err.exit_code, 1)
        self.assertEqual(err.details["method"], "agent.get")

    def test_server_not_running_error_code_exits_3(self):
        self.server.set_error("agent.prompt", "server_not_running", "nope")
        with self.assertRaises(HerdrTeamError) as ctx:
            self.client.request("agent.prompt", {"target": "x", "text": "hi"})
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_unknown_method(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            self.client.request("nope.method")
        self.assertEqual(ctx.exception.code, "unknown_method")

    def test_timeout(self):
        self.server.set_delay("agent.list", 1.0)
        started = time.monotonic()
        with self.assertRaises(HerdrTeamError) as ctx:
            self.client.request("agent.list", timeout=0.2)
        self.assertEqual(ctx.exception.code, "herdr_timeout")
        self.assertEqual(ctx.exception.exit_code, 3)
        self.assertLess(time.monotonic() - started, 0.9)

    def test_missing_socket(self):
        client = api.HerdrApi(Path(tempfile.gettempdir()) / "ht-missing" / "herdr.sock", timeout=1.0, env={})
        with self.assertRaises(HerdrTeamError) as ctx:
            client.ping()
        self.assertEqual(ctx.exception.code, "server_not_running")
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_callable_response_and_prompt(self):
        result = self.client.request("agent.prompt", {"target": "alpha-reviewer", "text": "hi"})
        self.assertEqual(result["type"], "agent_prompted")
        self.assertEqual(result["agent"]["agent_status"], "working")

    def test_subscribe_yields_events_then_ends_at_eof(self):
        events = [
            {"event": "pane_agent_detected", "data": {"type": "pane_agent_detected", "pane_id": "w2:p1"}},
            {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "w2:p1"}},
        ]
        self.server.script_events(events, interval_s=0.01, hold_open=False)
        got = list(self.client.subscribe(["pane.agent_detected", {"type": "pane.closed"}]))
        self.assertEqual(got, events)
        sub = self.server.requests_for("events.subscribe")[0]
        self.assertEqual(sub["params"]["subscriptions"], [{"type": "pane.agent_detected"}, {"type": "pane.closed"}])

    def test_subscribe_ticks_on_idle(self):
        self.server.script_events([{"event": "pane_updated", "data": {}}], hold_open=True)
        gen = self.client.subscribe(["pane.updated"], tick_timeout=0.05)
        first = next(gen)
        for _ in range(100):  # on a slow runner the scripted event may land after the first 50 ms tick
            if first is not None:
                break
            first = next(gen)
        self.assertEqual(first["event"], "pane_updated")
        self.assertIsNone(next(gen))
        self.assertIsNone(next(gen))
        gen.close()

    def test_subscribe_bad_kind(self):
        if os.name == "nt":
            # Windows pipe-rig limitation (2026-10-21 port fix): this case never
            # connects - the usage error is raised before the socket is opened -
            # and the fake server's stop path (releasing a pending named-pipe
            # accept that no client ever took) can block on Windows. The
            # validation itself is covered by the product's own usage error, and
            # the live transport is covered by the request/response cases and by
            # the round-M8 live check against the real herdr server.
            self.skipTest("named-pipe rig teardown hangs when the client never connects")
        with self.assertRaises(HerdrTeamError) as ctx:
            list(self.client.subscribe([42]))
        self.assertEqual(ctx.exception.exit_code, 2)


class SubprocessTests(unittest.TestCase):
    def setUp(self):
        if os.name == "nt":
            # The fake herdr this class drives is a POSIX sh script, and the
            # product's subprocess path runs the binary without a shell - a
            # .cmd twin would need `cmd /c`, which the product does not use
            # (the real herdr on Windows is herdr.exe). The subprocess path is
            # covered live instead (the plugin's own `herdr` calls against the
            # real server) (2026-10-21 port fix).
            self.skipTest("the fake herdr binary is a POSIX sh script")
        self.dir = Path(tempfile.mkdtemp(prefix="ht-bin-"))
        self.fake = write_fake_herdr(self.dir)
        self.env = {"HERDR_BIN_PATH": os.fspath(self.fake), "PATH": "/usr/bin:/bin", "HERDR_SESSION": "rig"}

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_uses_herdr_bin_path_and_pins_socket(self):
        client = api.HerdrApi("/tmp/sock/herdr.sock", env=self.env)
        result = client.run(["agent", "list", "--json"])
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["argv"], ["agent list --json"])
        self.assertEqual(payload["socket"], "/tmp/sock/herdr.sock")
        self.assertEqual(payload["session"], "")
        self.assertEqual(result.args[0], os.fspath(self.fake))

    def test_run_json(self):
        client = api.HerdrApi("/tmp/sock/herdr.sock", env=self.env)
        payload = client.run_json(["pane", "get", "w1:p1"])
        self.assertEqual(payload["argv"], ["pane get w1:p1"])

    def test_run_json_error_maps_cli_error_code(self):
        client = api.HerdrApi("/tmp/sock/herdr.sock", env=self.env)
        with self.assertRaises(HerdrTeamError) as ctx:
            client.run_json(["fail"])
        self.assertEqual(ctx.exception.code, "agent_not_found")
        self.assertEqual(ctx.exception.details["returncode"], 1)

    def test_stdin_is_devnull(self):
        result = api.run_herdr(["stdin"], env=self.env)
        self.assertEqual(result.stdout.strip(), "stdin-closed")

    def test_timeout(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            api.run_herdr(["sleep", "5"], timeout=0.2, env=self.env)
        self.assertEqual(ctx.exception.code, "herdr_timeout")
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_missing_binary(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            api.run_herdr(["--version"], env={"HERDR_BIN_PATH": "/nonexistent/herdr"})
        self.assertEqual(ctx.exception.code, "herdr_not_found")

    def test_bare_herdr_only_without_bin_path(self):
        self.assertEqual(api.herdr_bin({}), "herdr")
        self.assertEqual(api.herdr_bin({"HERDR_BIN_PATH": "/x/herdr"}), "/x/herdr")

    def test_scrub_env(self):
        env = {"HERDR_PANE_ID": "w1:p1", "HERDR_TAB_ID": "t", "HERDR_WORKSPACE_ID": "w", "HOME": "/h"}
        self.assertEqual(api.scrub_env(env), {"HOME": "/h"})

    def test_parse_cli_json_non_json_output(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            api.parse_cli_json(api.RunResult(["herdr"], 0, "not json", ""))
        self.assertEqual(ctx.exception.code, "herdr_protocol")

    def test_parse_cli_json_plain_text_failure(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            api.parse_cli_json(api.RunResult(["herdr"], 1, "", "error: server not running at /x"))
        self.assertEqual(ctx.exception.code, "server_not_running")
        self.assertEqual(ctx.exception.exit_code, 3)


class LostPipeTests(unittest.TestCase):
    """A pipe that dies is a lost CONNECTION, not a dead server (W-15).

    On Windows the Herdr server closes the pipe and the client's handle goes
    invalid while the server keeps running. Reporting that as
    ``server_not_running`` told the daemon the server was gone, so it tore
    the subscription down and rebuilt it, over and over: 313 ``agent.list``
    failures and 105 subscription reconnects against a provably healthy
    server, each cycle costing a burst of failed pane opens.
    """

    def _channel(self, state):
        """A channel that fails the way the state says, and answers otherwise."""

        class _FakeChannel:
            def __init__(self, socket_path, timeout=None):
                self.socket_path = socket_path
                self.timeout = timeout
                self._id = "herdr-synapse:test"
                state["channels"] += 1
                state["this"] = self

            def settimeout(self, timeout):
                pass

            def sendall(self, data):
                self._id = json.loads(data.decode("utf-8").strip())["id"]
                failure = state.get("fail_send")
                if failure is not None and state["channels"] == 1:
                    # only the FIRST connection dies: the reconnect must work
                    raise failure

            def recv(self, size):
                failure = state.get("fail_recv")
                if failure is not None:
                    raise failure
                return json.dumps({"id": self._id, "result": {"type": "pong"}}).encode("utf-8") + b"\n"

            def close(self):
                pass

        return _FakeChannel

    def _client(self, state):
        original = api.HerdrChannel
        api.HerdrChannel = self._channel(state)
        self.addCleanup(lambda: setattr(api, "HerdrChannel", original))
        return api.HerdrApi("/nonexistent/herdr.sock", timeout=1.0, env={})

    def test_a_stale_handle_before_the_request_went_out_reconnects_and_retries(self):
        from herdr_team.platform_compat import LostPipeError

        state = {"channels": 0, "fail_send": LostPipeError(5, "write to the Herdr pipe failed: The handle is invalid", 0)}
        client = self._client(state)
        # the first connection dies with nothing written; the api reconnects
        # and the request lands - nothing reached Herdr, so this cannot
        # duplicate anything.
        result = client.request("agent.list")
        self.assertEqual(result["type"], "pong")
        self.assertEqual(state["channels"], 2)

    def test_a_stale_handle_is_never_reported_as_server_not_running(self):
        from herdr_team.platform_compat import LostPipeError

        state = {"channels": 0, "fail_send": LostPipeError(5, "write to the Herdr pipe failed: The handle is invalid", 7)}
        client = self._client(state)
        with self.assertRaises(HerdrTeamError) as ctx:
            client.request("agent.list")
        self.assertNotEqual(ctx.exception.code, "server_not_running")
        self.assertEqual(ctx.exception.code, "herdr_connection_lost")

    def test_a_stale_handle_while_waiting_is_never_reported_as_server_not_running(self):
        from herdr_team.platform_compat import LostPipeError

        state = {"channels": 0, "fail_recv": LostPipeError(5, "read from the Herdr pipe failed: The handle is invalid")}
        client = self._client(state)
        with self.assertRaises(HerdrTeamError) as ctx:
            client.request("agent.list")
        self.assertNotEqual(ctx.exception.code, "server_not_running")
        self.assertEqual(ctx.exception.code, "herdr_connection_lost")

    def test_a_server_that_is_not_listening_is_still_server_not_running(self):
        # the connect path is unchanged: a pipe nobody listens on is a server
        # that is not running.
        client = api.HerdrApi("/nonexistent/herdr.sock", timeout=0.5, env={})
        with self.assertRaises(HerdrTeamError) as ctx:
            client.request("agent.list")
        self.assertEqual(ctx.exception.code, "server_not_running")

    def test_a_busy_pipe_is_retried_and_never_reported_as_a_dead_server(self):
        """Every instance of the pipe busy is a transient race for a
        connection, not a dead server (W-15).

        The server allows a limited number of pipe instances and the daemon
        opens one per request, so the open can lose the race. The port used
        to report that as "the pipe is not listening", which the daemon read
        as "the server is gone" and answered by tearing the subscription down
        and rebuilding it, over and over.
        """
        from herdr_team.platform_compat import PipeBusyError

        state = {"channels": 0, "busy_first": 3}
        original = api.HerdrChannel

        class _BusyChannel:
            def __init__(self, socket_path, timeout=None):
                state["channels"] += 1
                if state["channels"] <= state["busy_first"]:
                    raise PipeBusyError("all pipe instances are busy")
                self._id = "herdr-synapse:test"

            def settimeout(self, timeout):
                pass

            def sendall(self, data):
                self._id = json.loads(data.decode("utf-8").strip())["id"]

            def recv(self, size):
                return json.dumps({"id": self._id, "result": {"type": "pong"}}).encode("utf-8") + b"\n"

            def close(self):
                pass

        api.HerdrChannel = _BusyChannel
        self.addCleanup(lambda: setattr(api, "HerdrChannel", original))
        client = api.HerdrApi("/nonexistent/herdr.sock", timeout=2.0, env={})
        result = client.request("agent.list")
        self.assertEqual(result["type"], "pong")
        self.assertEqual(state["channels"], 4)

    def test_a_pipe_that_stays_busy_is_reported_as_busy_not_as_a_dead_server(self):
        from herdr_team.platform_compat import PipeBusyError

        original = api.HerdrChannel

        class _AlwaysBusy:
            def __init__(self, socket_path, timeout=None):
                raise PipeBusyError("all pipe instances are busy")

        api.HerdrChannel = _AlwaysBusy
        self.addCleanup(lambda: setattr(api, "HerdrChannel", original))
        client = api.HerdrApi("/nonexistent/herdr.sock", timeout=0.3, env={})
        with self.assertRaises(HerdrTeamError) as ctx:
            client.request("agent.list")
        self.assertEqual(ctx.exception.code, "herdr_pipe_busy")
        self.assertNotEqual(ctx.exception.code, "server_not_running")


class FakeApiTests(unittest.TestCase):
    def test_surface_matches(self):
        fake = FakeApi()
        self.assertEqual(fake.ping()["type"], "pong")
        self.assertEqual(fake.request("agent.get", {"target": "w2:p2"})["agent"]["name"], "alpha-worker")
        with self.assertRaises(HerdrTeamError) as ctx:
            fake.request("agent.get", {"target": "zzz"})
        self.assertEqual(ctx.exception.code, "agent_not_found")
        fake.set_error("agent.prompt", "agent_blocked", "blocked")
        with self.assertRaises(HerdrTeamError):
            fake.request("agent.prompt", {"target": "w2:p1", "text": "x"})
        fake.events = [{"event": "pane_updated", "data": {}}]
        self.assertEqual(list(fake.subscribe(["pane.updated"])), fake.events)
        fake.set_cli_json(["agent", "list", "--json"], {"agents": []})
        self.assertEqual(fake.run_json(["agent", "list", "--json"]), {"agents": []})
        self.assertEqual(fake.runs[-1], ["agent", "list", "--json"])
        self.assertEqual(fake.calls[0][0], "ping")
        fake.unreachable = True
        with self.assertRaises(HerdrTeamError) as ctx:
            fake.ping()
        self.assertEqual(ctx.exception.exit_code, 3)

    def test_fake_error_from_callable(self):
        fake = FakeApi(responses={"x.y": lambda p: (_ for _ in ()).throw(FakeError("boom", "b"))})
        with self.assertRaises(HerdrTeamError) as ctx:
            fake.request("x.y")
        self.assertEqual(ctx.exception.code, "boom")


if __name__ == "__main__":
    unittest.main()
