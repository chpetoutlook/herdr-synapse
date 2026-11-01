"""The lead and the manager never need the operator for a team task.

The operator delegates its authority to a seat with ``operator grant``. That
delegate is the operator's hands inside its team: it writes the charter, the
rules and the instructions, it rules on work items, it gives the verdicts the
operator used to give, it appoints the manager, it watches its team, and it
staffs it. Nothing it does waits for a person who is not in the loop.

This file is the guarantee, written down. Every gate the work flow passes
through is listed here, so a gate that starts refusing the delegate again
fails a test rather than stalling a team quietly.
"""
from __future__ import annotations

import unittest

from herdr_team import charter, cmd_board, cmd_roster, cmd_watch, cmd_work, identity, staffing, store
from herdr_team import work as W
from herdr_team.errors import HerdrTeamError

from support import TempState

MEMBER = "alpha-reviewer"   # codex, w2:p1
WORKER = "alpha-worker"     # claude, w2:p2
TEAM = "alpha"


class DelegateCapabilityTests(unittest.TestCase):
    """Every operator gate the team passes through, for a delegate."""

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == MEMBER:
                m["manager"] = True
        store.write_json(self.ts.team.team_json, doc)
        self.layout = self.ts.layout
        self.doc = store.read_json(self.ts.team.team_json)
        self.delegate = self._delegate()
        self.item = self._item()

    def _delegate(self, granted=True):
        from herdr_team import operator as _operator

        if granted:
            _operator.grant(self.ts.session, TEAM, MEMBER, ttl_s=0, note="the lead and the manager")
        else:
            _operator.revoke(self.ts.session, TEAM, MEMBER)
        author = identity.Author(MEMBER, "codex", identity.VIA_CLI, True, team=TEAM)
        # the flag is set the way the real path sets it: from the live grant
        return identity._apply_operator_grant(author, self.layout)

    def _item(self):
        from herdr_team import work as _work

        items = _work.load(self.ts.team)
        return items.get("W-1")

    # -- the gates the work flow passes through ---------------------------

    def test_the_delegate_may_write_the_charter_the_rules_and_the_instructions(self):
        charter.require_human(self.layout, TEAM, self.delegate, "charter set")
        charter.require_human(self.layout, TEAM, self.delegate, "knowledge set")
        charter.require_human(self.layout, TEAM, self.delegate, "instructions set")

    def test_the_delegate_may_give_the_verdicts_the_operator_used_to_give(self):
        cmd_work._require_operator(self.layout, TEAM, self.delegate, self.item, "review in place of human")
        cmd_work._require_operator(self.layout, TEAM, self.delegate, self.item, "close")
        cmd_work._require_operator(self.layout, TEAM, self.delegate, self.item, "update --review-by")

    def test_the_delegate_may_assign_and_reroute(self):
        cmd_work._require_manage(self.layout, TEAM, self.delegate, self.item, self.doc, "assign")
        cmd_work._require_manage(self.layout, TEAM, self.delegate, self.item, self.doc, "update")
        cmd_work._require_ruling(self.layout, TEAM, self.delegate, self.item, self.doc, "rule")

    def test_the_delegate_may_appoint_the_manager_and_bind_seats(self):
        cmd_roster._human_only(self.layout, TEAM, self.delegate, "manager set")
        cmd_roster._human_only(self.layout, TEAM, self.delegate, "add member")
        cmd_roster._human_only(self.layout, TEAM, self.delegate, "remove member")
        cmd_roster._human_only(self.layout, TEAM, self.delegate, "bind member")

    def test_the_delegate_may_hand_the_authority_on_inside_its_own_team(self):
        cmd_roster._strictly_human(self.layout, TEAM, self.delegate, "operator grant", scope="team")
        cmd_roster._strictly_human(self.layout, TEAM, self.delegate, "operator revoke", scope="team")
        cmd_roster._strictly_human(self.layout, TEAM, self.delegate, "manager --operator", scope="team")

    def test_the_delegate_may_staff_and_watch_its_own_team(self):
        from herdr_team import roster as _roster

        staffing.operator(self.delegate)
        staffing.require_manager(_roster.Team.from_json(self.doc), self.delegate)
        cmd_watch._require_operator(self.layout, self.delegate, "watch", TEAM)

    def test_the_delegate_never_waits_for_the_operator(self):
        seconds = cmd_board._wait_seconds(
            argparse_wait(kind="question", wait=None),
            self.doc, ["human"], self.delegate)
        self.assertIsNone(seconds, "a delegate waits for nobody")

    # -- and the boundary that stays ------------------------------------------------

    def test_a_delegate_may_name_a_superuser_pane_for_its_own_team(self):
        """A per-team designation is the team's own business."""
        cmd_roster._strictly_human(self.layout, TEAM, self.delegate, "superuser designate", scope="team")

    def test_a_delegate_cannot_name_a_global_superuser_pane(self):
        """A global designation reaches outside the team: it stays the operator's."""
        with self.assertRaises(HerdrTeamError) as caught:
            cmd_roster._strictly_human(self.layout, TEAM, self.delegate, "superuser designate", scope="session")
        self.assertEqual(caught.exception.code, "author_mismatch")

    def test_a_delegate_without_a_grant_is_refused(self):
        author = self._delegate(granted=False)
        with self.assertRaises(HerdrTeamError) as caught:
            charter.require_human(self.layout, TEAM, author, "charter set")
        self.assertEqual(caught.exception.code, "author_mismatch")

    def test_a_plain_member_is_refused(self):
        author = identity.Author(WORKER, "claude", identity.VIA_CLI, True, team=TEAM)
        with self.assertRaises(HerdrTeamError) as caught:
            charter.require_human(self.layout, TEAM, author, "charter set")
        self.assertEqual(caught.exception.code, "author_mismatch")


class _WaitArgs:
    """The two fields ``_wait_seconds`` reads off the argparse namespace."""

    def __init__(self, kind="question", wait=None):
        self.kind = kind
        self.wait = wait
        self.timeout = None


def argparse_wait(kind="question", wait=None):
    return _WaitArgs(kind=kind, wait=wait)


if __name__ == "__main__":
    unittest.main()


class ForwardingTests(unittest.TestCase):
    """A member's request to the operator is forwarded to the seats that can act.

    The operator stays the escalator, never the queue: the manager (who
    assigns and gives the verdicts) and the lead (who rules) are addressed
    with the request, so it is answered by a seat in the team's loop.
    """

    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        doc = store.read_json(self.ts.team.team_json)
        for m in doc["members"]:
            if m["name"] == MEMBER:
                m["manager"] = True
            if m["name"] == WORKER:
                m["role"] = "lead"
        store.write_json(self.ts.team.team_json, doc)

    def test_a_request_to_the_operator_reaches_the_manager_and_the_lead(self):
        from test_cmd_board import json_out, run_cli
        from test_cmd_roster import live_api

        env = self.ts.env_with(HERDR_PANE_ID="w2:p2")  # alpha-worker, a plain member
        code, _out, err = json_out(run_cli(
            ["--json", "--team", TEAM, "post", "--to", "human", "--kind", "request", "--no-wait",
             "please assign me the next item"],
            env, live_api()))
        self.assertEqual(code, 0, err)
        records = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "request"]
        self.assertTrue(records)
        to = records[-1].get("to") or []
        self.assertIn("human", to, "the operator is still told")
        self.assertIn(MEMBER, to, "the manager is asked, because the manager assigns")
        self.assertIn(WORKER, to, "the lead is asked, because the lead rules")

    def test_a_plain_note_to_the_operator_is_not_forwarded(self):
        from test_cmd_board import json_out, run_cli
        from test_cmd_roster import live_api

        env = self.ts.env_with(HERDR_PANE_ID="w2:p2")
        code, _out, err = json_out(run_cli(
            ["--json", "--team", TEAM, "post", "--to", "human", "--kind", "note", "--no-wait",
             "fyi, the build is green"],
            env, live_api()))
        self.assertEqual(code, 0, err)
        records = [r for r in store.BoardStore(self.ts.team).read() if r.get("kind") == "note"]
        self.assertTrue(records)
        to = records[-1].get("to") or []
        self.assertNotIn(MEMBER, to, "a note wants no answer, so it is not forwarded")