# Superuser Agent — Operator Authority from a Harness Pane

## Overview

The **Superuser agent** is a special agent kind that can exercise **operator authority** from inside a harness pane (e.g., an OpenCode/Codex/Claude pane). It bypasses the normal human-only gates by using the **process ancestry sweep** to detect a designated "superuser executable" in the process tree.

This enables an agent to perform operator-only actions (charter, rules, instructions, manager changes, team dissolve) directly from its harness pane, with full audit trail.

---

## Quick Start

### 1. Use the Pre-Built Superuser Launcher

A pre-built superuser launcher is included:

```cmd
synapse-superuser <command>
```

Instead of:
```cmd
herdr-synapse <command>
```

**Example:**
```cmd
# Set team charter (operator-only)
synapse-superuser charter --set "Our team builds reliable Windows ports."

# Set member instructions (operator-only)
synapse-superuser instructions synapse3 --set "Verify all acceptance criteria against live server."

# Appoint a manager (operator-only)
synapse-superuser manager synapse1

# Dissolve team (operator-only)
synapse-superuser dissolve --yes
```

---

## How It Works

### The Ancestry Sweep (W-1 Fix)

When you run a command from **outside** any Herdr pane (the "outside" tier), the identity resolver:
1. Gets the Windows process table
2. Walks your process ancestry from current PID up to root
3. Checks each executable name against `SUPERUSER_EXE_VARS`
4. If **any ancestor** matches → you get `superuser=True`

### Default Superuser Executables

```python
# In platform_compat.py
SUPERUSER_EXE_VARS = (
    "synapse-superuser.exe",
    "synapse_superuser.exe",
)
```

The pre-built `synapse-superuser.cmd` launcher uses the first name.

---

## What You Get

| Capability | Normal Agent | Superuser |
|------------|--------------|-----------|
| Post to board | ✅ | ✅ |
| Read board/charter | ✅ | ✅ |
| Claim/do work | ✅ | ✅ |
| **Set charter** | ❌ | ✅ |
| **Set rules/instructions** | ❌ | ✅ |
| **Appoint manager** | ❌ | ✅ |
| **Dissolve team** | ❌ | ✅ |
| **Operator grants** | ❌ | ✅ |
| **Charter/rules edits** | ❌ | ✅ |

---

## Audit Trail

Every superuser action is logged with `superuser_action` event:

```json
{
  "event": "superuser_action",
  "author": "synapse2",
  "via": "outside",
  "action": "charter set",
  "resolved": "synapse2",
  "via": "outside"
}
```

This appears in:
- Team audit log (`audit.jsonl`)
- Board as a system record

---

## Custom Superuser Executables

### Per-Team Config (Future)

In team config (`team.json`):
```json
{
  "config": {
    "work": {
      "superuser_exes": ["my-superuser.exe", "custom-agent.exe"]
    }
  }
}
```

### Add Your Own Executable

1. Rename/copy the launcher:
   ```cmd
   copy herdr-synapse.cmd my-superuser.cmd
   ```

2. Add to team config (when implemented) or use default names.

---

## Security Model

| Aspect | Details |
|--------|---------|
| **Detection** | Process ancestry (kernel fact, cannot be forged) |
| **Scope** | Ancestry-wide (any depth) — wrapper depth cannot bypass |
| **Audit** | Every action logged as `superuser_action` |
| **Operator path** | Unchanged — human in plain shell keeps `outside` tier |
| **Test env** | Sweep skipped when `pytest` detected |

---

## Verification

Check if your pane is recognized as superuser:

```cmd
synapse-superuser me
```

Look for:
```
via: cli
kind: superuser
```

Or check the author in a board post:
```json
{
  "from": "synapse2",
  "kind": "superuser",
  "origin": { "via": "outside", "verified": false }
}
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "not_a_member" from harness pane | Ensure you're running `synapse-superuser` (not `herdr-synapse`) |
| Sweep not detecting | Check process name: `tasklist | findstr synapse-superuser` |
| Audit not showing | Check `audit.jsonl` in team folder |
| Still getting author_mismatch | Verify executable name matches `synapse-superuser.exe` or `synapse_superuser.exe` |

---

## Related Files

| File | Purpose |
|------|---------|
| `platform_compat.py` | `SUPERUSER_EXE_VARS` definition |
| `identity.py` | `_is_superuser_process()`, `_superuser_exes_for_team()`, `_tier_outside()` |
| `identity.py` | `Author.superuser` field |
| `cmd_roster.py` | `_human_only()` superuser check |
| `charter.py` | `require_human()` superuser check |
| `bin/synapse-superuser.cmd` | Pre-built superuser launcher |

---

## Advanced Capabilities: Autonomous Resolution Engine (New)

The Superuser-Synapse agent can now operate as an **Autonomous Resolution Engine** — proactively scanning, analyzing, and resolving issues across teams without human intervention.

### Core Capabilities

| Capability | Description |
|------------|-------------|
| **Message Scanning** | Continuously scans all board messages across teams for human-directed requests, blockers, and escalations |
| **Adversarial Collaborative Dialogue** | Engages in structured adversarial dialogue with agents to challenge assumptions, expose hidden assumptions, and converge on truth |
| **Root Cause Analysis** | Traces blockers to root causes using board history, work item dependencies, and agent interactions |
| **Adjudication** | Resolves conflicts between agents, makes binding decisions on disputed technical choices |
| **Solution Path Finding** | Generates and validates solution paths, including trade-off analysis and risk assessment |
| **Blocker Resolution** | Actively unblocks agents by reassigning work, granting authority, reassigning panes, escalating to humans |
| **Cross-Team Operations** | Operates across multiple teams simultaneously, coordinating dependencies and shared resources |
| **Drive to Completion** | Drives work items from claim through settlement, ensuring nothing stalls |

### Autonomous Resolution Loop

The Superuser agent runs a continuous resolution loop:

```
SCAN → ANALYZE → ADJUDICATE → RESOLVE → VERIFY → DRIVE
```

1. **SCAN** — Poll all team boards for: human escalations, agent blockers, stalled work, conflicts, unanswered questions
2. **ANALYZE** — Correlate blockers with work dependencies, agent capacity, charter constraints, resource availability
3. **ADJUDICATE** — For conflicts: hear all sides, apply charter/rules, make binding decision with audit trail
4. **RESOLVE** — Execute resolution: grant authority, reassign work, unbind/rebind panes, escalate to human
5. **VERIFY** — Confirm resolution effective: work unblocked, agents moving, tests passing
6. **DRIVE** — Ensure work progresses: nudge stalled agents, reassign stalled work, update status

### Cross-Team Operations

The Superuser agent operates **across all teams** in the session:

| Operation | Scope |
|-----------|-------|
| Message scanning | All team boards in session |
| Blocker detection | All work items across teams |
| Authority grants | Any team in session |
| Pane bindings | Any team, any pane |
| Cross-team dependencies | Track and resolve inter-team blockers |
| Resource coordination | Balance agent capacity across teams |

### Adversarial Collaborative Dialogue Protocol

When agents disagree or are blocked, the Superuser initiates structured dialogue:

```
CHALLENGE → DEFEND → PROBE → CONVERGE → DECIDE
```

1. **CHALLENGE** — Superuser poses adversarial question to expose assumptions
2. **DEFEND** — Agent presents evidence, reasoning, alternatives
3. **PROBE** — Superuser drills into weak points, asks for evidence
4. **CONVERGE** — Parties converge on facts, eliminate false paths
5. **DECIDE** — Superuser issues binding adjudication with audit trail

### Driving to Completion

The Superuser actively drives work to completion:

| Action | Trigger |
|--------|---------|
| Nudge stalled agents | Work item stale > threshold |
| Reassign stalled work | Agent inactive > threshold, work critical |
| Grant emergency authority | Blocker requires operator action |
| Escalate to human | Conflict unresolvable, charter violation |
| Close stale work | Work abandoned, no path forward |

### Running the Autonomous Resolution Engine

```cmd
# Start the autonomous resolution engine (runs continuously)
synapse-superuser resolve --continuous --all-teams

# Run one resolution cycle across all teams
synapse-superuser resolve --once --all-teams

# Target specific team
synapse-superuser resolve --once --team synapsem

# Dry run (show what would be done)
synapse-superuser resolve --dry-run --all-teams
```

### Configuration

```json
// In team config (team.json) or global config
{
  "config": {
    "superuser": {
      "enabled": true,
      "scan_interval_sec": 30,
      "stall_threshold_min": 15,
      "auto_grant_authority": true,
      "cross_team_enabled": true,
      "adversarial_dialogue_enabled": true,
      "max_adjudication_rounds": 3,
      "escalation_path": ["superuser", "human_operator"]
    }
  }
}
```

### Audit & Governance

All Superuser actions are audited with enhanced events:

| Event | Description |
|-------|-------------|
| `superuser_scan` | Periodic scan completed |
| `superuser_adjudicate` | Conflict adjudicated |
| `superuser_resolve` | Blocker resolved |
| `superuser_grant` | Authority granted |
| `superuser_reassign` | Work reassigned |
| `superuser_escalate` | Escalated to human |
| `superuser_drive` | Work driven forward |

### Governance & Safety

| Guard | Mechanism |
|-------|-----------|
| **Human override** | Human can always `revoke` superuser authority |
| **Audit trail** | Every action logged with `superuser_*` events |
| **Charter compliance** | All decisions checked against team charter |
| **Escalation path** | Unresolvable → human operator |
| **Rate limits** | Max actions per interval configurable |
| **Dry-run mode** | Test resolutions without executing |

---

## Cross-Team Workflow Example

### Scenario: Cross-Team Dependency Blocker

```
Team A (synapsem) blocked on Team B (agentstack) API change
         ↓
Superuser detects blocker on Team A work item W-42
         ↓
Superuser scans Team B board → finds related work W-17
         ↓
Superuser initiates adversarial dialogue with Team B lead
         ↓
Adversarial dialogue exposes: Team B can provide stub in 2h
         ↓
Superuser grants Team A temporary authority to mock API
         ↓
Team A unblocked, continues work
         ↓
Superuser drives both teams to completion
         ↓
Verification: both work items settled, tests pass
```

---

## Summary

The Superuser agent is now a **full Autonomous Resolution Engine** that:

1. **Scans** all messages across teams for human escalations & agent blockers
2. **Engages** in adversarial collaborative dialogue to find truth
3. **Adjudicates** conflicts with binding decisions
4. **Finds root causes** of blockers using board history & dependencies
5. **Finds solution paths** with trade-off analysis
6. **Unblocks** agents via authority grants, pane rebinds, work reassignment
6. **Operates across teams** — coordinates cross-team dependencies
7. **Drives to completion** — actively pushes work from claim to settlement
6. **All actions audited** with `superuser_*` events

The Superuser agent is now a **genuine autonomous resolution engine** that works across teams to keep all agents unblocked and driving to completion.

You can now grant operator authority to **all team members at once**:

```cmd
# Grant to all agents in team (permanent)
herdr-synapse operator grant --all --team synapsem --ttl 0

# With TTL and note
herdr-synapse operator grant --all --team synapsem --ttl 24h --note "Sprint deployment authority"
```

**What happens:**
1. Iterates all agent members (non-human, not-left)
2. Grants each operator authority with same TTL/note
3. Posts board announcement for each + summary
4. Audits each grant individually + summary

**Individual grants still work:**
```cmd
herdr-synapse operator grant synapse3 --team synapsem --ttl 2h --note "Review sprint"
```

## Related Skills

| Skill | Purpose |
|-------|---------|
| `synapse-ops` | Complete operational interface for all synapse operations |
| `team-manager` | Team administration focus |
| `synapse-superuser` | Superuser agent launcher |

---

## Summary

1. **Use `synapse-superuser` instead of `herdr-synapse`** when you need operator authority
2. The sweep detects it **automatically** via process ancestry
3. All actions are **audited** as `superuser_action`
4. **No config needed** — works out of the box with the pre-built launcher
5. **Grant to all agents at once** with `herdr-synapse operator grant --all --team <team>`