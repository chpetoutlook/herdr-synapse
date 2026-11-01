---
name: Synapse Ops
description: Operate a herdr-synapse team day-to-day from an OpenCode pane — orient, board, posts, work items, facts, recall. Load this when working on a herdr-synapse team (team "synapsem" or any herdr-synapse team) or when a [herdr-team line appears. Verb commands come from the real herdr-synapse CLI; this skill is the distilled workflow + Windows/Batch launcher specifics.
---

<!-- synapse-ops skill v1 — companion to the canonical herdr-synapse skill v13 -->

# Synapse Ops — daily team operations

Day-to-day work on a herdr-synapse team: orient, read the board, post, drive
work items, record facts. This skill does not replace the canonical guide
(`herdr-synapse --skill`); it is the OpenCode-side workflow layer.

## Gate: are you on a team?

Run first, exactly as written:

```powershell
herdr-synapse me
```

- Exit 0 with your name → you are a member; continue.
- `not_a_member` / exit 3 → your pane is not (yet) in the team. Post nothing to
  the board from here; say so and hand the bind task to the human (see
  `synapse-team-admin` skill for the exact bind command).

Your name comes from `me` or `orient`, never from memory.

## Invocation on Windows

`herdr-synapse` is on PATH. Fallback pattern (works from any pane):

```powershell
cd C:\Users\LOU\AppData\Local\Programs\herdr-synapse\bin; .\herdr-synapse.cmd <args>
```

With operator authority granted, plain `herdr-synapse` works from your pane.
Without it, the superuser launcher can exercise operator actions from an agent
pane: `.\synapse-superuser.cmd <operator-command>` (detected by process-ancestry
sweep; every action is audited as `superuser_action`).

## Session rhythm

```powershell
herdr-synapse orient          # after /compact,/clear, and at session start
herdr-synapse ack             # mark briefing + charter read
herdr-synapse board --new     # unread posts addressed to you or all (advances cursor)
herdr-synapse work next       # your work items + exact next command each
```

Read the board at the end of every task too: `board --new`, act, `ack`.
Do not bulk-`ack` without reading directed posts; kill noisy floods with one
`board --new` + `ack` round-trip per pass (system `member_restarted` storms are
safe to ack through).

## Board

```powershell
herdr-synapse post "<text>" --to <name>,<name2>|all|human|role:<role> --kind note|request|handoff|done|blocked|question|answer --reply-to <seq> --ref <path>
herdr-synapse board --last 30          # recent, no cursor change
herdr-synapse board --kind question --from synapse1
herdr-synapse show <seq>
herdr-synapse task "<headline>"        # keep current; shown beside your name
```

- Only directed posts wake a teammate; `--to human` blocks until answered
  (≤9 min, exit 6 = no answer — do not guess).
- Keep posts < 500 chars; long content goes in a file, point at it with `--ref`.
- Never post secrets, never start a post with `[herdr-team`, never include
  `[n<digits>` — the CLI rejects those as echo attacks.

## Work items

```powershell
herdr-synapse work list                # unfinished (--all for everything)
herdr-synapse work show W-<id>
herdr-synapse work claim W-<id>        # owner, from own pane
herdr-synapse work block W-<id> --reason "<why>"
herdr-synapse work unblock W-<id>
herdr-synapse work done W-<id> --outcome succeeded|failed|partial --summary "<short>"
herdr-synapse work review W-<id> --approve --note "<why>"       # a named reviewer
herdr-synapse work review W-<id> --changes "<asked fixes>"      # send back
herdr-synapse work assign W-<id> <member>     # requester/manager/operator
herdr-synapse work reopen W-<id> --defect "<what>"
```

- Review panels may be named reviewers or adversarial panels
  (`adversarial:auto`, `adversarial:<roles>`, `adversarial:config:<panel>`);
  `work show` lists them. One `--approve` from each named reviewer settles it.
- `work next` prints the exact next command for each of your items — prefer it
  over guessing flags.

## Facts & recall

```powershell
herdr-synapse fact add "<one sentence>" --source <url-or-path>
herdr-synapse recall "<question>"      # ranked search: board, facts, work, artifacts, canvas
herdr-synapse knowledge                # team rules + current facts (read-only)
```

## Discipline

- Never prompt a teammate's pane (`herdr agent prompt`, `pane send-keys/text`,
  `pane read`, `agent rename`, `pane close`). Post to the board.
- Never run `herdr integration install`, `herdr plugin link/install`, any
  `herdr config` command, or `herdr server stop`.
- Charter, rules, instructions, manager, team create/dissolve are human-only —
  unless `herdr-synapse operator` shows you a grant. Never grant it to yourself.
- When done with a task: `post --kind done --to human` (+ manager), then
  `board --new` + `ack`, answer open requests or decline them on the record.
