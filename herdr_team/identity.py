"""Author resolution for every CLI invocation (plan 4.3).

Tiers, in order:

1. ``HERDR_PLUGIN_EVENT`` or ``HERDR_PLUGIN_ID`` without
   ``HERDR_PLUGIN_ENTRYPOINT_ID`` -> ``system`` (never a member, never
   human).
2. ``HERDR_PLUGIN_ENTRYPOINT_ID=console`` in the terminal recorded in
   ``console.json`` (confirmed by ``pane.get`` on ``HERDR_PANE_ID`` and
   ``pane.process_info`` on that pane) -> ``human``; ``verified`` only when
   the pane reports ``focused: true`` at Enter, else ``console-unfocused``.
   Any other entrypoint (compose, picker) -> ``human`` via ``popup``,
   unverified.
3. ``HERDR_PANE_ID`` -> ``pane.get`` (alias-tolerant after a move) -> the
   current ``pane_id``, ``terminal_id``, ``agent``. Agent present:
   ``agent.get`` for the live name, roster member by ``terminal_id`` (else
   ``HERDR_TEAM_MEMBER`` unverified, else ``not_a_member``); ``--as human``
   is refused with ``author_mismatch`` and audited. Agent absent: candidate
   human, verified when the pane's foreground process group is ours and
   the group leader's parent is the pane shell; otherwise
   ``cli-unverified`` with a reason, never refused.
4. Nothing set -> ``--team`` or ``--session`` required (``team_required``);
   ``human`` via ``outside``, unverified.

``--as <member>`` is never accepted (the CLI has no such flag).
``from_label`` (``--name`` or ``HERDR_TEAM_HUMAN``) is honoured only for
verified human posts. ``--relayed-for human`` is accepted only from a
member and recorded as ``relayed_for``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import sanitize, store

from herdr_team import paths as _paths
from herdr_team import platform_compat as _compat
from herdr_team.errors import EXIT_REFUSED, EXIT_UNREACHABLE, HerdrTeamError
from herdr_team.paths import Layout, ensure_team_dirs, env_workspace, team_name_from_arg
from herdr_team import operator as _operator
from herdr_team import roster as _roster
from herdr_team import superuser_designations as _superuser_designations

VIA_CLI = "cli"
VIA_CLI_UNVERIFIED = "cli-unverified"
VIA_CONSOLE = "console"
VIA_CONSOLE_UNFOCUSED = "console-unfocused"
VIA_POPUP = "popup"
VIA_OUTSIDE = "outside"
VIA_HOOK = "hook"
VIA_SYSTEM = "system"
#: Written only by the notifier, on the operator's behalf: a scheduled post the
#: operator set up (``schedule``), or an answer the operator sent from a paired
#: phone (``remote``). They count as the human's word *on the board*, so the
#: post is delivered and the ask it answers is closed. No CLI author ever
#: carries them, so they never pass an authority gate.
VIA_SCHEDULE = "schedule"
VIA_REMOTE = "remote"
RECORD_ONLY_HUMAN_VIAS = (VIA_SCHEDULE, VIA_REMOTE)

AUTHOR_HUMAN = "human"
AUTHOR_SYSTEM = "system"

TIER_SYSTEM = 1
TIER_CONSOLE = 2
TIER_PANE = 3
TIER_OUTSIDE = 4

CONSOLE_ENTRYPOINT = "console"
POPUP_ENTRYPOINTS = ("compose", "picker", "who")

LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}\Z")
ANCESTRY_MAX_DEPTH = 16
PS_TIMEOUT_S = 2.0


def _in_test_environment() -> bool:
    """Detect if we're running in a pytest test environment.

    The test runner (pytest) runs in the same process and imports pytest modules,
    so we can detect it by checking sys.modules. This is used to skip the
    harness exe ancestry check in _tier_outside during test runs, because
    the test runner itself is often launched from an agent terminal and would
    otherwise have harness executables in its ancestry.
    """
    return "pytest" in sys.modules or "_pytest" in sys.modules


@dataclass
class Author:
    """Who is running this command, and how sure we are."""

    name: str  # member name, "human", or "system"
    kind: Optional[str]  # agent kind label, "human", or None
    via: str  # one of the VIA_* constants
    verified: bool
    pane_id: Optional[str] = None
    terminal_id: Optional[str] = None
    workspace_id: Optional[str] = None
    tab_id: Optional[str] = None
    from_label: Optional[str] = None
    team: Optional[str] = None
    reason: Optional[str] = None  # why unverified, printed to stderr
    origin: Dict[str, Any] = field(default_factory=dict)  # the record's ``origin`` object
    tier: int = 0
    relayed_for: Optional[str] = None
    generation: Optional[int] = None
    #: A live delegation from the operator (``herdr_team.operator``). Only ever
    #: set on a verified roster member, and only by an explicit grant.
    operator: bool = False
    #: A superuser agent (``herdr-synapse`` superuser kind). Can exercise
    #: operator authority from a harness pane without an operator grant.
    superuser: bool = False

    @property
    def is_human(self) -> bool:
        return self.name == AUTHOR_HUMAN

    @property
    def trusted_human(self) -> bool:
        """The operator, from an origin the board's own reader rule trusts.

        ``is_human`` is a name, and a name was what every authority gate
        tested -- so a process that reached an entrypoint tier with one
        variable set was the operator to charter, rules, manager and grant
        writes alike. This applies ``human_origin_ok`` to the author the way
        readers apply it to a record: console, popup, outside, or a shell
        whose ancestry was confirmed. A shell that could not be confirmed is
        still *named* human and still posts; it just carries no authority.
        """
        return self.is_human and human_origin_ok({"via": self.via, "verified": self.verified})

    @property
    def is_system(self) -> bool:
        return self.name == AUTHOR_SYSTEM

    @property
    def is_member(self) -> bool:
        return self.name not in (AUTHOR_HUMAN, AUTHOR_SYSTEM)

    def to_json(self) -> Dict[str, Any]:
        obj: Dict[str, Any] = {
            "name": self.name, "kind": self.kind, "via": self.via, "verified": self.verified,
            "pane_id": self.pane_id, "terminal_id": self.terminal_id, "team": self.team, "tier": self.tier,
        }
        if self.from_label:
            obj["from_label"] = self.from_label
        if self.relayed_for:
            obj["relayed_for"] = self.relayed_for
        if self.operator:
            obj["operator"] = True
        if self.reason:
            obj["reason"] = self.reason
        return obj


# --------------------------------------------------------------------------
# helpers


def sanitize_label(label: Optional[str]) -> Optional[str]:
    """``[A-Za-z0-9._-]{1,32}``; ``label_invalid`` otherwise (``sanitize.sanitize_label``)."""
    if label is None or label == "":
        return None
    return sanitize.sanitize_label(label)


def _base_origin(via: str, verified: bool, layout: Layout) -> Dict[str, Any]:
    return {
        "via": via, "verified": bool(verified), "pid": os.getpid(), "ppid": os.getppid(),
        "workspace_id": None, "tab_id": None, "socket": os.fspath(layout.socket),
    }


def ps_table() -> Optional[Dict[int, int]]:
    """``{pid: ppid}`` for every process visible to ``ps``; None when ``ps`` is unavailable."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PS_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    table: Dict[int, int] = {}
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            table[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return table or None


def ancestry(pid: int, table: Optional[Dict[int, int]], max_depth: int = ANCESTRY_MAX_DEPTH) -> List[int]:
    """``[pid, ppid, ...]`` up to ``max_depth`` levels using ``table`` (own ppid when the table is missing)."""
    chain = [pid]
    if table is None:
        if pid == os.getpid():
            chain.append(os.getppid())
        return chain
    current = pid
    for _ in range(max_depth):
        parent = table.get(current)
        if parent is None or parent <= 1 or parent == current:
            if parent == 1:
                chain.append(1)
            break
        chain.append(parent)
        current = parent
    return chain


def _process_info(api: Any, pane_id: str) -> Optional[Dict[str, Any]]:
    try:
        result = api.request("pane.process_info", {"pane_id": pane_id})
    except HerdrTeamError:
        return None
    info = result.get("process_info") if isinstance(result, dict) else None
    return info if isinstance(info, dict) else None


def verify_shell_ancestry(api: Any, pane_id: str, own_pgrp: int, table: Optional[Dict[int, int]] = None) -> Tuple[bool, Optional[str]]:
    """``pane process-info``: foreground pgid equals ours and the leader's parent is ``shell_pid``.

    Returns ``(verified, reason)``; the reason explains an unverified result.
    """
    info = _process_info(api, pane_id)
    if info is None:
        return False, "pane process-info unavailable for {}".format(pane_id)
    fg_pgid = info.get("foreground_process_group_id")
    shell_pid = info.get("shell_pid")
    if fg_pgid is None:
        return False, "pane {} reports no foreground process group".format(pane_id)
    if int(fg_pgid) != int(own_pgrp):
        return False, "foreground process group {} of pane {} is not ours ({})".format(fg_pgid, pane_id, own_pgrp)
    if shell_pid is None:
        return False, "pane {} reports no shell pid".format(pane_id)
    leader = int(fg_pgid)
    if leader == os.getpid():
        leader_parent: Optional[int] = os.getppid()
    else:
        resolved = table if table is not None else ps_table()
        leader_parent = resolved.get(leader) if resolved else None
    if leader_parent is None:
        return False, "cannot determine the parent of process group leader {}".format(leader)
    if int(leader_parent) != int(shell_pid):
        return False, "process group leader {} is not a child of the pane shell {}".format(leader, shell_pid)
    return True, None


def confirm_pane_ancestry(api: Any, pane_id: str, own_pid: Optional[int] = None, table: Optional[Dict[int, int]] = None) -> Tuple[Optional[bool], Optional[str]]:
    """Is this process a descendant of the pane's foreground job or shell?

    ``(True, None)`` when an ancestor is a foreground process or the shell,
    ``(False, reason)`` when process info is definitive and none is,
    ``(None, reason)`` when it cannot be determined.
    """
    info = _process_info(api, pane_id)
    if info is None:
        return None, "pane process-info unavailable for {}".format(pane_id)
    pids = set()
    for proc in info.get("foreground_processes") or []:
        if isinstance(proc, dict) and isinstance(proc.get("pid"), int):
            pids.add(proc["pid"])
    if isinstance(info.get("shell_pid"), int):
        pids.add(info["shell_pid"])
    if isinstance(info.get("foreground_process_group_id"), int):
        pids.add(info["foreground_process_group_id"])
    # init is every process's ancestor, so a pane reporting pid 1 (or 0) would
    # otherwise claim every caller in the session as its own descendant.
    pids = {p for p in pids if p > 1}
    if not pids:
        return None, "pane {} reports no usable processes".format(pane_id)
    resolved = table if table is not None else ps_table()
    if resolved is None:
        return None, "ps unavailable"
    chain = ancestry(own_pid if own_pid is not None else os.getpid(), resolved)
    if any(p in pids for p in chain):
        return True, None
    return False, "this process is not a descendant of pane {}".format(pane_id)


# --------------------------------------------------------------------------
# audit


#: Agent panes examined before giving up on the "am I inside one?" question.
MAX_ANCESTRY_PANES = 24


def session_agent_panes(api: Any, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The agent panes this process's environment says it belongs to.

    Upstream's answer to "which pane is this process in?" is the process
    tree: an agent's shell command is a descendant of that agent's own
    pane, and the tree cannot be unset, so it is what decides. On Windows
    there is no ``ps``, so ``ps_table()`` is None and that answer is
    unavailable.

    The Windows answer is the HARNESS SESSION ID. Herdr reports each agent
    pane's session id (``agent_session.value``), and the harness puts the
    same id in the environment of every command it runs, so a value in the
    environment that matches a pane's session id is that pane's session.
    Unlike the environment's pane variable, the session id is the
    harness's own fact and an agent cannot unset it for the whole process
    tree it spawns.

    The process tree is NOT the Windows answer, and that is measured, not
    assumed: the opencode server is shared across panes on Windows, so a
    command launched from one pane has an ancestry that runs through
    another pane's agent process. Feeding a Windows process table to
    :func:`hosting_agent_pane` attributes a member's write to a DIFFERENT
    member, which is worse than the hole this closes.

    Returns the pane ids, empty when this process belongs to no agent
    pane - which is the answer for a human in a plain shell pane and for
    the operator outside Herdr, so those paths are unchanged.
    """
    source = os.environ if env is None else env
    session_ids = _compat.session_ids_in_env(source)
    if not session_ids:
        return []
    try:
        result = api.request("pane.list", {})
    except HerdrTeamError:
        return []
    panes = result.get("panes") if isinstance(result, dict) else None
    if not isinstance(panes, list):
        return []
    return _compat.panes_for_session_ids(panes, session_ids)


def _pane_rows(api: Any) -> List[Dict[str, Any]]:
    """The pane list, or ``[]`` when the server cannot be asked.

    Fetched ONCE per resolution and shared by the checks that need it: the
    upstream identity contract is one socket call per tier ("one
    process-tree check, nothing else"), so the Windows steps must not add
    one of their own.
    """
    try:
        result = api.request("pane.list", {})
    except HerdrTeamError:
        return []
    panes = result.get("panes") if isinstance(result, dict) else None
    return panes if isinstance(panes, list) else []


def hosting_agent_pane(api: Any, own_pid: Optional[int] = None, table: Optional[Dict[int, int]] = None,
                       env: Optional[Dict[str, str]] = None,
                       panes: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """The agent pane this process is running inside, when there is one.

    An agent's shell command is a descendant of that agent's own pane whatever
    the environment says. ``HERDR_PANE_ID`` is set by Herdr, but a member can
    unset it, and every human-only gate tests only that the author is *named*
    ``human`` -- so dropping one variable used to turn any member into the
    operator. The process tree cannot be unset the same way, so it is what
    decides here.

    On Windows the tree is available too, and it is the SAME kind of fact:
    ``platform_compat.process_table`` builds it with ``CreateToolhelp32Snapshot``
    (a Win32 call - no subprocess, no shell, and it works on a machine with
    no ``ps``).  So the Windows branch asks the tree, exactly as the unix
    branch does.

    Why the tree and not the harness SESSION ID (which was tried first, and
    is still used for attribution): the session id is an ENVIRONMENT
    VARIABLE, so an agent's own command can unset it - and the verifier
    proved on the live server that doing so opens the human-only gates
    again.  The tree is a kernel fact and cannot be unset for the whole
    tree the caller spawns, which is the whole point of the guard.

    The session id keeps one job, and it is NOT this one: ATTRIBUTION -
    which pane, when the tree cannot place the process at all.  See
    :func:`_session_attributed_pane`.  A wrong answer there is a cosmetic
    problem; a wrong answer about WHO is the security one, and that is the
    tree's job.

    Returns the ``pane.list`` row on positive evidence, else None. Absence of
    evidence is not evidence of absence: an unreachable server or a missing
    table leaves the caller exactly where it was, because refusing on a
    failed lookup would lock the operator out of their own CLI.
    """
    if panes is None:
        panes = _pane_rows(api)
    if _compat.IS_WINDOWS:
        # The tree decides, on Windows exactly as on unix.  It is asked here
        # because it is the fact that cannot be forged, and it is the SAME
        # question upstream asks: "is this process a descendant of an agent
        # pane's foreground job?".  The session id is NOT consulted here -
        # it is an environment variable, so it is an attribution fact, never
        # a guard (2026-10-22, W-44).
        win_table, _names = _compat.process_table()
        if not win_table:
            return None
        pid = own_pid if own_pid is not None else os.getpid()
        checked = 0
        for pane in panes:
            if not isinstance(pane, dict) or not pane.get("agent") or not isinstance(pane.get("pane_id"), str):
                continue
            checked += 1
            if checked > MAX_ANCESTRY_PANES:
                break
            inside, _why = confirm_pane_ancestry(api, pane["pane_id"], own_pid=pid, table=win_table)
            if inside is True:
                return pane
        return None
    resolved = table if table is not None else ps_table()
    if resolved is None:
        return None
    pid = own_pid if own_pid is not None else os.getpid()
    checked = 0
    for pane in panes:
        if not isinstance(pane, dict) or not pane.get("agent") or not isinstance(pane.get("pane_id"), str):
            continue
        checked += 1
        if checked > MAX_ANCESTRY_PANES:
            break
        inside, _why = confirm_pane_ancestry(api, pane["pane_id"], own_pid=pid, table=resolved)
        if inside is True:
            return pane
    return None


def audit(layout: Layout, team: Optional[str], event: str, author: Author, details: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Append one line to ``<team>/audit.jsonl``; returns the path (None without a team)."""
    if not team:
        return None
    try:
        team_paths = layout.team(team)
    except HerdrTeamError:
        return None
    if not team_paths.root.is_dir():
        # The team was dissolved: its directory is gone, and an audit line must not bring
        # it back. The team lock refuses to resurrect a dissolved team for the same reason
        # (RS-10/RS-13), and an audit entry is not worth a team that no longer exists.
        return None
    ensure_team_dirs(team_paths)
    entry = {
        "ts": _roster.now_iso(),
        "event": event,
        "author": author.name,
        "via": author.via,
        "pane_id": author.pane_id,
        "terminal_id": author.terminal_id,
        "verified": bool(author.verified),
        "details": dict(details or {}),
    }
    store.append_line(team_paths.audit_jsonl, json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), fsync=False)
    return os.fspath(team_paths.audit_jsonl)


def read_audit(layout: Layout, team: str, last: Optional[int] = None) -> List[Dict[str, Any]]:
    raw = store.read_bytes(layout.team(team).audit_jsonl, default=b"") or b""
    entries: List[Dict[str, Any]] = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    if last is not None and last >= 0:
        entries = entries[-last:] if last else []
    return entries


def refuse_as_human(layout: Layout, author: Author, details: Optional[Dict[str, Any]] = None) -> HerdrTeamError:
    """``author_mismatch`` for ``--as human`` from a member, system, or hook path; audited."""
    info: Dict[str, Any] = {"requested": "human", "resolved": author.name, "via": author.via}
    if details:
        info.update(details)
    audit(layout, author.team, "author_mismatch", author, info)
    return HerdrTeamError(
        "author_mismatch",
        "this pane is {!r}, not the human; use --relayed-for human to relay the operator".format(author.name),
        EXIT_REFUSED,
        {"author": author.name, "via": author.via, "pane_id": author.pane_id},
    )


# --------------------------------------------------------------------------
# roster lookups


def _teams_to_search(layout: Layout, team: Optional[str]) -> List[str]:
    if team:
        return [team]
    return layout.session.list_teams()


def find_member(layout: Layout, terminal_id: Optional[str], team: Optional[str] = None) -> Optional[Tuple[str, _roster.Member]]:
    """``(team, member)`` for a live terminal id: pane record first, then every roster."""
    if not terminal_id:
        return None
    record = _roster.read_pane_record(layout.session, terminal_id)
    candidates: List[str] = []
    if isinstance(record, dict) and isinstance(record.get("team"), str) and (team is None or record["team"] == team):
        candidates.append(record["team"])
    for name in _teams_to_search(layout, team):
        if name not in candidates:
            candidates.append(name)
    for name in candidates:
        try:
            doc = _roster.load_team(layout.team(name))
        except HerdrTeamError:
            continue
        member = doc.find_by_terminal(terminal_id)
        if member is not None:
            return name, member
    return None


def _pane_get(api: Any, pane_id: str) -> Optional[Dict[str, Any]]:
    result = api.request("pane.get", {"pane_id": pane_id})
    pane = result.get("pane") if isinstance(result, dict) else None
    return pane if isinstance(pane, dict) else None


def _agent_name(api: Any, pane_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        result = api.request("agent.get", {"target": pane_id})
    except HerdrTeamError:
        return None, None
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return None, None
    return agent.get("name"), agent


def _env_team(env: Dict[str, str], team: Optional[str]) -> Optional[str]:
    if team:
        return team
    try:
        return team_name_from_arg(None, env)
    except HerdrTeamError:
        return None


#: What ``--name``/``HERDR_TEAM_HUMAN`` means, printed wherever a command drops one.
IGNORED_LABEL_NOTE = "--name names the operator behind a verified human pane; it is ignored here"


def _apply_label(author: Author, label: Optional[str], env: Dict[str, str]) -> Author:
    raw = label if label is not None else env.get("HERDR_TEAM_HUMAN")
    if not raw:
        return author
    clean = sanitize_label(raw)
    if author.is_human and author.verified:
        author.from_label = clean
    else:
        author.origin["ignored_label"] = clean
        note = "--name ignored on an unverified or non-human path"
        author.reason = "{}; {}".format(author.reason, note) if author.reason else note
    return author


def ignored_label_note(author: Author) -> Optional[str]:
    """One sentence for a ``--name`` this author does not get, or None.

    The label is applied in exactly one place (``_apply_label``) and recorded
    in exactly one place (``origin["ignored_label"]``), so the *report* belongs
    here too. Before this, a command either warned in its own words or -- on
    ``board``, which is what the live run hit -- dropped the label without a
    word, so a member could keep passing ``--name`` and keep reading its own
    inbox while believing it was reading somebody else's.
    """
    dropped = author.origin.get("ignored_label")
    if not dropped:
        return None
    return "{} ({!r}); reading as {}".format(IGNORED_LABEL_NOTE, dropped, author.name)


def _apply_operator_grant(author: Author, layout: Layout) -> Author:
    """Mark a member the operator has delegated its authority to.

    Only a *verified* roster member can hold one: an identity taken from
    ``HERDR_TEAM_MEMBER`` is a claim, not evidence, and a grant must not be
    reachable by claiming to be the member that holds it.
    """
    if not author.is_member or not author.verified or not author.team:
        return author
    grant = _operator.active(layout.session, author.team, author.name)
    if grant is None:
        return author
    author.operator = True
    author.origin["operator"] = {"granted_at": grant.get("granted_at"), "expires_at": grant.get("expires_at")}
    return author


def _apply_relay(author: Author, relayed_for: Optional[str]) -> Author:
    if relayed_for is None:
        return author
    if relayed_for != AUTHOR_HUMAN:
        raise HerdrTeamError("usage", "--relayed-for accepts only 'human'", 2, {"relayed_for": relayed_for})
    if not author.is_member:
        raise HerdrTeamError("author_mismatch", "--relayed-for human is for members relaying the operator; you are {!r}".format(author.name), EXIT_REFUSED, {"author": author.name})
    author.relayed_for = AUTHOR_HUMAN
    author.origin["relayed_for"] = AUTHOR_HUMAN
    return author


# --------------------------------------------------------------------------
# the tiers


def _tier_system(env: Dict[str, str], layout: Layout, team: Optional[str], as_human: bool) -> Author:
    via = VIA_HOOK if env.get("HERDR_PLUGIN_EVENT") else VIA_SYSTEM
    author = Author(AUTHOR_SYSTEM, None, via, True, team=_env_team(env, team), tier=TIER_SYSTEM)
    author.origin = _base_origin(via, True, layout)
    author.origin["event"] = env.get("HERDR_PLUGIN_EVENT")
    author.origin["plugin_id"] = env.get("HERDR_PLUGIN_ID")
    author.origin["pane_id"] = env.get("HERDR_PANE_ID")
    if as_human:
        raise refuse_as_human(layout, author, {"tier": TIER_SYSTEM})
    return author


def _tier_console(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str], require_server: bool) -> Author:
    from herdr_team import cmd_board as _console_registry

    inside = hosting_agent_pane(api, env=env)
    if inside is not None:
        # The entrypoint variable is Herdr's, but an agent can set it too, and
        # naming the real console's pane would then pass the registry check
        # below and land as ``console-unfocused``, which readers trust. The
        # process tree settles it: a real console descends from the Herdr
        # server, a forgery descends from the agent's own pane.
        return _rerouted_agent_author(env, layout, api, inside, _env_team(env, team), False, "console entrypoint set inside an agent pane")
    console = store.read_json(layout.session.console_json, default=None)
    console = console if isinstance(console, dict) else {}
    default_team = console.get("default_team") if isinstance(console.get("default_team"), str) else None
    resolved_team = team or default_team or _env_team(env, None)
    pane_env = env.get("HERDR_PANE_ID")
    author = Author(AUTHOR_HUMAN, "human", VIA_CONSOLE_UNFOCUSED, False, pane_id=pane_env, team=resolved_team, tier=TIER_CONSOLE)
    author.origin = _base_origin(VIA_CONSOLE_UNFOCUSED, False, layout)
    author.origin["entrypoint"] = CONSOLE_ENTRYPOINT
    if not pane_env:
        author.reason = "console process has no HERDR_PANE_ID"
        return author
    pane: Optional[Dict[str, Any]] = None
    try:
        pane = _pane_get(api, pane_env)
    except HerdrTeamError as err:
        if err.exit_code == EXIT_UNREACHABLE and not require_server:
            author.reason = "Herdr unreachable: {}".format(err.message)
            return author
        raise
    if pane is None:
        author.reason = "pane {} not found".format(pane_env)
        return author
    author.pane_id = pane.get("pane_id") or pane_env
    author.terminal_id = pane.get("terminal_id")
    author.workspace_id = pane.get("workspace_id")
    author.tab_id = pane.get("tab_id")
    author.origin.update({"pane_id": author.pane_id, "terminal_id": author.terminal_id, "workspace_id": author.workspace_id, "tab_id": author.tab_id})
    # Membership in the console registry, not equality with a single recorded
    # terminal: several consoles may be open, one per team. The entry must also
    # still be running, so a stale record cannot make a dead pane's terminal
    # verify. Ancestry and focus below remain the real gates.
    entry = _console_registry.console_entry(layout.session, author.terminal_id)
    live = bool(entry) and bool(entry.get("open")) and _console_registry._pid_is_alive(entry.get("pid"))
    if not live:
        known = sorted(_console_registry.live_console_entries(layout.session))
        author.via = VIA_CLI_UNVERIFIED
        author.origin["via"] = VIA_CLI_UNVERIFIED
        author.reason = "this pane is {!r}; live consoles are {}".format(author.terminal_id, known or "none")
        return author
    # A console is pinned to the team it was opened for.
    if isinstance(entry.get("team"), str) and entry["team"] and not team:
        author.team = entry["team"]
    confirmed, reason = confirm_pane_ancestry(api, author.pane_id)
    # ``say`` needs a positive answer: ``None`` (no process info, no ``ps``) stays an unfocused-grade author there.
    author.origin["ancestry"] = "confirmed" if confirmed else ("unconfirmed" if confirmed is False else "unavailable")
    if confirmed is False:
        # Definitively not a descendant of the console pane: whatever this
        # process is, it is not the console, so it does not get the console's
        # trusted origin. ``None`` (no process info) keeps the benefit of the
        # doubt, as everywhere else.
        author.via = VIA_CLI_UNVERIFIED
        author.origin["via"] = VIA_CLI_UNVERIFIED
        author.reason = reason
        return author
    if pane.get("focused") is True:
        author.via = VIA_CONSOLE
        author.verified = True
        author.origin["via"] = VIA_CONSOLE
        author.origin["verified"] = True
    else:
        author.reason = "console pane not focused at Enter"
    return author


def _space_team(layout: Layout, workspace_id: Optional[str]) -> Optional[str]:
    """The team of ``workspace_id``, ranked above the session's ``default_team``.

    A human has no roster row to be looked up in, so their team used to come
    from one session-wide default -- which is wrong in every space but one as
    soon as a session holds two teams. Deferred import: ``cmd_board`` imports
    this module.
    """
    from herdr_team.cmd_board import workspace_team

    try:
        return workspace_team(layout.session, workspace_id)
    except (HerdrTeamError, OSError):
        return None


def _tier_popup(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str]) -> Author:
    panes = _pane_rows(api)
    inside = hosting_agent_pane(api, env=env, panes=panes)
    if inside is not None:
        # A popup has no pane identity to check, which is exactly why this
        # tier used to make zero socket calls -- and why setting the variable
        # from an agent's shell made that agent the operator to every gate.
        # Same rule as ``_tier_outside``: the process tree cannot be unset.
        return _rerouted_agent_author(env, layout, api, inside, _env_team(env, team), False, "popup entrypoint set inside an agent pane")
    # The tree could not place this process and the session id did not name a
    # pane either.  A HARNESS MARKER still says an agent harness started it,
    # and a human's shell has none - so it is an agent we cannot place, and
    # it is refused the way ``_tier_outside`` refuses one.  The same
    # symmetry the verifier asked for: the entrypoint tier must refuse what
    # the outside tier refuses, or the entrypoint is a way around it
    # (2026-10-22, W-44).
    session_pane = _session_attributed_pane(panes, env)
    if session_pane is not None:
        return _rerouted_agent_author(
            env, layout, api, session_pane, _env_team(env, team), False,
            "popup entrypoint set inside an agent pane")
    marker = _compat.harness_marker_in_env(env)
    if marker:
        raise HerdrTeamError(
            "not_a_member",
            "an agent harness started this process ({}), and it cannot be attributed "
            "to a pane".format(marker),
            EXIT_UNREACHABLE,
            {"harness_marker": marker, "hint": {"teams": layout.session.list_teams()}},
        )
    console = store.read_json(layout.session.console_json, default=None)
    default_team = console.get("default_team") if isinstance(console, dict) and isinstance(console.get("default_team"), str) else None
    space_team = _space_team(layout, env_workspace(env))
    author = Author(AUTHOR_HUMAN, "human", VIA_POPUP, False, team=team or space_team or default_team or _env_team(env, None), tier=TIER_CONSOLE)
    author.origin = _base_origin(VIA_POPUP, False, layout)
    author.origin["entrypoint"] = env.get("HERDR_PLUGIN_ENTRYPOINT_ID")
    context = env.get("HERDR_PLUGIN_CONTEXT_JSON")
    if context:
        try:
            parsed = json.loads(context)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            author.origin["focused_pane_id"] = parsed.get("focused_pane_id")
            author.origin["nonce"] = parsed.get("nonce") or parsed.get("launch_nonce")
    author.reason = "popup launches carry no pane identity"
    return author


def _tier_pane(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str], as_human: bool, require_server: bool, team_explicit: bool = True) -> Author:
    pane_env = str(env.get("HERDR_PANE_ID"))
    env_team = _env_team(env, team)
    # W-16: a DESIGNATED superuser pane/session carries operator authority
    # even when it is not a roster member of any team. The designation is
    # checked FIRST (before the roster lookup below), because the whole point
    # of the feature is to let a pane that is not (yet) a member exercise
    # operator authority. The check is server-side registry + pane.list, so a
    # caller cannot forge it by setting env vars alone (W-44).
    designated = None
    if not _in_test_environment():
        designated = _designated_superuser_author(env, layout, api, env_team)
    try:
        pane = _pane_get(api, pane_env)
    except HerdrTeamError as err:
        if err.exit_code == EXIT_UNREACHABLE and not require_server:
            return _offline_pane_author(env, layout, env_team, pane_env, err.message)
        if err.code in ("pane_not_found", "invalid_pane_id", "pane_target_not_found"):
            raise HerdrTeamError("not_a_member", "pane {} not found on this session ({})".format(pane_env, err.message), EXIT_UNREACHABLE, {"pane_id": pane_env, "herdr_code": err.code})
        raise
    if pane is None:
        raise HerdrTeamError("not_a_member", "pane {} not found on this session".format(pane_env), EXIT_UNREACHABLE, {"pane_id": pane_env})
    current_pane = str(pane.get("pane_id") or pane_env)
    terminal_id = pane.get("terminal_id")
    origin = _base_origin(VIA_CLI, False, layout)
    origin.update({
        "pane_id": current_pane, "terminal_id": terminal_id,
        "workspace_id": pane.get("workspace_id"), "tab_id": pane.get("tab_id"),
    })
    if current_pane != pane_env:
        origin["claimed_pane_id"] = pane_env
    mismatches = {}
    for key, field_name in (("HERDR_WORKSPACE_ID", "workspace_id"), ("HERDR_TAB_ID", "tab_id")):
        value = env.get(key)
        if value and pane.get(field_name) and value != pane.get(field_name):
            mismatches[key] = {"env": value, "pane": pane.get(field_name)}
    if mismatches:
        origin["env_mismatch"] = mismatches

    if pane.get("agent"):
        return _agent_pane_author(env, layout, api, pane, current_pane, origin, env_team, as_human, team_explicit,
                                  designated=designated)
    return _shell_pane_author(env, layout, api, pane, current_pane, origin, env_team)


def _offline_pane_author(env: Dict[str, str], layout: Layout, env_team: Optional[str], pane_env: str, message: str) -> Author:
    member_env = env.get("HERDR_TEAM_MEMBER")
    if member_env and env_team:
        try:
            _roster.validate_member_name(member_env)
        except HerdrTeamError:
            member_env = None
    if member_env and env_team:
        author = Author(member_env, env.get("HERDR_TEAM_KIND"), VIA_CLI, False, pane_id=pane_env, team=env_team, tier=TIER_PANE)
        author.origin = _base_origin(VIA_CLI, False, layout)
        author.origin["pane_id"] = pane_env
        author.origin["source"] = "env:HERDR_TEAM_MEMBER"
        author.reason = "Herdr unreachable ({}); identity from HERDR_TEAM_MEMBER".format(message)
        return author
    author = Author(AUTHOR_HUMAN, "human", VIA_CLI_UNVERIFIED, False, pane_id=pane_env, team=env_team, tier=TIER_PANE)
    author.origin = _base_origin(VIA_CLI_UNVERIFIED, False, layout)
    author.origin["pane_id"] = pane_env
    author.reason = "Herdr unreachable ({}); pane identity unverified".format(message)
    return author


def _agent_pane_author(env: Dict[str, str], layout: Layout, api: Any, pane: Dict[str, Any], current_pane: str, origin: Dict[str, Any], env_team: Optional[str], as_human: bool, team_explicit: bool = True, designated: Optional[Author] = None) -> Author:
    kind = pane.get("agent")
    terminal_id = pane.get("terminal_id")
    live_name, _agent = _agent_name(api, current_pane)
    origin["agent"] = kind
    origin["live_name"] = live_name
    found = find_member(layout, terminal_id, env_team)
    if found is None and env_team is not None and not team_explicit:
        # The hint was only the session's ``default_team``: plan 4.3 ranks the roster match by
        # terminal id above it, and a terminal sits in at most one roster, so widening the search
        # cannot mis-attribute. Without this a member of any non-default team was ``not_a_member``
        # from its own pane (M5 rig finding).
        found = find_member(layout, terminal_id, None)
    if found is not None:
        team_name, member = found
        author = Author(member.name, member.kind, VIA_CLI, True, pane_id=current_pane, terminal_id=terminal_id,
                        workspace_id=pane.get("workspace_id"), tab_id=pane.get("tab_id"), team=team_name, tier=TIER_PANE,
                        generation=member.generation)
        origin["verified"] = True
        origin["generation"] = member.generation
        author.origin = origin
        if live_name and live_name != member.name and not member.retired_name_active(live_name):
            author.reason = "live agent name {!r} differs from roster name {!r}".format(live_name, member.name)
        if kind and kind != member.kind:
            author.reason = "live agent kind {!r} differs from roster kind {!r}".format(kind, member.kind)
        confirmed, reason = confirm_pane_ancestry(api, current_pane)
        origin["ancestry"] = "confirmed" if confirmed else ("unconfirmed" if confirmed is False else "unavailable")
        if confirmed is False:
            audit(layout, team_name, "pane_mismatch", author, {"reason": reason, "claimed_pane_id": origin.get("claimed_pane_id")})
            raise HerdrTeamError("pane_mismatch", "HERDR_PANE_ID names pane {} but this process does not run in it".format(current_pane), EXIT_REFUSED, {"pane_id": current_pane, "reason": reason})
        if confirmed is None:
            # The process tree could not answer (Windows: no ``ps``). The
            # harness SESSION ID answers the same question there, and it is
            # the harness's own fact: the session's own pane is the one whose
            # ``agent_session.value`` this process carries. A claimed pane
            # that is NOT in that set is a forged pane, and upstream refuses
            # it with ``pane_mismatch`` on the same ground.
            #
            # The set is asked once here, on Windows only (the tree cannot
            # answer there), so the unix path keeps its own call count.
            session_panes = session_agent_panes(api, env)
            if session_panes and current_pane not in session_panes:
                detail = "this process's session id names pane(s) {}, not {}".format(
                    ", ".join(session_panes), current_pane)
                audit(layout, team_name, "pane_mismatch", author, {"reason": detail, "claimed_pane_id": origin.get("claimed_pane_id")})
                raise HerdrTeamError("pane_mismatch", "HERDR_PANE_ID names pane {} but this process does not run in it".format(current_pane), EXIT_REFUSED, {"pane_id": current_pane, "reason": detail})
        if designated is not None:
            # W-16: this pane is the designated superuser AND a roster member.
            # Keep the member identity (so board posts still carry the member
            # name) and ADD the operator authority the designation grants.
            author.superuser = True
            author.origin = dict(origin)
            author.origin["designation_source"] = "designation"
            author.origin["pane_id"] = current_pane
            author.reason = "designated superuser pane/session"
        if as_human:
            raise refuse_as_human(layout, author, {"tier": TIER_PANE, "kind": kind})
        return author
    if designated is not None:
        # W-16: the designated superuser pane is NOT a roster member. The
        # designation is exactly the authority that lets it act anyway.
        return designated
    member_env = env.get("HERDR_TEAM_MEMBER")
    if member_env and env_team:
        try:
            _roster.validate_member_name(member_env)
        except HerdrTeamError:
            member_env = None
    if member_env and env_team:
        author = Author(member_env, kind, VIA_CLI, False, pane_id=current_pane, terminal_id=terminal_id,
                        workspace_id=pane.get("workspace_id"), tab_id=pane.get("tab_id"), team=env_team, tier=TIER_PANE)
        origin["source"] = "env:HERDR_TEAM_MEMBER"
        author.origin = origin
        author.reason = "terminal {} is not in the roster of {!r}; identity from HERDR_TEAM_MEMBER".format(terminal_id, env_team)
        if as_human:
            raise refuse_as_human(layout, author, {"tier": TIER_PANE, "kind": kind})
        return author
    if as_human:
        probe = Author(live_name or "agent", kind, VIA_CLI, False, pane_id=current_pane, terminal_id=terminal_id, team=env_team, tier=TIER_PANE)
        probe.origin = origin
        raise refuse_as_human(layout, probe, {"tier": TIER_PANE, "kind": kind, "member": False})
    raise HerdrTeamError(
        "not_a_member",
        "pane {} hosts a {} agent that is not in any team".format(current_pane, kind),
        EXIT_UNREACHABLE,
        {"pane_id": current_pane, "terminal_id": terminal_id, "agent": kind, "name": live_name, "hint": {"teams": layout.session.list_teams()}},
    )


def own_pgrp() -> int:
    """This process's process group id.

    Windows has no POSIX process groups: the shell-ancestry check compares
    Herdr's ``foreground_process_group_id`` with this value, and Herdr on
    Windows does not report one, so the check simply stays unverified
    there (the author is marked ``cli-unverified``).
    """
    getpgrp = getattr(os, "getpgrp", None)
    if getpgrp is None:
        return os.getpid()
    return int(getpgrp())


def _shell_pane_author(env: Dict[str, str], layout: Layout, api: Any, pane: Dict[str, Any], current_pane: str, origin: Dict[str, Any], env_team: Optional[str]) -> Author:
    terminal_id = pane.get("terminal_id")
    team_name = env_team
    if team_name is None:
        team_name = _space_team(layout, pane.get("workspace_id"))
    if team_name is None:
        console = store.read_json(layout.session.console_json, default=None)
        if isinstance(console, dict) and isinstance(console.get("default_team"), str):
            team_name = console["default_team"]
    verified, reason = verify_shell_ancestry(api, current_pane, own_pgrp())
    if not verified:
        # A verified shell is the foreground job of this very pane, so it cannot
        # also be a descendant of some agent's pane and the scan is skipped. An
        # unverified one might be an agent's subprocess pointed at a shell pane.
        inside = hosting_agent_pane(api, env=env)
        if inside is not None and inside.get("pane_id") != current_pane:
            return _rerouted_agent_author(env, layout, api, inside, env_team, False, "HERDR_PANE_ID names another pane")
    via = VIA_CLI if verified else VIA_CLI_UNVERIFIED
    origin["via"] = via
    origin["verified"] = bool(verified)
    origin["pgrp"] = own_pgrp()
    author = Author(AUTHOR_HUMAN, "human", via, bool(verified), pane_id=current_pane, terminal_id=terminal_id,
                    workspace_id=pane.get("workspace_id"), tab_id=pane.get("tab_id"), team=team_name, tier=TIER_PANE)
    author.origin = origin
    author.reason = reason
    # A post typed in a dead member's pane is the human's, with that pane as origin.
    former = None
    if terminal_id:
        for name in _teams_to_search(layout, env_team):
            try:
                doc = _roster.load_team(layout.team(name))
            except HerdrTeamError:
                continue
            member = doc.find_by_terminal(terminal_id, include_left=True)
            if member is not None:
                former = (name, member.name)
                break
    if former is not None:
        origin["former_member"] = {"team": former[0], "name": former[1]}
        if author.team is None:
            author.team = former[0]
    return author


def _superuser_exes_for_team(team: Optional[str]) -> Tuple[str, ...]:
    """Get the superuser executable names for a team from its config."""
    # This will be called from _tier_outside where we have the team
    # For now, return the default SUPERUSER_EXE_VARS
    # In the future, this could read from team config: work.superuser_exes
    return _compat.SUPERUSER_EXE_VARS


def _is_superuser_process(win_table: Dict[int, int], win_names: Dict[int, str], superuser_exes: Tuple[str, ...]) -> bool:
    """Check if any process in the ancestry is a superuser agent executable."""
    for pid in ancestry(os.getpid(), win_table):
        exe = str(win_names.get(pid, "")).lower()
        if exe in superuser_exes:
            return True
    return False


def _identity_process_table() -> Tuple[Optional[Dict[int, int]], Dict[int, str]]:
    """The process table the identity checks read.

    Windows has no ``ps``, so the Win32 table (``CreateToolhelp32Snapshot``,
    via ``platform_compat.process_table``) is the tree here; elsewhere it is
    ``ps``.

    ``confirm_pane_ancestry`` asks for ``ps`` BY ITSELF, so every call that
    does not pass a table is "unavailable" on Windows. That is why the
    pane-membership check falls back to the session-id attribution there, and
    why the designation check asks the tree DIRECTLY: the tree is the only
    fact a caller cannot forge.
    """
    if _compat.IS_WINDOWS:
        table, names = _compat.process_table()
        return (table or None), dict(names or {})
    return ps_table(), {}


def _designated_pane_confirmed(
    api: Any,
    designations: List[Any],
    panes: List[Dict[str, Any]],
    resolved_team: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """WHICH pane this process actually runs in, among the designated ones.

    This is FACTOR 3 of the W-16 security model, and the implementation was
    missing it: the registry says WHICH pane carries superuser authority, and
    the PROCESS TREE says WHICH pane this process runs in. The grant requires
    the two to agree.

    Why the tree and not the env vars: ``HERDR_PANE_ID`` and the harness
    session id are ENVIRONMENT VARIABLES, so a caller sets them to another
    pane's values and claims that pane's authority. Measured on the live
    server (2026-10-30): a member pane in another workspace, with
    ``HERDR_PANE_ID`` pointed at the designated pane, was granted superuser
    authority over two teams it was not a member of. The tree is a kernel
    fact and cannot be forged.

    The designated pane must also be a real pane on this session (FACTOR 2),
    and the W-16 base marker - a trusted superuser executable in the
    caller's own ancestry - is accepted as the same unforgeable fact seen
    from the other side.

    Returns ``(pane_id, reason)``: the confirmed pane, or ``(None, reason)``
    saying why the designation could not be honoured.
    """
    session_pane_ids = {p.get("pane_id") for p in panes if isinstance(p, dict)}
    candidates: List[str] = []
    for entry in designations:
        pane_id = getattr(entry, "pane_id", None)
        if isinstance(pane_id, str) and pane_id and pane_id not in candidates:
            candidates.append(pane_id)
    if not candidates:
        # A session-only designation: the panes whose harness session id is
        # the designated one.
        session_ids = [getattr(e, "session_id", None) for e in designations]
        wanted = [s for s in session_ids if isinstance(s, str) and s]
        for pane_id in _compat.panes_for_session_ids(panes, wanted):
            if pane_id not in candidates:
                candidates.append(pane_id)
    candidates = [p for p in candidates if p in session_pane_ids]
    if not candidates:
        return None, "the designated pane is not on this session"
    table, names = _identity_process_table()
    reason = ""
    for pane_id in candidates:
        confirmed, why = confirm_pane_ancestry(api, pane_id, table=table)
        if confirmed is True:
            return pane_id, None
        reason = why or "the process tree did not place this process in pane {}".format(pane_id)
    if table and names and _is_superuser_process(table, names, _superuser_exes_for_team(resolved_team)):
        # The W-16 base marker: the caller's own ancestry carries a trusted
        # superuser executable, which is the same unforgeable fact.
        return candidates[0], None
    return None, reason


def _designated_superuser_author(
    env: Dict[str, str],
    layout: Layout,
    api: Any,
    resolved_team: Optional[str],
) -> Optional[Author]:
    """The W-16 designated superuser pane/session, if the caller IS one.

    The designation lives in the server-side registry
    (``<session>/superuser_designations.json``) and is matched against a pane
    that ``pane.list`` confirms exists on this session. Caller-controlled env
    vars are lookup keys only, never authority tokens (W-44): they SELECT a
    candidate designation, and the process tree then decides whether this
    process really is that pane.

    Returns an ``Author`` with ``superuser=True`` for a designated caller, or
    ``None`` for everyone else. A broken registry falls through to the normal
    tiers rather than granting or refusing on its own. A designation that
    MATCHES but cannot be confirmed is refused loudly (``pane_mismatch``):
    that is a caller claiming another pane's authority, and it must not
    quietly become "not a member" either.
    """
    try:
        if not (layout.session and layout.session.root.exists()):
            return None
        pane_env = env.get("HERDR_PANE_ID")
        session_env = env.get("OPENCODE_SESSION_ID") or env.get("HERDR_SESSION")
        if not pane_env and not session_env:
            return None
        registry = _superuser_designations.DesignationRegistry(layout.session)
        designations = []
        if pane_env:
            designations = registry.find_active(pane_id=pane_env)
        if not designations and session_env:
            designations = registry.find_active(session_id=session_env)
        if not designations:
            return None
    except Exception:
        # Fail closed: if the registry is broken/unreadable, fall through to
        # the normal tiers (a broken registry grants nothing).
        return None
    panes = _pane_rows(api)
    confirmed_pane, why = _designated_pane_confirmed(api, designations, panes, resolved_team)
    if confirmed_pane is None:
        raise HerdrTeamError(
            "pane_mismatch",
            "the designated superuser pane could not be confirmed: {}".format(why),
            EXIT_REFUSED,
            {"claimed_pane_id": pane_env or "", "reason": why},
        )
    author = Author(AUTHOR_HUMAN, "superuser", VIA_OUTSIDE, False, team=resolved_team,
                    tier=TIER_OUTSIDE, superuser=True)
    author.origin = _base_origin(VIA_OUTSIDE, False, layout)
    origin_extra = dict(author.origin or {})
    origin_extra["designation_source"] = "designation"
    origin_extra["pane_id"] = confirmed_pane
    if session_env:
        origin_extra["session_id"] = session_env
    author.origin = origin_extra
    author.reason = "designated superuser pane/session"
    audit(layout, resolved_team, "superuser_action", author,
          {"action": "designated_superuser_pane_session", "pane_id": confirmed_pane,
           "session_id": session_env or "", "origin": "designation"})
    return author


def _tier_outside(env: Dict[str, str], layout: Layout, api: Any, team: Optional[str], as_human: bool = False) -> Author:
    resolved_team = _env_team(env, team)
    if resolved_team is None and not layout.env_socket.explicit and layout.state_root.team_dir is None:
        raise HerdrTeamError(
            "team_required",
            "not inside a Herdr pane: pass --team <name|path> or --session <name>",
            EXIT_UNREACHABLE,
            {"teams": layout.session.list_teams()},
        )
    panes = _pane_rows(api)
    inside = hosting_agent_pane(api, env=env, panes=panes)
    if inside is not None:
        # No pane in the environment, but the process tree says otherwise: this is
        # an agent's own subprocess with the variable stripped. It gets its own
        # identity back, not the operator's.
        return _rerouted_agent_author(env, layout, api, inside, resolved_team, as_human, "no HERDR_PANE_ID")
    # Windows: the process tree could not place this process (no ``ps``), and
    # no session id names a pane, so this process cannot be attributed. A
    # HARNESS MARKER still says an agent harness started it - a human's shell
    # The tree could not place this process.  The harness session id may still
    # name the agent it belongs to - ATTRIBUTION, not a guard (the id is a
    # variable the caller controls, 2026-10-22, W-44) - and a process that
    # ran outside its agent's own pane still gets its member's identity back
    # rather than the operator's.
    session_pane = _session_attributed_pane(panes, env)
    if session_pane is not None:
        return _rerouted_agent_author(
            env, layout, api, session_pane, resolved_team, as_human,
            "no HERDR_PANE_ID; the harness session id names this agent's pane")
    # The tree could not place this process (no ``ps``), and
    # no session id names a pane, so this process cannot be attributed. A
    # HARNESS MARKER still says an agent harness started it - a human's shell
    # has none - so it is an agent we cannot place, and it is refused the way
    # upstream refuses an agent that is not on any roster.
    #
    # The marker is a weaker fact than a session id (a caller controls its own
    # environment), so this narrows the gap, it does not close the class.
    # The operator's own path is untouched: a human in a plain shell pane has
    # no marker and keeps the outside tier.
    if _compat.IS_WINDOWS:
        marker = _compat.harness_marker_in_env(env)
        if marker:
            raise HerdrTeamError(
                "not_a_member",
                "an agent harness started this process ({}), and it cannot be attributed "
                "to a pane or a member".format(marker),
                EXIT_UNREACHABLE,
                {"harness_marker": marker, "hint": {"teams": layout.session.list_teams()}},
            )
        # Even without an env marker, the tree can still say "a harness
        # started this".  The sweep is ANCESTRY-WIDE, on purpose, and two
        # live measurements on 2026-10-23 (W-1) are why:
        #
        #   * case G - a member's in-pane command with every variable
        #     stripped - was refused, but a sweeper that only fires when the
        #     harness service is the caller's GRANDPARENT (``[me, shell,
        #     opencode.exe]``) was DEFEATED by one extra wrapper process: the
        #     caller chooses its own wrapper depth, so depth is not a fact
        #     that can decide anything.
        #   * the operator's detached-from-pane elevation has the SAME shape
        #     as the attacker's ("[ ... pane shell, detached, opencode.exe]"),
        #     so no property of the process itself separates them on this
        #     deployment.  Refusing harness-descended processes therefore
        #     refuses BOTH, and that is the accepted posture: on Windows the
        #     operator's authority lives in a terminal that is not a harness
        #     descendant (the app console tier, or the human's own Windows
        #     Terminal) - the detached-from-pane elevation is retired, and
        #     the port doc records the migration (section 0n).
        # SKIP in test environment: the test runner itself is launched from an
        # agent pane by the harness, so the LIVE ancestry would refuse every
        # in-process outside-tier resolution; tests exercise the sweep through
        # a synthetic table instead.
        if not _in_test_environment():
            # W-16 Designated Superuser Pane/Session feature: check server-side
            # designation before the ancestry sweep. The designation must be
            # verified by server-side registry + pane.list confirmation,
            # never trusted from caller-controlled env vars alone (W-44).
            designated = _designated_superuser_author(env, layout, api, resolved_team)
            if designated is not None:
                return designated
            win_table, win_names = _compat.process_table()
            if win_table:
                # Check for superuser agents first - they are ALLOWED through the sweep
                superuser_exes = _superuser_exes_for_team(resolved_team)
                is_superuser = _is_superuser_process(win_table, win_names, superuser_exes)
                if is_superuser:
                    # This is a superuser agent - allow it through the sweep
                    author = Author(AUTHOR_HUMAN, "superuser", VIA_OUTSIDE, False, team=resolved_team, tier=TIER_OUTSIDE, superuser=True)
                    author.origin = _base_origin(VIA_OUTSIDE, False, layout)
                    author.reason = "superuser agent"
                    return author
                else:
                    for pid in ancestry(os.getpid(), win_table):
                        exe = str(win_names.get(pid, "")).lower()
                        if exe in _compat.HARNESS_EXE_VARS:
                            raise HerdrTeamError(
                                "not_a_member",
                                "an agent harness started this process ({}), and it cannot be attributed "
                                "to a pane or a member".format(exe),
                                EXIT_UNREACHABLE,
                                {"harness_exe": exe, "hint": {"teams": layout.session.list_teams()}},
                            )
    author = Author(AUTHOR_HUMAN, "human", VIA_OUTSIDE, False, team=resolved_team, tier=TIER_OUTSIDE)
    author.origin = _base_origin(VIA_OUTSIDE, False, layout)
    author.reason = "outside Herdr"
    return author


def _session_attributed_pane(panes: List[Dict[str, Any]], env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """The pane the harness session id names, when the tree could not place us.

    This is ATTRIBUTION, never a guard: the session id is an environment
    variable, so an agent's own command controls it (measured 2026-10-22,
    W-44).  It is asked only where the process tree - the kernel fact that
    cannot be unset - has already answered "this is not inside an agent
    pane", and it is used to say WHICH agent, so a process that ran outside
    its agent's own pane still gets its member's identity back rather than
    the operator's.

    The pane list is the caller's - it was already fetched for the tree
    check, so this costs no socket call (the identity contract is one per
    tier).

    Windows only in effect: on unix the process tree places the process, and
    this is never reached.
    """
    if not _compat.IS_WINDOWS:
        return None
    pane_ids = _compat.panes_for_session_ids(panes, _compat.session_ids_in_env(env))
    for pane in panes:
        if isinstance(pane, dict) and pane.get("pane_id") in pane_ids:
            return pane
    return None


def _rerouted_agent_author(env: Dict[str, str], layout: Layout, api: Any, pane: Dict[str, Any], env_team: Optional[str], as_human: bool, why: str) -> Author:
    """Resolve as the agent whose pane this process actually runs in."""
    pane_id = str(pane.get("pane_id"))
    origin = _base_origin(VIA_CLI, False, layout)
    origin.update({
        "pane_id": pane_id, "terminal_id": pane.get("terminal_id"),
        "workspace_id": pane.get("workspace_id"), "tab_id": pane.get("tab_id"),
        "rerouted": why,
    })
    return _agent_pane_author(env, layout, api, pane, pane_id, origin, env_team, as_human, team_explicit=env_team is not None)


def resolve_author(
    env: Dict[str, str],
    layout: Layout,
    api: Any,
    team: Optional[str] = None,
    as_human: bool = False,
    relayed_for: Optional[str] = None,
    label: Optional[str] = None,
    require_server: bool = True,
    team_explicit: bool = True,
) -> Author:
    """Apply the tiers; raise ``not_a_member`` (3), ``author_mismatch`` (1), ``team_required`` (3).

    ``team_explicit`` says whether ``team`` came from ``--team`` / ``HERDR_TEAM`` /
    ``HERDR_TEAM_DIR`` (True) or only from the session's ``default_team`` (False);
    a member pane in another team is still resolved in the latter case.
    """
    env = dict(env)
    entrypoint = env.get("HERDR_PLUGIN_ENTRYPOINT_ID")
    if (env.get("HERDR_PLUGIN_EVENT") or env.get("HERDR_PLUGIN_ID")) and not entrypoint:
        author = _tier_system(env, layout, team, as_human)
    elif entrypoint == CONSOLE_ENTRYPOINT or entrypoint == _paths.entrypoint_id(CONSOLE_ENTRYPOINT):
        author = _tier_console(env, layout, api, team, require_server)
    elif entrypoint:
        author = _tier_popup(env, layout, api, team)
    elif env.get("HERDR_PANE_ID"):
        author = _tier_pane(env, layout, api, team, as_human, require_server, team_explicit)
    else:
        author = _tier_outside(env, layout, api, team, as_human)
    if as_human and not author.is_human:
        raise refuse_as_human(layout, author, {"tier": author.tier})
    author = _apply_relay(author, relayed_for)
    author = _apply_label(author, label, env)
    author = _apply_operator_grant(author, layout)
    if author.origin:
        author.origin["via"] = author.via
        author.origin["verified"] = bool(author.verified)
    return author


def authority_refusal(action: str, author: Author) -> str:
    """Why an authority write was refused, with the way out for a real operator.

    A member is told the action is human only. A human whose shell could not be
    verified is told how to be trusted: the console, a focused pane, or the
    ``outside`` tier -- each still process-tree-checked, so none of them is a
    way in for an agent.
    """
    if author.is_human:
        why = " ({})".format(author.reason) if author.reason else ""
        return ("{} needs the operator from a trusted origin, and this shell could not be verified as yours{}. "
                "Use the team console, a focused Herdr pane, or run it outside Herdr: "
                "env -u HERDR_PANE_ID herdr-synapse --team <team> ...").format(action, why)
    return "{} is human only; this pane is {!r} ({})".format(action, author.name, author.via)


def is_operator_delegate(author: Any, team: Optional[str] = None) -> bool:
    """Does this author hold the operator's authority for ``team``?

    The operator delegates its authority to a seat with ``operator grant``. A
    delegate is the operator's hands inside that team: it writes the charter,
    the rules and the instructions, it rules on work items, it gives verdicts
    the operator used to give, and it appoints the manager. Every use is
    audited, so the trail says a delegate acted and which one.

    A grant is team-scoped: a delegate's authority in one team says nothing
    about another, and nothing about the session (naming a superuser pane
    stays the operator's). ``team`` is the roster's name, not the session's.
    """
    if not getattr(author, "operator", False):
        return False
    if not getattr(author, "is_member", False) or not getattr(author, "verified", False):
        return False
    if team is not None and getattr(author, "team", None) != team:
        return False
    return True


def human_origin_ok(record_origin: Dict[str, Any]) -> bool:
    """The one rule for trusting ``from: human``: console, popup, outside, or a verified shell.

    Readers apply it to a record's ``origin`` (``daemon._counts_for_nudges``,
    ``asks.answered_by``); the authority gates apply it to the author through
    ``Author.trusted_human``. Two callers, one answer.
    """
    via = record_origin.get("via")
    if via in (VIA_CONSOLE, VIA_CONSOLE_UNFOCUSED, VIA_POPUP, VIA_OUTSIDE):
        return True
    return via == VIA_CLI and bool(record_origin.get("verified"))


def record_human_ok(record_origin: Dict[str, Any]) -> bool:
    """``human_origin_ok`` for a stored record, which may also carry a notifier-written origin.

    Only record readers use this (delivery, ask answers, rendering). The
    authority gates keep ``Author.trusted_human``, which never sees a
    ``schedule`` or ``remote`` origin.
    """
    if human_origin_ok(record_origin):
        return True
    return record_origin.get("via") in RECORD_ONLY_HUMAN_VIAS and bool(record_origin.get("verified"))
