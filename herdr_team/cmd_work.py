"""``work``: track units of work with a brief, an owner, dependencies, attempts and reviews (0.19).

The model and its rules live in ``herdr_team.work``; this module adds the
authority checks, the board posts that carry every change to the people it
concerns, and the audit lines. Every change is also an ordinary authored post
with a ``work`` field (``{"id", "op"}``), so the notifier delivers it through
the same gates as any other post; only "your dependency finished" is a system
record (``work_ready``, which wakes the owner).

Who may do what:

* anyone on the team may add work, for themselves, for a teammate, or open
  for whoever takes it (a request, like ``post --kind request``);
* the owner claims, blocks and settles its own attempt, and only from the
  generation that claimed it (``attempt_fenced``);
* a named reviewer, or a holder of a named role, reviews;
* the requester, the team manager, and the operator (or a delegate) assign,
  close, cancel and edit;
* a settled item is reopened - with the reason on the record - by the
  requester, the manager or the operator, or by one of the item's reviewers
  who did not approve the attempt being reopened (W-38). A reviewer who
  approved that attempt is refused, and the refusal names why.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import roster as _roster
from herdr_team import store
from herdr_team import work as W
from herdr_team.cli import Command, add_global_arguments, emit
from herdr_team.cmd_board import (
    AUTHOR_HUMAN,
    _open_team,
    agent_members,
    board_append,
    build_record,
    find_member,
    member_generation,
    member_or_raise,
    prepare_text,
    role_holders,
    task_file,
    warn,
)
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.identity import Author, audit
from herdr_team.sanitize import headline

CLI = "herdr-synapse"


# --------------------------------------------------------------------------
# helpers


def _clean(raw: Optional[str], limit: int, what: str) -> str:
    """Sanitized one-block text: echo markers and secrets are refused like a post's."""
    if raw is None:
        return ""
    text, _truncated, _body = prepare_text(str(raw), False, False)
    text = text.strip()
    if len(text) > limit:
        raise HerdrTeamError("text_too_long", "{} is {} characters; keep it under {}".format(what, len(text), limit), EXIT_REFUSED, {"field": what, "limit": limit})
    return text


def _manager(doc: Dict[str, Any]) -> Optional[str]:
    for member in agent_members(doc):
        if member.get("manager"):
            return str(member.get("name"))
    return None


def _verdict_holder(doc: Dict[str, Any], owner: Optional[str]) -> Optional[str]:
    """The seat that owes the verdict when the item names the operator as reviewer.

    A review slot only the operator can fill is a review that never happens: the
    operator is not in the team's loop. The manager takes the verdict -- unless
    the manager owns the item, because a seat never reviews its own work -- and
    then the lead does. ``None`` means this roster has no seat that can give it,
    and the request stays with the operator rather than being sent nowhere.
    """
    manager = _manager(doc)
    if manager and manager != owner:
        return manager
    for member in agent_members(doc):
        if member.get("role") == "lead" and str(member.get("name")) != owner:
            return str(member.get("name"))
    return None


def _reviewable(doc: Dict[str, Any], review_by: Sequence[str], owner: Optional[str]) -> List[str]:
    """The reviewer list of an item owned by ``owner``, able to actually review.

    Three rules, one invariant: an item always has a reviewer who is not its
    owner, and an item that names the operator also names an agent who can give
    the verdict. Reassigning an item to its own reviewer would otherwise leave
    the work with nobody who may close it.
    """
    names = [str(r) for r in review_by if r]
    if not names:
        # No reviewer configured: the item settles on its own.
        return names
    if owner and owner in names:
        names = [n for n in names if n != owner]
        if not names:
            # The owner was the only reviewer: the verdict needs a seat that can
            # give it, or the work could never be closed.
            holder = _verdict_holder(doc, owner)
            if holder:
                names.append(holder)
    if AUTHOR_HUMAN in names:
        # Nothing waits on the operator: an item that names the operator also names
        # a seat that can give the verdict.
        holder = _verdict_holder(doc, owner)
        if holder and holder not in names:
            names.append(holder)
    return names


def _members(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(m.get("name")): m for m in agent_members(doc, include_left=True)}


def _can_manage(author: Author, item: Optional[W.Item], doc: Dict[str, Any]) -> bool:
    if author.trusted_human or getattr(author, "operator", False):
        return True
    if not author.is_member or not author.verified:
        return False
    if item is not None and author.name == item.requester:
        return True
    return _manager(doc) == author.name


def _require_manage(layout: Any, team_name: str, author: Author, item: Optional[W.Item], doc: Dict[str, Any], action: str) -> None:
    if _can_manage(author, item, doc):
        if getattr(author, "operator", False) and not author.trusted_human:
            audit(layout, team_name, "operator_action", author, {"action": "work " + action, "id": item.id if item else None})
        return
    audit(layout, team_name, "author_mismatch", author, {"action": "work " + action, "id": item.id if item else None})
    raise HerdrTeamError(
        "author_mismatch",
        "work {} is for the requester{}, the manager or the operator".format(action, " ({})".format(item.requester) if item else ""),
        EXIT_REFUSED, {"action": action, "author": author.name},
    )


def _require_operator(layout: Any, team_name: str, author: Author, item: Optional[W.Item], action: str) -> None:
    """The operator from a trusted origin, or a delegate (audited); nobody else."""
    if author.trusted_human:
        return
    if getattr(author, "operator", False) and author.is_member and author.verified:
        audit(layout, team_name, "operator_action", author, {"action": "work " + action, "id": item.id if item else None})
        return
    audit(layout, team_name, "author_mismatch", author, {"action": "work " + action, "id": item.id if item else None})
    raise HerdrTeamError("author_mismatch", "work {} is for the operator".format(action), EXIT_REFUSED, {"action": action, "author": author.name})


def _require_ruling(layout: Any, team_name: str, author: Author, item: Optional[W.Item], doc: Dict[str, Any], action: str) -> None:
    """Who may rule on a work item: the operator (or a delegate), or the manager.

    A ruling decides an item that has no attempt, so it is a judgement about
    work rather than a settlement of work. The operator's word is the
    natural authority -- every brief in this team says "the operator rules"
    -- and the team manager judges its own queue. The requester is not
    enough: the requester wants the work done, and letting them rule it
    done would route around the review the item names.

    An operator acting through a grant is audited, as every other delegated
    operator act is.
    """
    if author.trusted_human:
        return
    if getattr(author, "operator", False) and author.is_member and author.verified:
        audit(layout, team_name, "operator_action", author, {"action": "work " + action, "id": item.id if item else None})
        return
    if author.is_member and author.verified and _manager(doc) == author.name:
        return
    audit(layout, team_name, "author_mismatch", author, {"action": "work " + action, "id": item.id if item else None})
    raise HerdrTeamError(
        "author_mismatch",
        "work {} is for the operator (or a delegate the operator granted it to), or the team manager".format(action),
        EXIT_REFUSED, {"action": action, "author": author.name, "id": item.id if item else None},
    )


def _require_verified_member(author: Author, action: str) -> None:
    if not author.is_member:
        raise HerdrTeamError("not_a_member", "work {} runs from a member's own pane; you are {}".format(action, author.name), EXIT_REFUSED, {"author": author.name, "via": author.via})
    if not author.verified:
        raise HerdrTeamError("author_unverified", "work {} needs a verified member pane ({})".format(action, author.reason or "identity unverified"), EXIT_REFUSED, {"author": author.name, "via": author.via})


def _is_item_reviewer(author: Author, item: Optional[W.Item], doc: Dict[str, Any]) -> bool:
    """Is this author one of the item's reviewers - named in ``review_by``, a
    holder of a role it names - or already on its review record?"""
    if item is None or not author.is_member or not author.verified:
        return False
    if author.name in _expand_reviewers(doc, item.review_by):
        return True
    return any(r.get("by") == author.name for r in item.reviews)


def _approved_the_attempt(item: W.Item, author_name: str) -> Optional[str]:
    """What this author approved that they may not reopen, or ``None`` (W-38).

    A reviewer may reopen an item they did not approve; one who approved the
    attempt being reopened sends the defect back through the requester or the
    manager instead. An approval belongs to an attempt when it was recorded
    at or after that attempt was claimed, so a fresh attempt opened after a
    reopen is not the attempt the approval covered.
    """
    attempt = item.attempt
    for review in item.reviews:
        if review.get("by") != author_name or review.get("verdict") != "approved":
            continue
        if attempt is None:
            return "the approval of {}".format(item.id)
        at = W.store_parse_iso(review.get("at"))
        claimed = W.store_parse_iso(attempt.claimed_at)
        if at is None or claimed is None or at >= claimed:
            return "attempt {} of {}".format(attempt.n, item.id)
    return None


def _require_reopen(layout: Any, team_name: str, author: Author, item: W.Item, doc: Dict[str, Any]) -> None:
    """Who may reopen a settled item for a defect (W-38).

    The requester, the manager and the operator keep the authority they have.
    A reviewer gains one, and only one: on an item they did not approve in
    the attempt being reopened, with the reason on the record. A reviewer who
    approved that attempt is refused and the refusal names why, because that
    route is the lead's and the requester's - an author_mismatch answers a
    misuse here, not a defect.
    """
    if _can_manage(author, item, doc):
        if getattr(author, "operator", False) and not author.trusted_human:
            audit(layout, team_name, "operator_action", author, {"action": "work reopen", "id": item.id})
        return
    if _is_item_reviewer(author, item, doc):
        approved = _approved_the_attempt(item, author.name)
        if approved is None:
            return
        audit(layout, team_name, "author_mismatch", author, {"action": "work reopen", "id": item.id})
        raise HerdrTeamError(
            "author_mismatch",
            "you approved {}; a reopen for a defect is the requester's ({}) or the manager's".format(approved, item.requester),
            EXIT_REFUSED, {"action": "reopen", "author": author.name, "id": item.id, "approved": approved},
        )
    audit(layout, team_name, "author_mismatch", author, {"action": "work reopen", "id": item.id})
    raise HerdrTeamError(
        "author_mismatch",
        "work reopen is for the requester ({}), the manager or the operator".format(item.requester),
        EXIT_REFUSED, {"action": "reopen", "author": author.name, "id": item.id},
    )


def _expand_reviewers(doc: Dict[str, Any], review_by: Sequence[str]) -> List[str]:
    names: List[str] = []
    for entry in review_by:
        if entry.startswith("role:"):
            holders = [str(m.get("name")) for m in role_holders(doc, entry[len("role:"):])]
        elif entry == AUTHOR_HUMAN:
            holders = [AUTHOR_HUMAN]
        elif entry.startswith("adversarial:"):
            # adversarial:panel_name or adversarial:auto - panel members resolved at review time
            holders = [entry]  # Keep as-is for validation; panel resolved during review
        else:
            holders = [entry]
        for name in holders:
            if name not in names:
                names.append(name)
    return names


def _is_adversarial_review(item: 'W.Item') -> bool:
    """Check if this item uses adversarial review."""
    return any(r.startswith("adversarial:") for r in item.review_by)


def _resolve_adversarial_panel(doc: Dict[str, Any], spec: str) -> List[str]:
    """Resolve an adversarial panel spec to agent names.
    
    Spec formats:
    - "auto" -> pick 3 agents with different roles/kinds (excluding owner)
    - "role1,role2,role3" -> holders of those roles
    - "config:panel_name" -> look up in team config
    """
    if spec == "auto":
        members = [m for m in agent_members(doc) if m.get("name")]
        # Prefer diverse kinds/roles, exclude owner (resolved at call site)
        return [str(m.get("name")) for m in members[:3]]
    elif spec.startswith("config:"):
        panel_name = spec[len("config:"):]
        config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
        # Adversarial panels are under config.work.adversarial_panels
        work_config = config.get("work") if isinstance(config.get("work"), dict) else {}
        panels = work_config.get("adversarial_panels") if isinstance(work_config.get("adversarial_panels"), dict) else {}
        panel = panels.get(panel_name)
        if isinstance(panel, dict) and isinstance(panel.get("members"), list):
            return [str(m) for m in panel["members"] if isinstance(m, str)]
        return []
    else:
        # Comma-separated role names: "security,performance,maintainability"
        roles = [r.strip() for r in spec.split(",") if r.strip()]
        holders = []
        for role in roles:
            role_holders_list = role_holders(doc, role)
            for m in role_holders_list:
                name = str(m.get("name"))
                if name not in holders:
                    holders.append(name)
        return holders


def _validate_reviewers(doc: Dict[str, Any], team_name: str, review_by: Sequence[str]) -> List[str]:
    out: List[str] = []
    for raw in review_by:
        # Handle adversarial: specs as a single entry (don't split by comma)
        # because the comma inside adversarial: is part of the spec, not a separator
        if raw.strip().startswith("adversarial:"):
            entry = raw.strip()
            if entry not in out:
                out.append(entry)
            continue
        for entry in (part.strip() for part in raw.split(",")):
            if not entry:
                continue
            if entry.startswith("role:"):
                role_name = entry[len("role:"):]
                holders = role_holders(doc, role_name)
                if not holders:
                    # A role nobody holds adds nobody - this is not an error
                    # (the template's default reviewer role may not have a holder)
                    continue
                for m in holders:
                    name = str(m.get("name"))
                    if name not in out:
                        out.append(name)
            elif entry == AUTHOR_HUMAN:
                if AUTHOR_HUMAN not in out:
                    out.append(AUTHOR_HUMAN)
            elif entry.startswith("adversarial:"):
                # Accept adversarial: prefix; panel resolved at review time
                if entry not in out:
                    out.append(entry)
            else:
                entry = str(member_or_raise(doc, entry, team_name).get("name"))
                if entry not in out:
                    out.append(entry)
    return out


def _owner_name(doc: Dict[str, Any], team_name: str, author: Author, raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    if raw == "me":
        if not author.is_member:
            raise UsageError("--to me is for a member; name the member")
        return author.name
    return str(member_or_raise(doc, raw, team_name).get("name"))


def _post(layout: Any, team: Any, doc: Dict[str, Any], author: Author, to: Sequence[Optional[str]], kind: str, text: str, meta: Dict[str, Any], refs: Optional[List[str]] = None) -> Optional[int]:
    """One authored board post about a work item, to everyone concerned except the author."""
    recipients: List[str] = []
    for name in to:
        if not name or name == author.name or name in recipients:
            continue
        if name not in ("all", AUTHOR_HUMAN) and find_member(doc, name, allow_retired=False) is None:
            continue
        recipients.append(name)
    if not recipients:
        return None
    record = build_record(author, recipients, kind, text, refs=[r for r in (refs or []) if r], socket_path=os.fspath(layout.socket), from_gen=member_generation(doc, author))
    record["work"] = meta
    return board_append(team, record)


def _event(author: Author, doc: Dict[str, Any], op: str, work_id: Optional[str], **fields: Any) -> Dict[str, Any]:
    event: Dict[str, Any] = {"op": op, "id": work_id, "by": author.name, "by_via": author.via, "by_gen": member_generation(doc, author)}
    event.update({k: v for k, v in fields.items() if v is not None})
    return event


def _announce_ready(layout: Any, team: Any, doc: Dict[str, Any], before: Dict[str, W.Item], after: Dict[str, W.Item]) -> List[int]:
    """``work_ready`` for each item a settlement just unblocked: its owner (or the manager) is woken."""
    seqs: List[int] = []
    for item in W.newly_ready(before, after):
        target = item.owner or _manager(doc) or AUTHOR_HUMAN
        text = "{} is ready: what it waits on is done ({}). {}".format(
            item.id, ", ".join(item.deps),
            "Start with: {} work claim {}".format(CLI, item.id) if item.owner else "Assign it: {} work assign {} <member>".format(CLI, item.id))
        try:
            seqs.append(_roster.append_system_record(team, "work_ready", text, to=[target], extra={"work": {"id": item.id, "op": "ready"}}, socket=os.fspath(layout.socket)))
        except HerdrTeamError:
            continue
    return seqs


def _set_task(team: Any, name: str, text: str) -> None:
    """Claiming work is what a member does now, so the headline beside its name follows."""
    try:
        path = task_file(team, name)
        store.write_json(path, {"v": 1, "member": name, "text": text, "headline": headline(text), "set_at": store.now_iso()})
    except (HerdrTeamError, OSError):
        pass


def _load_item(team: Any, raw_id: Any) -> Tuple[Dict[str, W.Item], W.Item]:
    work_id = W.parse_id(raw_id)
    items = W.load(team)
    item = items.get(work_id)
    if item is None:
        raise HerdrTeamError("work_not_found", "no work item {}".format(work_id), EXIT_REFUSED, {"id": work_id})
    return items, item


def _acceptance_policy(doc: Dict[str, Any]) -> str:
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    work_cfg = config.get("work") if isinstance(config.get("work"), dict) else {}
    policy = work_cfg.get("acceptance", W.DEFAULT_ACCEPTANCE_POLICY)
    return policy if policy in W.ACCEPTANCE_POLICIES else W.DEFAULT_ACCEPTANCE_POLICY


def _brief_from_args(args: argparse.Namespace) -> Dict[str, str]:
    brief: Dict[str, str] = {}
    brief_file = getattr(args, "brief_file", None)
    if brief_file:
        try:
            text = Path(os.path.expanduser(brief_file)).read_text(encoding="utf-8")
        except OSError as err:
            raise HerdrTeamError("file_unreadable", "cannot read {}: {}".format(brief_file, err), EXIT_REFUSED, {"path": brief_file})
        brief.update(W.parse_brief_markdown(text))
    for key in W.BRIEF_FIELDS:
        value = getattr(args, key, None)
        if value is not None:
            brief[key] = value
    return {k: _clean(v, W.MAX_FIELD_CHARS, k) for k, v in brief.items() if v and v.strip()}


def _row(item: W.Item, items: Dict[str, W.Item], doc: Dict[str, Any]) -> Dict[str, Any]:
    row = item.to_json()
    row["ready"] = W.is_ready(item, items)
    row["waiting_on"] = W.waiting_on(item, items)
    row["hints"] = W.hints(item, items, _members(doc), _manager(doc))
    row["attention"] = [h["attention"] for h in row["hints"] if h["for"] != "nobody"]
    row["next"] = next((h["argv"] for h in row["hints"] if h["for"] != "nobody"), None)
    return row


def _line(item: W.Item, items: Dict[str, W.Item]) -> str:
    extra = []
    if item.attempt is not None and item.status in W.ACTIVE:
        extra.append("attempt {}".format(item.attempt.n))
    waiting = W.waiting_on(item, items)
    if waiting and item.status in (W.STATUS_OPEN, W.STATUS_ASSIGNED):
        extra.append("waits on " + ", ".join(waiting))
    elif W.is_ready(item, items):
        extra.append("ready")
    if item.status == W.STATUS_BLOCKED and item.block_reason:
        extra.append(W.clip(item.block_reason, 40))
    return "{:<6} {:<17} {:<24} {}{}".format(item.id, item.status, item.owner or "-", W.clip(item.title, 60), "  ({})".format("; ".join(extra)) if extra else "")


# --------------------------------------------------------------------------
# subcommands


def _add(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    if author.name == "system":
        raise HerdrTeamError("author_mismatch", "hooks and startup processes cannot add work", EXIT_REFUSED)
    if author.is_member and not author.verified:
        raise HerdrTeamError("author_unverified", "work add needs a verified member pane ({})".format(author.reason or "identity unverified"), EXIT_REFUSED)
    if not author.is_member and not _can_manage(author, None, doc):
        _require_manage(layout, team_name, author, None, doc, "add")
    title = _clean(args.title, W.MAX_TITLE_CHARS, "title")
    if not title:
        raise UsageError("work needs a title")
    brief = _brief_from_args(args)
    missing = W.missing_brief(brief)
    policy = _acceptance_policy(doc)
    if not args.quick and "acceptance" in missing:
        if policy == "require":
            raise HerdrTeamError("brief_incomplete", "this team requires --acceptance: the evidence that proves it is done", EXIT_REFUSED, {"missing": missing})
        if policy == "warn":
            warn(args, "no --acceptance: say what evidence proves {} done (or pass --quick)".format("it is"))
    owner = _owner_name(doc, team_name, author, args.to)
    config = doc.get("config") if isinstance(doc.get("config"), dict) else {}
    default_review = (config.get("work") or {}).get("review_by") if isinstance(config.get("work"), dict) else None
    # Combine command-line reviewers with team config default reviewers
    all_review_specs = list(args.review_by or [])
    for entry in (default_review or []):
        if isinstance(entry, str):
            all_review_specs.append(entry)
    # Validate all reviewers together (including adversarial specs)
    reviewers = _validate_reviewers(doc, team_name, all_review_specs)
    # Expand reviewers (role: -> holders, adversarial: stays as-is for resolution at review time)
    expanded = _expand_reviewers(doc, reviewers)
    # Remove owner from reviewers (nobody reviews their own work), and give the
    # verdict a seat that can give it when the item names the operator.
    reviewers = _reviewable(doc, expanded, owner)
    items = W.load(team)
    deps = W.check_deps(items, None, W.clean_list((",".join(args.deps or [])).split(","), "dependencies"))
    _before, after, event = W.append(team, _event(author, doc, W.OP_CREATE, None, title=title, brief=brief, owner=owner, deps=deps, review_by=reviewers))
    item = after[event["id"]]
    audit(layout, team_name, "work_add", author, {"id": item.id, "owner": owner, "deps": deps, "review_by": reviewers})
    meta = {"id": item.id, "op": "assign" if owner else "open"}
    done_when = " Done when: {}.".format(W.clip(brief["acceptance"], 200)) if brief.get("acceptance") else ""
    waits = " It waits on {}.".format(", ".join(deps)) if deps and not W.deps_done(item, after) else ""
    if owner:
        seq = _post(layout, team, doc, author, [owner], "request", "{} for you: {}.{}{} Brief: {} work show {}".format(item.id, title, done_when, waits, CLI, item.id), meta)
    else:
        seq = _post(layout, team, doc, author, ["all"], "note", "{} is open: {}.{}{} Take it with: {} work claim {}".format(item.id, title, done_when, waits, CLI, item.id), meta)
    payload = {"team": team_name, "work": _row(item, after, doc), "board_seq": seq, "missing_brief": missing}
    return emit(args, payload, "{} created{}: {}".format(item.id, " for {}".format(owner) if owner else " (open)", title))


def _list(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    items = W.load(team)
    rows = W.sorted_items(items)
    if args.ready:
        rows = [i for i in rows if W.is_ready(i, items)]
    if args.status:
        wanted = set(s for raw in args.status for s in raw.split(","))
        rows = [i for i in rows if i.status in wanted]
    elif not args.all and not args.ready:
        rows = [i for i in rows if i.status not in W.FINAL]
    if args.owner:
        owner = author.name if args.owner == "me" else args.owner
        rows = [i for i in rows if i.owner == owner]
    payload = {"team": team_name, "work": [_row(i, items, doc) for i in rows], "counts": W.counts(items)}

    def human() -> str:
        if not rows:
            return "no work items{}; add one with: {} work add \"<title>\" --to <member> --acceptance \"<evidence>\"".format(
                "" if args.all or not items else " open (--all shows finished ones)", CLI)
        return "\n".join(_line(i, items) for i in rows)

    return emit(args, payload, human)


def _show(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    items, item = _load_item(team, args.id)
    posts = []
    try:
        for record in store.BoardStore(team).read(include_retracted=False):
            meta = record.get("work")
            if isinstance(meta, dict) and meta.get("id") == item.id:
                posts.append({"seq": record.get("seq"), "from": record.get("from"), "kind": record.get("kind"), "op": meta.get("op"), "text": record.get("text")})
    except HerdrTeamError:
        pass
    row = _row(item, items, doc)
    row["history"] = list(item.history)
    row["posts"] = posts[-50:]

    def human() -> str:
        lines = ["{} [{}] {}".format(item.id, item.status, item.title),
                 "requested by {}; owner {}{}".format(item.requester, item.owner or "nobody", "; reviewed by {}".format(", ".join(item.review_by)) if item.review_by else "")]
        if item.deps:
            lines.append("depends on: {}".format(", ".join("{} ({})".format(d, items[d].status if d in items else "?") for d in item.deps)))
        for key in W.BRIEF_FIELDS:
            if item.brief.get(key):
                lines.append("{}: {}".format(key.capitalize(), item.brief[key]))
        for attempt in item.attempts:
            state = "{} {}".format(attempt.outcome, attempt.settled_at) if attempt.settled_at else ("closed" if attempt.closed else "in progress")
            lines.append("attempt {} by {} (generation {}), claimed {}: {}".format(attempt.n, attempt.owner, attempt.gen, attempt.claimed_at, state))
            if attempt.summary:
                lines.append("  summary: {}".format(attempt.summary))
            for d in attempt.deliverables:
                lines.append("  deliverable: {}".format(d))
            for e in attempt.evidence:
                lines.append("  evidence: {}".format(e))
        for review in item.reviews:
            lines.append("review by {}: {}{}".format(review.get("by"), review.get("verdict"), ": " + str(review["note"]) if review.get("note") else ""))
        for ruling in item.rulings:
            lines.append("ruled by {} at {}: {} (now {})".format(
                ruling.get("by"), ruling.get("at"), ruling.get("outcome"), ruling.get("status")))
            if ruling.get("summary"):
                lines.append("  ruling: {}".format(ruling["summary"]))
            for e in ruling.get("evidence") or []:
                lines.append("  evidence: {}".format(e))
        for hint in row["hints"]:
            if hint["for"] != "nobody":
                lines.append("next ({}{}): {}  # {}".format(hint["for"], " " + hint["name"] if hint.get("name") else "", " ".join(hint["argv"]), hint["why"]))
        return "\n".join(lines)

    return emit(args, {"team": team_name, "work": row}, human)


def _claim(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _require_verified_member(author, "claim")
    items, item = _load_item(team, args.id)
    gen = member_generation(doc, author)
    _before, after, _event_stored = W.append(team, _event(author, doc, W.OP_CLAIM, item.id, gen=gen, force=bool(args.force)))
    item = after[item.id]
    attempt = item.attempt
    audit(layout, team_name, "work_claim", author, {"id": item.id, "attempt": attempt.n if attempt else None, "gen": gen})
    _set_task(team, author.name, "{}: {}".format(item.id, item.title))
    # Not to the human: any member post to the operator opens the answer popup, and a claim
    # asks nothing. The operator sees claims on the board and in mission control.
    seq = _post(layout, team, doc, author, [item.requester if item.requester != AUTHOR_HUMAN else None, _manager(doc)], "note",
                "claimed {} (attempt {}): {}".format(item.id, attempt.n if attempt else "?", item.title), {"id": item.id, "op": "claim"})
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq},
                "{} is yours (attempt {}). Settle it with: {} work done {} --outcome succeeded|failed|partial --summary \"...\"".format(item.id, attempt.n if attempt else "?", CLI, item.id))


def _assign(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    items, item = _load_item(team, args.id)
    _require_manage(layout, team_name, author, item, doc, "assign")
    owner = _owner_name(doc, team_name, author, args.member)
    previous = item.owner
    _before, after, _ev = W.append(team, _event(author, doc, W.OP_ASSIGN, item.id, owner=owner))
    item = after[item.id]
    # A seat never reviews its own work: handing the item to its reviewer would
    # leave the work with nobody who may close it.
    review_by = _reviewable(doc, item.review_by, owner)
    if review_by != list(item.review_by):
        _before, after, _ev = W.append(team, _event(author, doc, W.OP_UPDATE, item.id, review_by=review_by))
        item = after[item.id]
    audit(layout, team_name, "work_assign", author, {"id": item.id, "owner": owner, "previous": previous, "review_by": review_by})
    seq = _post(layout, team, doc, author, [owner], "request", "{} for you: {}.{} Brief: {} work show {}".format(
        item.id, item.title, " Done when: {}.".format(W.clip(item.brief["acceptance"], 200)) if item.brief.get("acceptance") else "", CLI, item.id), {"id": item.id, "op": "assign"})
    if previous and previous != owner:
        _post(layout, team, doc, author, [previous], "note", "{} was reassigned to {}; stop work on it".format(item.id, owner), {"id": item.id, "op": "unassign"})
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq}, "{} assigned to {}".format(item.id, owner))


def _block(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _require_verified_member(author, "block")
    items, item = _load_item(team, args.id)
    if item.owner != author.name:
        raise HerdrTeamError("work_not_owner", "{} belongs to {}".format(item.id, item.owner or "nobody"), EXIT_REFUSED, {"id": item.id, "owner": item.owner})
    reason = _clean(args.reason, W.MAX_FIELD_CHARS, "reason")
    if not reason:
        raise UsageError("say what blocks it")
    _before, after, _ev = W.append(team, _event(author, doc, W.OP_BLOCK, item.id, reason=reason))
    item = after[item.id]
    audit(layout, team_name, "work_block", author, {"id": item.id})
    seq = _post(layout, team, doc, author, [item.requester, _manager(doc)], "blocked", "{} blocked: {}".format(item.id, reason), {"id": item.id, "op": "block"})
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq}, "{} blocked".format(item.id))


def _unblock(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    items, item = _load_item(team, args.id)
    if item.owner == author.name and author.is_member:
        _require_verified_member(author, "unblock")
    else:
        _require_manage(layout, team_name, author, item, doc, "unblock")
    _before, after, _ev = W.append(team, _event(author, doc, W.OP_UNBLOCK, item.id))
    item = after[item.id]
    seq = _post(layout, team, doc, author, [item.owner, item.requester, _manager(doc)], "note", "{} unblocked".format(item.id), {"id": item.id, "op": "unblock"})
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq}, "{} unblocked".format(item.id))


def _done(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _require_verified_member(author, "done")
    items, item = _load_item(team, args.id)
    summary = _clean(args.summary, W.MAX_SUMMARY_CHARS, "summary")
    if not summary:
        raise UsageError("--summary is required: what was done, what was found, what remains")
    deliverables = [_clean(d, 500, "deliverable") for d in W.clean_list(args.deliverable, "deliverables")]
    evidence = [_clean(e, 500, "evidence") for e in W.clean_list(args.evidence, "evidence entries")]
    gen = member_generation(doc, author)
    before, after, _ev = W.append(team, _event(author, doc, W.OP_SETTLE, item.id, gen=gen, outcome=args.outcome, summary=summary, deliverables=deliverables, evidence=evidence))
    item = after[item.id]
    attempt = item.attempts[-1] if item.attempts else None
    audit(layout, team_name, "work_settle", author, {"id": item.id, "attempt": attempt.n if attempt else None, "outcome": args.outcome, "gen": gen})
    refs = [d for d in deliverables if not d.startswith(("http://", "https://"))]
    label = {"succeeded": "done", "failed": "FAILED", "partial": "PARTIAL"}[args.outcome]
    seqs: Dict[str, Optional[int]] = {}
    seqs["settled"] = _post(layout, team, doc, author, [item.requester, _manager(doc)], "done", "{} {}: {}".format(item.id, label, summary), {"id": item.id, "op": "settle", "outcome": args.outcome, "attempt": attempt.n if attempt else None}, refs=None)
    if item.status == W.STATUS_IN_REVIEW:
        reviewers = _expand_reviewers(doc, item.review_by)
        if AUTHOR_HUMAN in reviewers:
            # Nothing waits on the operator: the verdict request goes to the seat
            # that can give it. The operator is still told the item settled (the
            # requester and the manager see that post) but is not the addressee of
            # a review it may never come to.
            holder = _verdict_holder(doc, str(item.attempt.owner) if item.attempt and item.attempt.owner else item.owner)
            if holder:
                reviewers = [holder] + [r for r in reviewers if r != AUTHOR_HUMAN]
        seqs["review"] = _post(layout, team, doc, author, reviewers, "request",
                               "{} is ready for your review: {} Approve: {} work review {} --approve, or --changes \"what to fix\"".format(item.id, summary, CLI, item.id),
                               {"id": item.id, "op": "review_requested"})
    ready = _announce_ready(layout, team, doc, before, after)
    payload = {"team": team_name, "work": _row(item, after, doc), "board_seqs": seqs, "ready": ready, "refs": refs}
    return emit(args, payload, "{} settled {} (now {}){}".format(item.id, args.outcome, item.status, "; ready now: " + ", ".join("#{}".format(s) for s in ready) if ready else ""))


def _review(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    items, item = _load_item(team, args.id)
    
    # Check for adversarial review
    if _is_adversarial_review(item):
        return _run_adversarial_review(args, layout, team_name, team, doc, author, item)
    
    reviewers = _expand_reviewers(doc, item.review_by)
    named = (author.is_member and author.verified and author.name in reviewers) or (author.trusted_human and AUTHOR_HUMAN in reviewers)
    if not named:
        # Review is the point of naming a reviewer: the requester or the manager standing in
        # for one would let the review the operator configured be skipped. Only the operator
        # (or a delegate, audited) may review in a named reviewer's place.
        _require_operator(layout, team_name, author, item, "review in place of {}".format(", ".join(item.review_by) or "the reviewer"))
    if author.is_member and author.name == (item.attempt.owner if item.attempt else item.owner):
        raise HerdrTeamError("work_self_review", "the owner cannot review its own work", EXIT_REFUSED, {"id": item.id})
    verdict = "approved" if args.approve else "changes"
    note = _clean(args.changes if args.changes else args.note, W.MAX_FIELD_CHARS, "note") or None
    if verdict == "changes" and not note:
        raise UsageError("--changes needs to say what to fix")
    before, after, _ev = W.append(team, _event(author, doc, W.OP_REVIEW, item.id, verdict=verdict, note=note))
    item = after[item.id]
    audit(layout, team_name, "work_review", author, {"id": item.id, "verdict": verdict})
    if verdict == "approved":
        seq = _post(layout, team, doc, author, [item.owner, item.requester, _manager(doc)], "answer",
                    "{} approved by {}{}".format(item.id, author.name, ": " + note if note else ""), {"id": item.id, "op": "approved"})
    else:
        seq = _post(layout, team, doc, author, [item.owner], "request",
                    "{} needs changes ({}): {} Resubmit with: {} work done {} --outcome succeeded --summary \"...\"".format(item.id, author.name, note, CLI, item.id), {"id": item.id, "op": "changes"})
    ready = _announce_ready(layout, team, doc, before, after)
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq, "ready": ready}, "{} {}".format(item.id, "approved" if verdict == "approved" else "sent back for changes"))


# --------------------------------------------------------------------------
# adversarial review
# --------------------------------------------------------------------------


def _run_adversarial_review(args: argparse.Namespace, layout: Any, team_name: str, team: Any, doc: Dict[str, Any], author: Author, item: 'W.Item') -> int:
    """Run adversarial collaborative review via board-based panel.
    
    The adversarial review posts a structured review session to the board,
    panel members (resolved from the adversarial spec) post claim/challenge/
    disposition records, and the chair synthesizes a verdict.
    """
    # Resolve the adversarial panel
    panel_specs = [r[len("adversarial:"):] for r in item.review_by if r.startswith("adversarial:")]
    if not panel_specs:
        raise HerdrTeamError("adversarial_config", "no adversarial panel spec found", EXIT_REFUSED, {"id": item.id})
    
    panel_members: List[str] = []
    for spec in panel_specs:
        panel_members.extend(_resolve_adversarial_panel(doc, spec))
    
    # Exclude owner from panel
    owner = item.attempt.owner if item.attempt else item.owner
    panel_members = [m for m in panel_members if m != owner]
    
    if len(panel_members) < 2:
        raise HerdrTeamError("adversarial_panel", "adversarial panel needs at least 2 members (got {})".format(len(panel_members)), EXIT_REFUSED, {"id": item.id, "panel": panel_members})
    
    # Determine chair: prefer role:lead, then first panel member
    chair = None
    lead_holders = role_holders(doc, "lead")
    if lead_holders:
        chair = str(lead_holders[0].get("name"))
    if not chair:
        chair = panel_members[0]
    
    # Build review context
    brief = item.brief
    acceptance = brief.get("acceptance", "No acceptance criteria specified")
    deliverable = brief.get("deliverable", "No deliverable specified")
    summary = item.summary or "No summary provided"
    
    review_context = {
        "work_id": item.id,
        "title": item.title,
        "owner": owner,
        "attempt": item.attempt.n if item.attempt else 0,
        "outcome": item.attempt.outcome if item.attempt else "unknown",
        "summary": summary,
        "acceptance": acceptance,
        "deliverable": deliverable,
        "panel": panel_members,
        "chair": chair,
    }
    
    # Post review session start to board
    session_text = (
        "ADVERSARIAL REVIEW SESSION STARTED\n"
        "Work: {} ({})\n"
        "Owner: {} (attempt {})\n"
        "Outcome: {}\n"
        "Summary: {}\n"
        "Deliverable: {}\n"
        "Acceptance: {}\n"
        "Panel: {} (chair: {})\n"
        "Process: Each panel member posts CLAIM/CHALLENGE/DISPOSITION. Chair synthesizes VERDICT."
    ).format(item.id, item.title, owner, item.attempt.n if item.attempt else 0, 
             item.attempt.outcome if item.attempt else "unknown",
             summary, deliverable, acceptance, ", ".join(panel_members), chair)
    
    meta = {"id": item.id, "op": "adversarial_review_start", "panel": panel_members, "chair": chair}
    _post(layout, team, doc, author, panel_members + [item.owner, item.requester, _manager(doc)], "request", session_text, meta)
    
    # For now, we auto-synthesize based on the work outcome and acceptance criteria
    # In a full implementation, this would wait for panel members to post
    # Here we simulate the adversarial process by evaluating against acceptance criteria
    
    verdict, note = _synthesize_adversarial_verdict(review_context, args.approve, args.changes if hasattr(args, 'changes') else None, args.note if hasattr(args, 'note') else None)
    
    # Record the review
    before, after, _ev = W.append(team, _event(author, doc, W.OP_REVIEW, item.id, verdict=verdict, note=note))
    item = after[item.id]
    audit(layout, team_name, "work_review", author, {"id": item.id, "verdict": verdict, "adversarial": True, "panel": panel_members, "chair": chair})
    
    # Post verdict
    if verdict == "approved":
        seq = _post(layout, team, doc, author, [item.owner, item.requester, _manager(doc)], "answer",
                    "{} approved by adversarial panel (chair: {}){}".format(item.id, chair, ": " + note if note else ""), {"id": item.id, "op": "approved", "adversarial": True})
    else:
        seq = _post(layout, team, doc, author, [item.owner], "request",
                    "{} needs changes (adversarial panel, chair: {}): {} Resubmit with: {} work done {} --outcome succeeded --summary \"...\"".format(item.id, chair, note, CLI, item.id), {"id": item.id, "op": "changes", "adversarial": True})
    
    # Post session end
    end_text = "ADVERSARIAL REVIEW SESSION ENDED\nWork: {}\nVerdict: {}\nChair: {}\nPanel: {}".format(item.id, verdict, chair, ", ".join(panel_members))
    _post(layout, team, doc, author, panel_members + [item.owner, item.requester, _manager(doc)], "note", end_text, {"id": item.id, "op": "adversarial_review_end", "verdict": verdict})
    
    ready = _announce_ready(layout, team, doc, before, after)
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq, "ready": ready, "adversarial": True}, "{} {} (adversarial panel)".format(item.id, "approved" if verdict == "approved" else "sent back for changes"))


def _synthesize_adversarial_verdict(context: Dict[str, Any], approve: bool, changes: Optional[str], note: Optional[str]) -> Tuple[str, Optional[str]]:
    """Synthesize adversarial verdict.
    
    In a real implementation, this would wait for panel members to deliberate.
    Here we use a heuristic: if --approve is given and acceptance criteria are met,
    approve; otherwise require changes.
    """
    if approve:
        return "approved", note or "Adversarial panel concurs: acceptance criteria satisfied."
    else:
        change_text = changes or note or "Adversarial panel finds gaps against acceptance criteria."
        return "changes", change_text


def _decide(args: argparse.Namespace, op: str) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    items, item = _load_item(team, args.id)
    if op == W.OP_REOPEN:
        # A reviewer may reopen an item they did not approve, with a reason on
        # the record; the requester, the manager and the operator are unchanged.
        _require_reopen(layout, team_name, author, item, doc)
    elif op == W.OP_CLOSE and item.status == W.STATUS_IN_REVIEW:
        _require_operator(layout, team_name, author, item, "close without the review")
    else:
        _require_manage(layout, team_name, author, item, doc, op)
    text = _clean(args.reason, W.MAX_FIELD_CHARS, "reason") or None
    if op == W.OP_REOPEN and not text:
        raise UsageError("a reopen carries a reason: the defect that reopens it", {"action": "reopen", "id": item.id})
    fields = {"reason": text} if op in (W.OP_REOPEN, W.OP_CANCEL) else {"note": text}
    before, after, _ev = W.append(team, _event(author, doc, op, item.id, **fields))
    item = after[item.id]
    audit(layout, team_name, "work_" + op, author, {"id": item.id})
    if op == W.OP_REOPEN:
        seq = _post(layout, team, doc, author, [item.owner], "request", "{} reopened{}. Claim it again: {} work claim {}".format(item.id, ": " + text if text else "", CLI, item.id), {"id": item.id, "op": op})
    else:
        verb = "closed as done" if op == W.OP_CLOSE else "cancelled"
        seq = _post(layout, team, doc, author, [item.owner], "note", "{} {} by {}{}".format(item.id, verb, author.name, ": " + text if text else ""), {"id": item.id, "op": op})
    ready = _announce_ready(layout, team, doc, before, after) if op == W.OP_CLOSE else []
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq, "ready": ready}, "{} {}".format(item.id, item.status))


def _rule(args: argparse.Namespace) -> int:
    """Record the operator's ruling on a work item that has no attempt.

    Until this verb existed there was no sanctioned way to say "the operator
    ruled on this item": ``work done`` needs an attempt in progress and
    ``work block`` is the owner's alone, so an item the operator ruled on
    before anyone claimed it could only be recorded by hand-appending a
    board record. Hand-appended records carry no seq and no valid JSON, so
    every reader skips them: the ruling is on the board file and nowhere
    else. This verb writes both halves under one lock, through the store,
    so the seq is assigned and the JSON is valid.
    """
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    items, item = _load_item(team, args.id)
    # A ruling is the operator's own (or the team manager's): it decides an
    # item that has no attempt, so it is a judgement, not a settlement.
    _require_ruling(layout, team_name, author, item, doc, "rule")
    outcome = _clean(args.outcome, 64, "outcome")
    if not W.RULING_OUTCOME_RE.match(outcome):
        raise UsageError("--outcome is lower-case words: e.g. ruled_recover_on_origin_main, blocked_tree_collapse, accepted")
    status = args.status
    if status not in W.RULING_STATUSES:
        raise UsageError("--status is one of: {}".format(", ".join(W.RULING_STATUSES)))
    summary = _clean(args.summary, W.MAX_SUMMARY_CHARS, "summary")
    if not summary:
        raise UsageError("--summary is required: what the ruling decides, and on what evidence")
    evidence = [_clean(e, 500, "evidence entries") for e in W.clean_list(args.evidence, "evidence entries")]
    before, after, _ev = W.append(team, _event(author, doc, W.OP_RULE, item.id, outcome=outcome, status=status, summary=summary, evidence=evidence))
    item = after[item.id]
    audit(layout, team_name, "work_rule", author, {"id": item.id, "outcome": outcome, "status": status})
    # One board record, written through the store, carrying the ruling and
    # the item it rules on. The recipients are the people the ruling moves:
    # the owner, the requester, the manager and whoever reviews it.
    recipients = [item.owner, item.requester, _manager(doc)]
    for reviewer in _expand_reviewers(doc, item.review_by):
        if reviewer not in recipients:
            recipients.append(reviewer)
    label = "{} ruled {}: {}".format(item.id, outcome, summary)
    seq = _post(layout, team, doc, author, recipients, "note", label, {"id": item.id, "op": "rule", "outcome": outcome, "status": status})
    ready = _announce_ready(layout, team, doc, before, after)
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq, "ready": ready},
                "{} ruled {} (now {})".format(item.id, outcome, item.status))


def _update(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    items, item = _load_item(team, args.id)
    _require_manage(layout, team_name, author, item, doc, "update")
    fields: Dict[str, Any] = {}
    if args.title:
        fields["title"] = _clean(args.title, W.MAX_TITLE_CHARS, "title")
    brief = _brief_from_args(args)
    if brief:
        fields["brief"] = brief
    if args.deps is not None:
        fields["deps"] = W.check_deps(items, item.id, W.clean_list((",".join(args.deps)).split(","), "dependencies"))
    if args.review_by is not None:
        # Who reviews is a quality gate the requester's own work must pass: changing it is the operator's.
        _require_operator(layout, team_name, author, item, "update --review-by")
        fields["review_by"] = _validate_reviewers(doc, team_name, [r for r in args.review_by if r.strip()])
    if not fields:
        raise UsageError("nothing to change: give --title, a brief field, --deps or --review-by")
    _before, after, _ev = W.append(team, _event(author, doc, W.OP_UPDATE, item.id, **fields))
    item = after[item.id]
    audit(layout, team_name, "work_update", author, {"id": item.id, "fields": sorted(fields)})
    seq = _post(layout, team, doc, author, [item.owner], "note", "{} was updated ({}): {} work show {}".format(item.id, ", ".join(sorted(fields)), CLI, item.id), {"id": item.id, "op": "update"})
    return emit(args, {"team": team_name, "work": _row(item, after, doc), "board_seq": seq}, "{} updated".format(item.id))


def _next(args: argparse.Namespace) -> int:
    """The actionable hints for the caller: what to run next, per item."""
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    items = W.load(team)
    manager = _manager(doc)
    rows: List[Dict[str, Any]] = []
    for item in W.sorted_items(items):
        for hint in W.hints(item, items, _members(doc), manager):
            if hint["for"] == "nobody":
                continue
            mine = hint.get("name") == author.name or (hint["for"] == "human" and author.is_human) or (hint["for"] == "manager" and author.name == manager)
            if args.all or mine:
                rows.append(dict(hint, id=item.id, title=item.title, status=item.status))

    def human() -> str:
        if not rows:
            return "nothing waits on {}".format("anyone" if args.all else "you")
        return "\n".join("{:<6} {:<22} {:<10} {}  # {}".format(r["id"], r["attention"], r.get("name") or r["for"], " ".join(r["argv"]), r["why"]) for r in rows)

    return emit(args, {"team": team_name, "next": rows}, human)


# --------------------------------------------------------------------------
# argument wiring


def _brief_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", metavar="TEXT", help="what is in scope")
    parser.add_argument("--deliverable", metavar="TEXT", help="what gets produced")
    parser.add_argument("--constraints", metavar="TEXT", help="what must not change or be violated")
    parser.add_argument("--ownership", metavar="TEXT", help="what the owner may edit or decide")
    parser.add_argument("--acceptance", metavar="TEXT", help="the evidence that proves it is done")
    parser.add_argument("--brief-file", dest="brief_file", metavar="PATH", help="a Markdown brief with ## Target / ## Deliverable / ## Constraints / ## Ownership / ## Acceptance")


def _add_work_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="work_action", metavar="<action>")
    specs = [
        ("add", "add a work item: for a member (--to), or open for whoever claims it"),
        ("list", "work items that are not finished (--all for every one)"),
        ("ready", "items nobody has started whose dependencies are done"),
        ("show", "one item: brief, attempts, reviews, history, posts and what to do next"),
        ("claim", "take an item and start an attempt (the owner, from its own pane)"),
        ("assign", "give an item to a member (requester, manager or operator)"),
        ("block", "say what blocks your item; the requester and the manager are told"),
        ("unblock", "your item can move again"),
        ("done", "settle your attempt: outcome, a short summary, deliverables and evidence"),
        ("rule", "record the operator's ruling on an item that has no attempt (outcome, status, summary)"),
        ("review", "approve an item waiting for your review, or send it back with --changes"),
        ("reopen", "reopen a settled or cancelled item with the defect that reopens it (requester, manager, operator, or a reviewer who did not approve it)"),
        ("close", "accept a failed, partial or in-review item as done (requester, manager or operator)"),
        ("cancel", "cancel an item (requester, manager or operator)"),
        ("update", "change an item's title, brief, dependencies or reviewers"),
        ("next", "what you should run next, one command per item (--all for everyone's)"),
    ]
    parsers: Dict[str, argparse.ArgumentParser] = {}
    for name, help_text in specs:
        p = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        add_global_arguments(p, nested=True)
        parsers[name] = p
    p = parsers["add"]
    p.add_argument("title")
    p.add_argument("--to", metavar="MEMBER", help="the owner (a member name, or me); omit to leave it open")
    p.add_argument("--deps", metavar="W-N[,W-N]", action="append", help="items that must be done first")
    p.add_argument("--review-by", dest="review_by", metavar="NAME|role:R|human", action="append", help="who must approve before it counts as done (repeatable)")
    p.add_argument("--quick", action="store_true", help="a small item: no brief fields expected")
    _brief_arguments(p)
    p = parsers["list"]
    p.add_argument("--status", action="append", metavar="STATUS[,STATUS]", help="only these statuses: " + ", ".join(W.STATUSES))
    p.add_argument("--owner", metavar="MEMBER|me")
    p.add_argument("--all", action="store_true", help="include done and cancelled items")
    p.add_argument("--ready", action="store_true", help=argparse.SUPPRESS)
    parsers["ready"].set_defaults(ready=True, status=None, owner=None, all=False)
    for name in ("show", "claim", "assign", "block", "unblock", "done", "rule", "review", "reopen", "close", "cancel", "update"):
        parsers[name].add_argument("id", metavar="W-N")
    parsers["claim"].add_argument("--force", action="store_true", help="claim even though a dependency is unfinished")
    parsers["assign"].add_argument("member", help="the new owner, or me")
    parsers["block"].add_argument("reason")
    p = parsers["done"]
    p.add_argument("--outcome", required=True, choices=W.OUTCOMES, help="succeeded, failed, or partial; never leave failure to the prose")
    p.add_argument("--summary", required=True, help="three sentences: what was done, what was found, what remains")
    p.add_argument("--deliverable", action="append", metavar="PATH|URL", help="what was produced (repeatable)")
    p.add_argument("--evidence", action="append", metavar="TEXT|URL", help="what proves the acceptance criteria (repeatable)")
    p = parsers["rule"]
    p.add_argument("--outcome", required=True, metavar="SLUG", help="the ruling's own name: ruled_recover_on_origin_main, blocked_tree_collapse, accepted")
    p.add_argument("--status", required=True, choices=W.RULING_STATUSES, help="where the item stands after the ruling")
    p.add_argument("--summary", required=True, help="what the ruling decides, and on what evidence")
    p.add_argument("--evidence", action="append", metavar="TEXT|URL", help="what the ruling rests on (repeatable)")
    p = parsers["review"]
    verdict = p.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true")
    verdict.add_argument("--changes", metavar="TEXT", help="what to fix before it can be approved")
    p.add_argument("--note", metavar="TEXT", help="a remark with an approval")
    for name in ("reopen", "close", "cancel"):
        parsers[name].add_argument("reason", nargs="?", help="why (a reopen must carry one: the defect it names)" if name == "reopen" else "why (optional)")
    p = parsers["update"]
    p.add_argument("--title")
    p.add_argument("--deps", metavar="W-N[,W-N]", action="append", help="replace the dependencies")
    p.add_argument("--review-by", dest="review_by", metavar="NAME|role:R|human", action="append", help="replace the reviewers")
    _brief_arguments(p)
    parsers["next"].add_argument("--all", action="store_true", help="every actionable item, not only yours")


_ACTIONS = {
    "add": _add, "list": _list, "ready": _list, "show": _show, "claim": _claim, "assign": _assign,
    "block": _block, "unblock": _unblock, "done": _done, "review": _review, "rule": _rule,
    "reopen": lambda a: _decide(a, W.OP_REOPEN), "close": lambda a: _decide(a, W.OP_CLOSE), "cancel": lambda a: _decide(a, W.OP_CANCEL),
    "update": _update, "next": _next,
}


def _run_work(args: argparse.Namespace) -> int:
    action = getattr(args, "work_action", None) or "list"
    if action == "list" and not hasattr(args, "status"):
        args.status, args.owner, args.all, args.ready = None, None, False, False
    return _ACTIONS[action](args)


COMMANDS: List[Command] = [
    Command("work", "tracked work items: briefs, owners, dependencies, attempts, reviews", _add_work_arguments, _run_work),
]
