"""Human in the loop (0.12.0): the operator is asked, and the agent waits.

Every test here fails against 0.11.0: nothing knew which posts were waiting on
the operator, `post` never blocked, there was no popup and no policy.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import unittest
from unittest import mock

from herdr_team import asks, cmd_asks, cmd_misc, operator, store, tui_model
from herdr_team import daemon as D
from herdr_team.errors import EXIT_NO_ANSWER, HerdrTeamError
from support import FakeApi, TempState
from test_cmd_board import json_out, run_cli, write_live_daemon


def err_of(err):
    """The JSON error object, past any `warning:` lines argparse put first."""
    if isinstance(err, dict):
        return err
    for line in str(err or "").splitlines():
        if line.lstrip().startswith("{"):
            return json.loads(line)
    return {}
from test_cmd_roster import live_api
from test_daemon import make_daemon


def _console_reply_in_process(args, env, timeout: float = 0.0):
    """W-14 rig: run the console-spawned CLI child in-process (same contract).

    The ack path answers the operator through a real child process
    (``console.run_cli`` spawns ``herdr-synapse``), and that child resolves
    its author through the W-1 ancestry sweep. On Windows the suite runner
    is itself launched from an agent pane, so a real child IS a genuine
    harness descendant and the sweep refuses it - a true fact about the
    runner's ancestry, not a defect (the sweep stays ancestry-wide; case G
    stays exit 3). The sweep rigs swap the process table only in-process;
    a spawned child cannot be rigged that way. So this rig runs the same
    argv in-process - the one place the suite's disclosed test-environment
    skip already holds - with the same env, the same
    ``(rc, stdout json, stderr json)`` contract, and ``api=None`` exactly
    like the real child. The product's child spawn is unchanged, and skip:
    on harness-descended Windows runners the console-reply child is
    exercised in-process; a clean-runner full-tree run exercises the real
    spawn.
    """
    from herdr_team import cli as _cli

    out, err = io.StringIO(), io.StringIO()
    code = _cli.main([str(a) for a in args], env=dict(env), stdout=out, stderr=err, api=None)
    parsed = json.loads(out.getvalue()) if out.getvalue().strip() else None
    first = err.getvalue().strip().split("\n", 1)[0]
    err_obj = None
    if first:
        try:
            obj = json.loads(first)
        except ValueError:
            err_obj = {"code": "cli_failed", "message": first} if code else None
        else:
            err_obj = obj if isinstance(obj, dict) and "code" in obj else None
    return code, parsed, err_obj

MEMBER = "alpha-reviewer"
PEER = "alpha-worker"


def post(ts, text="need a call", kind="question", author=MEMBER, to=("human",), reply_to=None):
    rec = {"from": author, "from_kind": "codex", "from_terminal": "term_r1",
           "origin": {"via": "cli", "verified": True}, "to": list(to), "kind": kind, "text": text}
    if reply_to is not None:
        rec["reply_to"] = reply_to
    return store.BoardStore(ts.team).append(rec)


def human_post(ts, text="go ahead", reply_to=None, kind="answer"):
    rec = {"from": "human", "from_kind": "human", "from_terminal": "term_console",
           "origin": {"via": "console", "verified": True}, "to": [MEMBER], "kind": kind, "text": text}
    if reply_to is not None:
        rec["reply_to"] = reply_to
    return store.BoardStore(ts.team).append(rec)


# --------------------------------------------------------------------------
# which posts are waiting


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def pending(self):
        return [r["seq"] for r in asks.pending(self.ts.team, dismissed=asks.dismissed(self.ts.team))]

    def test_a_post_to_the_operator_waits_until_the_operator_answers(self):
        seq = post(self.ts)
        self.assertEqual(self.pending(), [seq])
        human_post(self.ts, reply_to=seq)
        self.assertEqual(self.pending(), [])

    def test_a_peer_reply_does_not_close_it(self):
        # This is the whole point. On the team this was built for, four of the
        # ten asks were "answered" by another agent, and one of those overrode
        # a genuine pre-submission halt.
        seq = post(self.ts)
        post(self.ts, "STOP ORDER IS STALE", kind="answer", author=PEER, to=(MEMBER,), reply_to=seq)
        self.assertEqual(self.pending(), [seq], "only the operator closes an ask")

    def test_only_posts_addressed_to_the_operator_count(self):
        post(self.ts, to=("all",))
        post(self.ts, to=(PEER,))
        self.assertEqual(self.pending(), [])
        seq = post(self.ts, to=(PEER, "human"))
        self.assertEqual(self.pending(), [seq], "naming the operator among others still asks")

    def test_the_operator_and_the_system_never_ask_themselves(self):
        human_post(self.ts, "a note to myself")
        store.BoardStore(self.ts.team).append({
            "from": "system", "kind": "system", "event": "toast", "to": ["human"], "text": "toast shown",
            "origin": {"via": "system", "verified": True}})
        self.assertEqual(self.pending(), [])

    def test_retracted_and_dismissed_asks_drop_out(self):
        seq = post(self.ts)
        keep = post(self.ts, "second")
        store.BoardStore(self.ts.team).append({
            "from": MEMBER, "from_kind": "codex", "origin": {"via": "cli", "verified": True},
            "to": ["human"], "kind": "retract", "text": "retracted", "retracts": seq})
        self.assertEqual(self.pending(), [keep])
        asks.dismiss(self.ts.team, [keep])
        self.assertEqual(self.pending(), [], "dismissed is 'not now', and the popup stops reopening")
        self.assertEqual(asks.dismissed(self.ts.team), [keep])

    def test_newest_first(self):
        first = post(self.ts, "one")
        second = post(self.ts, "two")
        self.assertEqual(self.pending(), [second, first])

    def test_which_kinds_block(self):
        policy = {"block": True, "block_kinds": ["question", "blocked", "request"]}
        for kind in ("question", "blocked", "request"):
            self.assertTrue(asks.blocks(kind, policy), kind)
        for kind in ("note", "done", "answer", "handoff"):
            self.assertFalse(asks.blocks(kind, policy), kind)
        self.assertFalse(asks.blocks("question", dict(policy, block=False)), "block:false wins")


# --------------------------------------------------------------------------
# the wait


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.env = self.ts.env_with(HERDR_PANE_ID="w2:p1")

    def test_it_returns_the_operators_answer(self):
        answered = {}

        def answer():
            for _ in range(100):
                rows = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]
                if rows:
                    answered["seq"] = human_post(self.ts, "proceed", reply_to=rows[-1]["seq"])
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=answer)
        thread.start()
        code, out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "20s", "need a call"],
            self.env, live_api()))
        thread.join()
        self.assertEqual(code, 0, err)
        self.assertTrue(out["waited"])
        self.assertEqual((out["answer"]["from"], out["answer"]["text"]), ("human", "proceed"))

    def test_nobody_answers(self):
        started = time.monotonic()
        code, _out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "1s", "need a call"],
            self.env, live_api()))
        self.assertEqual(code, EXIT_NO_ANSWER, "a distinct code, not 'refused'")
        self.assertEqual(err_of(err)["code"], "wait_no_answer")
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(len([r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]), 1,
                         "the ask stays on the board")

    def test_a_peer_reply_does_not_end_the_wait(self):
        def peer():
            time.sleep(0.4)
            rows = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]
            if rows:
                post(self.ts, "I think you should proceed", kind="answer", author=PEER, to=(MEMBER,), reply_to=rows[-1]["seq"])

        thread = threading.Thread(target=peer)
        thread.start()
        code, _out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "2s", "need a call"],
            self.env, live_api()))
        thread.join()
        self.assertEqual((code, err_of(err)["code"]), (EXIT_NO_ANSWER, "wait_no_answer"))

    def test_no_wait_and_a_non_asking_kind_return_at_once(self):
        for extra in (["--kind", "question", "--no-wait"], ["--kind", "done"], ["--kind", "note"]):
            started = time.monotonic()
            code, out, err = json_out(run_cli(["--json", "post", "--to", "human"] + extra + ["x"], self.env, live_api()))
            self.assertEqual(code, 0, err)
            self.assertFalse(out["waited"], extra)
            self.assertLess(time.monotonic() - started, 5.0, extra)

    def test_acknowledging_ends_a_wait_and_dismissing_does_not(self):
        def ack():
            for _ in range(200):
                rows = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "question"]
                if rows:
                    _code, _out, err = run_cli(["--json", "--team", "alpha", "asks", "--ack", str(rows[-1]["seq"])], self.ts.env, FakeApi())
                    ack_result["err"] = err
                    return
                time.sleep(0.05)

        ack_result: dict = {}
        with mock.patch("herdr_team.console.run_cli", _console_reply_in_process):
            thread = threading.Thread(target=ack)
            thread.start()
            code, out, err = json_out(run_cli(
                ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "20s", "submit?"],
                self.env, live_api()))
            thread.join()
        if ack_result.get("err"):
            self.fail("the ack thread was refused: {}".format(ack_result["err"]))
        self.assertEqual(code, 0, err)
        self.assertTrue(out["waited"])
        self.assertIn("not a decision", out["answer"]["text"], "the agent is told this was not approval")

    def test_the_operators_own_post_never_waits(self):
        code, out, err = json_out(run_cli(
            ["--json", "--team", "alpha", "post", "--to", "human", "--kind", "question", "x"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        self.assertFalse(out["waited"])

    def test_the_board_lock_is_free_while_one_member_waits(self):
        # Holding the team lock across a wait would fail every other member's
        # post with board_locked after five seconds.
        other = {}

        def second_poster():
            time.sleep(0.3)
            other["result"] = json_out(run_cli(
                ["--json", "--team", "alpha", "post", "--to", "all", "meanwhile"], self.ts.env, FakeApi()))[0]

        thread = threading.Thread(target=second_poster)
        thread.start()
        run_cli(["--json", "post", "--to", "human", "--kind", "question", "--timeout", "2s", "waiting"], self.env, live_api())
        thread.join()
        self.assertEqual(other.get("result"), 0, "another member could post while one waited")

    def test_it_tails_rather_than_re_reading_the_whole_board(self):
        # BoardStore.read re-parses the active file, up to 4 MB, on every call.
        with mock.patch.object(store.BoardStore, "read", side_effect=AssertionError("read() in the wait loop")):
            self.assertIsNone(cmd_asks._asks and None)  # keep the import used
            self.assertIsNone(__import__("herdr_team.cmd_board", fromlist=["x"]).wait_for_answer(
                self.ts.team, 1, 0.2, poll_s=0.05))

    def test_the_wait_is_capped_under_the_harness_limit(self):
        from herdr_team import cmd_board

        self.assertLessEqual(cmd_board.MAX_WAIT_S, 540.0, "Claude Code kills a shell command at ten minutes")
        self.assertEqual(cmd_misc.ASK_DEFAULTS["timeout_s"], 480)


# --------------------------------------------------------------------------
# the policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def cli(self, *args, env=None, api=None):
        return json_out(run_cli(["--json", "--team", "alpha"] + list(args), env or self.ts.env, api or FakeApi()))

    def config(self):
        return store.read_json(self.ts.team.team_json).get("config") or {}

    def test_defaults_and_a_read_needs_no_authority(self):
        code, out, err = self.cli("ask-policy", env=self.ts.env_with(HERDR_PANE_ID="w2:p1"), api=live_api())
        self.assertEqual(code, 0, err)
        self.assertEqual((out["block"], out["timeout_s"], out["popup"]), (True, 480, True))
        self.assertEqual(out["block_kinds"], ["question", "blocked", "request"])
        self.assertFalse(out["changed"])

    def test_a_member_cannot_change_it(self):
        code, _out, err = self.cli("ask-policy", "--no-block", env=self.ts.env_with(HERDR_PANE_ID="w2:p1"), api=live_api())
        self.assertEqual((code, err_of(err)["code"]), (1, "author_mismatch"))
        self.assertEqual(self.config().get("ask"), None)

    def test_it_writes_config_ask_and_leaves_the_gate_alone(self):
        # config.gate is whitelisted: one unknown key there throws the whole
        # gate config back to defaults, which is why this lives beside it.
        code, out, err = self.cli("ask-policy", "--no-block", "--timeout", "3m", "--kinds", "blocked", "--no-popup")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["block"], out["timeout_s"], out["popup"], out["block_kinds"]), (False, 180, False, ["blocked"]))
        config = self.config()
        self.assertIn("ask", config)
        self.assertNotIn("ask", config.get("gate") or {})
        from herdr_team import gate

        parsed, overrides, error = D.gate_config_from_roster(store.read_json(self.ts.team.team_json))
        self.assertIsNone(error, "config.ask must not trip the gate whitelist")
        self.assertIsNone(overrides)
        self.assertIs(parsed, gate.DEFAULT_CONFIG)

    def test_kinds_none_and_a_bad_kind(self):
        self.assertEqual(self.cli("ask-policy", "--kinds", "none")[1]["block_kinds"], [])
        code, _out, err = self.cli("ask-policy", "--kinds", "nonsense")
        self.assertEqual((code, err_of(err)["code"]), (2, "usage"))

    def test_the_console_parses_it(self):
        self.assertEqual(tui_model.parse_input_line("/asks", "alpha").kind, "asks")
        intent = tui_model.parse_input_line("/ask-policy noblock 5m", "alpha")
        self.assertEqual((intent.kind, intent.args["block"], intent.args["timeout"]), ("ask_policy", False, "5m"))
        self.assertEqual(tui_model.parse_input_line("/ask-policy nonsense", "alpha").kind, "error")
        for command in ("/asks", "/ask-policy"):
            self.assertIn(command, tui_model.SLASH_COMMANDS)
            self.assertIn(command, tui_model.SLASH_USAGE)


# --------------------------------------------------------------------------
# the popup


class PopupModelTests(unittest.TestCase):
    def model(self, *records):
        return cmd_asks.AskModel(team="alpha", asks=list(records), width=70, height=20)

    def record(self, seq=7, sender=MEMBER, kind="blocked", text="PRE-SUBMISSION STOP: tracker says otherwise"):
        return {"seq": seq, "from": sender, "kind": kind, "text": text, "ts": store.now_iso()}

    def test_it_renders_the_queue_and_the_empty_case(self):
        lines = "\n".join(cmd_asks.ask_lines(self.model(self.record(), self.record(seq=8))))
        self.assertIn("1 of 2 waiting on you", lines)
        self.assertIn("#7 alpha-reviewer · blocked", lines)
        self.assertIn("PRE-SUBMISSION STOP", lines)
        self.assertIn("Enter replies", lines)
        self.assertIn("nothing is waiting on you", "\n".join(cmd_asks.ask_lines(self.model())))

    def test_typing_then_enter_answers_the_current_ask(self):
        model = self.model(self.record())
        for ch in "proceed":
            cmd_asks.ask_key(model, ch)
        self.assertEqual(model.input, "proceed")
        intent = cmd_asks.ask_key(model, "ENTER")
        self.assertEqual((intent.kind, intent.args["seq"], intent.args["to"], intent.args["text"]), ("reply", 7, MEMBER, "proceed"))
        argv = cmd_asks.reply_args("alpha", intent)
        self.assertIn("--reply-to", argv)
        self.assertIn("7", argv)
        self.assertIn("--no-wait", argv)
        self.assertEqual(argv[argv.index("--kind") + 1], "answer")

    def test_enter_on_an_empty_line_says_so_rather_than_posting(self):
        model = self.model(self.record())
        self.assertEqual(cmd_asks.ask_key(model, "ENTER").kind, "error")
        self.assertIn("type an answer", model.status)

    def test_tab_moves_and_clears_the_half_typed_reply(self):
        model = self.model(self.record(seq=7), self.record(seq=8))
        cmd_asks.ask_key(model, "x")
        cmd_asks.ask_key(model, "TAB")
        self.assertEqual((model.index, model.input), (1, ""))
        cmd_asks.ask_key(model, "TAB")
        self.assertEqual(model.index, 0, "it wraps")

    def test_ctrl_a_acknowledges_without_typing(self):
        model = self.model(self.record(kind="blocked"))
        intent = cmd_asks.ask_key(model, "CTRL_A")
        self.assertEqual((intent.kind, intent.args["seq"], intent.args["to"]), ("ack", 7, MEMBER))
        argv = cmd_asks.reply_args("alpha", intent)
        self.assertIn("--reply-to", argv)
        self.assertEqual(argv[argv.index("--kind") + 1], "answer")

    def test_the_console_says_that_it_answered(self):
        # An acknowledgement CLOSES the console - the ask list empties and the
        # loop ends - so without a line saying so, a successful Ctrl-A looks
        # exactly like a quit and the operator cannot tell that the answer is
        # on the board (W-23).
        model = self.model(self.record(kind="blocked"))
        intent = cmd_asks.ask_key(model, "CTRL_A")
        notice = cmd_asks.answer_notice(intent)
        self.assertIn("answered", notice)
        self.assertIn("#7", notice)
        self.assertIn("board", notice)
        # the line is DRAWN: the status is part of what the console renders
        model.status = notice
        self.assertIn("answered", "\n".join(cmd_asks.ask_lines(model)))
        # a reply says the same thing, in its own words
        model2 = self.model(self.record(kind="blocked"))
        for ch in "proceed":
            cmd_asks.ask_key(model2, ch)
        reply = cmd_asks.answer_notice(cmd_asks.ask_key(model2, "ENTER"))
        self.assertIn("answered", reply)
        self.assertIn("#7", reply)
        # and it never reads as a quit
        self.assertNotIn("quit", notice.lower())
        self.assertNotIn("closed", notice.lower())

    def test_the_hint_says_the_acknowledgement_is_announced(self):
        # the hint must not offer a key whose effect the operator cannot tell
        # from a quit, so it says that the acknowledgement is announced
        lines = "\n".join(cmd_asks.ask_lines(self.model(self.record())))
        self.assertIn("says so", lines)
        self.assertIn("acknowledges", lines)

    def test_the_console_runs_the_launcher_this_platform_can(self):
        # the console shells out to the CLI to land an answer, and on Windows
        # the POSIX sh script cannot be run at all - so a well-timed Ctrl-A
        # could never land, whatever the console said afterwards (W-23).
        from herdr_team import console

        path = console.cli_path()
        self.assertTrue(path.is_file(), "the launcher must exist: {}".format(path))
        if os.name == "nt":
            self.assertEqual(path.suffix, ".cmd", "Windows must run the .cmd launcher")
        else:
            self.assertEqual(path.name, "herdr-synapse", "unix must keep the sh script")

    def test_an_acknowledgement_never_reads_as_approval(self):
        # An agent that asked "should I submit?" must not take "seen" for a
        # yes. A peer's reply being read as approval is why this exists.
        asking = cmd_asks.ack_text("question")
        self.assertIn("not a decision", asking)
        self.assertIn("say what you would do and stop", asking)
        self.assertEqual(cmd_asks.ack_text("blocked"), asking)
        self.assertEqual(cmd_asks.ack_text("request"), asking)
        # a finished-work notice is finished by being read, and says only that
        self.assertEqual(cmd_asks.ack_text("done"), cmd_asks.ACK_SEEN)
        self.assertNotIn("decision", cmd_asks.ack_text("note"))

    def test_escape_dismisses_rather_than_answering(self):
        model = self.model(self.record())
        cmd_asks.ask_key(model, "y")
        self.assertEqual(cmd_asks.ask_key(model, "ESC").kind, "none", "the first Esc only clears the line")
        intent = cmd_asks.ask_key(model, "ESC")
        self.assertEqual((intent.kind, intent.args["seq"]), ("dismiss", 7))

    def test_q_closes_but_not_while_typing(self):
        model = self.model(self.record())
        cmd_asks.ask_key(model, "a")
        self.assertEqual(cmd_asks.ask_key(model, "q").kind, "none", "q is a character when a reply is half typed")
        self.assertEqual(model.input, "aq")
        model.input = ""
        self.assertEqual(cmd_asks.ask_key(model, "q").kind, "quit")


class AsksCommandTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def test_ack_closes_the_ask_and_leaves_the_operators_reply(self):
        seq = post(self.ts, kind="blocked", text="PRE-SUBMISSION STOP")
        with mock.patch("herdr_team.console.run_cli", _console_reply_in_process):
            code, out, err = json_out(run_cli(["--json", "--team", "alpha", "asks", "--ack", str(seq)], self.ts.env, FakeApi()))
        self.assertEqual((code, out["acknowledged"]), (0, seq), err)
        self.assertEqual(asks.pending(self.ts.team), [], "acknowledging closes it, unlike dismissing")
        reply = [r for r in store.BoardStore(self.ts.team).read() if r.get("reply_to") == seq][0]
        self.assertEqual((reply["from"], reply["kind"], reply["to"]), ("human", "answer", [MEMBER]))
        self.assertIn("not a decision", reply["text"])

    def test_ack_refuses_something_that_is_not_waiting(self):
        code, _out, err = json_out(run_cli(["--json", "--team", "alpha", "asks", "--ack", "99"], self.ts.env, FakeApi()))
        self.assertEqual((code, err_of(err)["code"]), (1, "ask_not_pending"))

    def test_it_lists_what_is_waiting_and_dismisses(self):
        seq = post(self.ts)
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "asks"], self.ts.env, FakeApi()))
        self.assertEqual(code, 0, err)
        self.assertEqual([p["seq"] for p in out["pending"]], [seq])
        self.assertEqual(out["pending"][0]["from"], MEMBER)
        code, out, err = json_out(run_cli(["--json", "--team", "alpha", "asks", "--dismiss", str(seq)], self.ts.env, FakeApi()))
        self.assertEqual((code, out["dismissed"]), (0, seq))
        self.assertEqual(json_out(run_cli(["--json", "--team", "alpha", "asks"], self.ts.env, FakeApi()))[1]["pending"], [])


# --------------------------------------------------------------------------
# the daemon raising it


class DaemonPopupTests(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()
        self.team = self.d.teams["alpha"]

    def opens(self):
        # the entrypoint id the PORT asks for, not a hardcoded unix id: the
        # Windows manifest registers asks-windows, and the daemon asks for
        # the id of the platform it runs on (W-9 shape, W-23).
        from herdr_team import paths

        wanted = paths.entrypoint_id("asks")
        return [p for m, p in self.api.calls if m == "plugin.pane.open" and p.get("entrypoint") == wanted]

    def ingest(self):
        """What one tick does before ``raise_asks``: the daemon learns of an ask
        from the record flowing past, never by re-reading the board."""
        self.d.scan_teams(force=True)
        self.d.tail_boards()

    def test_it_opens_once_for_a_waiting_ask_and_backs_off(self):
        self.assertEqual(self.opens(), [])
        post(self.ts)
        self.ingest()
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(len(self.opens()), 1)
        self.assertEqual(self.opens()[0]["env"], {"HERDR_TEAM": "alpha"})
        self.clock.advance(1)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(len(self.opens()), 1, "it backs off rather than reopening every tick")

    def test_a_busy_popup_slot_is_a_retry_not_an_error(self):
        from support import FakeError

        post(self.ts)
        self.ingest()
        self.api.set_response("plugin.pane.open", lambda params: (_ for _ in ()).throw(FakeError("ui_busy", "a popup pane is already open")))
        self.d.raise_asks(self.clock() * 1000)
        self.assertFalse([l for l in self.d.logged if "could not open" in l], "a busy slot is normal, not worth a log line")

    def test_it_never_closes_a_popup_it_did_not_open(self):
        post(self.ts)
        self.ingest()
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual([m for m, _p in self.api.calls if m == "popup.close"], [],
                         "popup.close shuts whatever the operator had open")

    def test_nothing_waiting_and_dismissed_asks_raise_nothing(self):
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [])
        seq = post(self.ts)
        asks.dismiss(self.ts.team, [seq])
        self.ingest()
        self.clock.advance(60)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [], "Esc means not now, not ask me again in a second")

    def test_an_ask_that_aged_out_of_the_popup_window_does_not_open_an_empty_popup(self):
        seq = post(self.ts)
        self.ingest()
        self.assertIn(seq, self.team.open_asks)
        for i in range(asks.LOOKBACK):
            post(self.ts, "ordinary board traffic {}".format(i), kind="note", author=PEER, to=(MEMBER,))
        self.ingest()
        self.assertIn(seq, self.team.open_asks, "the cheap in-memory tracker retains the old ask")
        self.assertEqual(asks.pending(self.ts.team), [], "the popup reads only the current bounded window")

        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [], "never steal focus for a popup that will render empty")
        self.assertEqual(self.team.open_asks, {}, "the side-effect boundary refreshes the stale tracker")
        self.clock.advance(D.ASK_POPUP_RETRY_S + 1)
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [], "a stale ask must not reopen on the retry interval")

    def test_the_policy_turns_the_popup_off(self):
        run_cli(["--json", "--team", "alpha", "ask-policy", "--no-popup"], self.ts.env, FakeApi())
        post(self.ts)
        self.ingest()
        self.d.raise_asks(self.clock() * 1000)
        self.assertEqual(self.opens(), [])


if __name__ == "__main__":
    unittest.main()


class DelegateAuthorityTests(unittest.TestCase):
    """The lead and the manager hold the operator's authority for their team.

    Nothing waits on a person who is not in the team's loop: a delegate does
    not wait for the operator's answer, and a delegate's answer closes the ask
    the same way the operator's does. A peer answer still closes nothing --
    that is the rule the whole module exists to protect.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.env = self.ts.env_with(HERDR_PANE_ID="w2:p1")

    def pending(self):
        return [r["seq"] for r in asks.pending(self.ts.team, dismissed=asks.dismissed(self.ts.team))]

    def test_a_delegate_does_not_wait_for_the_operator(self):
        operator.grant(self.ts.session, "alpha", MEMBER, ttl_s=0)
        started = time.monotonic()
        code, out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--timeout", "20s", "need a call"],
            self.env, live_api()))
        self.assertEqual(code, 0, err)
        self.assertFalse(out["waited"], "a delegate waits for nobody")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_delegate_may_not_ask_to_wait(self):
        operator.grant(self.ts.session, "alpha", MEMBER, ttl_s=0)
        code, _out, err = json_out(run_cli(
            ["--json", "post", "--to", "human", "--kind", "question", "--wait", "need a call"],
            self.env, live_api()))
        self.assertEqual((code, err_of(err)["code"]), (2, "usage"))

    def test_a_delegates_answer_closes_the_ask(self):
        operator.grant(self.ts.session, "alpha", MEMBER, ttl_s=0)
        seq = post(self.ts)
        self.assertEqual(self.pending(), [seq])
        post(self.ts, "ruled: proceed", kind="answer", author=MEMBER, to=("human",), reply_to=seq)
        self.assertNotIn(seq, self.pending(), "the delegate holds the operator's authority")

    def test_a_peer_answer_still_closes_nothing(self):
        operator.grant(self.ts.session, "alpha", MEMBER, ttl_s=0)
        seq = post(self.ts)
        post(self.ts, "I think you should proceed", kind="answer", author=PEER, to=("human",), reply_to=seq)
        self.assertIn(seq, self.pending(), "a peer answer is a note, not an answer")

    def test_a_revoked_grant_closes_nothing(self):
        operator.grant(self.ts.session, "alpha", MEMBER, ttl_s=0)
        operator.revoke(self.ts.session, "alpha", MEMBER)
        seq = post(self.ts)
        post(self.ts, "ruled: proceed", kind="answer", author=MEMBER, to=("human",), reply_to=seq)
        self.assertIn(seq, self.pending(), "no grant, no authority")

class OnlyTheAskingKindsAreAsksTests(unittest.TestCase):
    """The ask queue holds the posts that want an answer, and only those.

    The queue is the one list that means "somebody has to answer this", so
    every entry in it costs a person a decision. A completion notice, a note
    or a handoff addressed to the operator is a report: the operator reads it,
    but nothing is waiting on them. Narrowing the kinds keeps the queue
    honest -- and keeps the manager's sweep, which names the open asks,
    pointing at real work.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def pending(self):
        return [r["seq"] for r in asks.pending(self.ts.team, dismissed=asks.dismissed(self.ts.team))]

    def test_the_kinds_that_want_an_answer_are_asks(self):
        for kind in ("question", "request", "blocked"):
            seq = post(self.ts, kind=kind)
            self.assertIn(seq, self.pending(), kind)

    def test_the_kinds_that_report_are_not_asks(self):
        for kind in ("note", "done", "handoff", "answer"):
            post(self.ts, kind=kind)
            self.assertEqual(self.pending(), [], kind)

    def test_a_completion_notice_to_the_operator_is_not_an_ask(self):
        """The shape that made the queue lie: "W-26 complete" is not a question."""
        post(self.ts, "W-26 complete: L503 QUEUED", kind="done")
        self.assertEqual(self.pending(), [])

    def test_a_report_to_the_operator_does_not_block_the_posting_seat(self):
        """A report returns at once: it never becomes a wait."""
        seq = post(self.ts, "W-26 complete", kind="done")
        self.assertEqual(self.pending(), [])
        self.assertFalse(asks.is_ask(store.BoardStore(self.ts.team).read(seq - 1)))

    def test_the_narrowing_does_not_hide_a_real_ask(self):
        """The kinds that were always asks are still asks."""
        seq = post(self.ts, "need a decision", kind="request")
        self.assertEqual(self.pending(), [seq])


class DelegateIsNotAnAskTests(unittest.TestCase):
    """A delegate's own post is not a request the operator has to answer.

    The ask queue means "a person is needed here". A seat that holds the
    operator's authority for the team is the operator's hands, so its courtesy
    notes do not fill the one list that should hold the asks that really need
    a person.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)

    def _delegate_post(self, text="ruled: proceed", kind="note"):
        rec = {"from": MEMBER, "from_kind": "codex", "from_terminal": "term_r1",
               "origin": {"via": "cli", "verified": True,
                          "operator": {"granted_at": "2026-10-31T00:00:00Z", "expires_at": None}},
               "to": ["human"], "kind": kind, "text": text}
        return store.BoardStore(self.ts.team).append(rec)

    def test_a_delegates_post_is_not_an_ask(self):
        self._delegate_post()
        self.assertEqual(asks.pending(self.ts.team), [])

    def test_an_ordinary_members_post_is_still_an_ask(self):
        rec = {"from": PEER, "from_kind": "claude", "from_terminal": "term_w1",
               "origin": {"via": "cli", "verified": True},
               "to": ["human"], "kind": "question", "text": "need a call"}
        store.BoardStore(self.ts.team).append(rec)
        self.assertEqual(len(asks.pending(self.ts.team)), 1)