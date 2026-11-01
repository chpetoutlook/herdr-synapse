---
name: team-manager
description: "Manage herdr-synapse teams: bind/unbind panes, grant operator authority, manage membership. Use when you need to administer a herdr-synapse team."
---

<!-- herdr-synapse team-manager skill v1, cli >= 0.23 -->

# Team Manager Skill

Manage herdr-synapse teams: bind panes to members, grant/revoke operator authority, manage team membership, and handle pane bindings.

## Prerequisites

- You are a member of a herdr-synapse team (run `herdr-synapse me` to verify)
- For operator actions: you must have operator authority (via `operator grant`, superuser agent, or be the human operator)

---

## Commands

### Pane Binding

```bash
# Bind a pane to a team member
team-manager bind --team <team> --member <name> --pane <pane_id>

# Unbind a member from their current pane
team-manager unbind --team <team> --member <name>

# Show current pane bindings
team-manager bindings --team <team>
```

### Operator Authority

```bash
# Grant operator authority to a member (or all members)
team-manager grant --team <team> --member <name> [--ttl <duration>] [--note <text>]
team-manager grant --all --team <team> [--ttl <duration>] [--note <text>]

# Revoke operator authority
team-manager revoke --team <team> --member <name>

# List current grants
team-manager grants --team <team>
```

### Team Membership

```bash
# Add a member to the team
team-manager add --team <team> --member <name> --kind <kind> [--role <role>] [--brief <text>]

# Remove a member from the team
team-manager remove --team <team> --member <name>

# List team members
team-manager members --team <team>
```

### Team Info

```bash
# Show team overview
team-manager info --team <team>

# Show team charter
team-manager charter --team <team>

# Show team rules
team-manager rules --team <team>
```

---

## Examples

### Bind a member to a new pane
```bash
team-manager bind --team synapsem --member synapse4 --pane wF:pG
```

### Grant operator authority to all team members
```bash
team-manager grant --all --team synapsem --ttl 0 --note "Permanent team operator authority"
```

### Grant operator authority to one member
```bash
team-manager grant --team synapsem --member synapse3 --ttl 2h --note "Sprint review authority"
```

### Revoke operator authority
```bash
team-manager revoke --team synapsem --member synapse3
```

### Move a member to a new pane
```bash
# First unbind from old pane (or remove and re-add)
team-manager unbind --team synapsem --member synapse4
team-manager bind --team synapsem --member synapse4 --pane wF:pG
```

### Add a new team member
```bash
team-manager add --team synapsem --member new-agent --kind opencode --role worker --brief "Build and test features"
```

---

## Common Workflows

### Move an agent to a new pane
```bash
# 1. Unbind from current pane
team-manager unbind --team synapsem --member synapse4

# 2. Bind to new pane
team-manager bind --team synapsem --member synapse4 --pane wF:pG
```

### Grant full team operator authority
```bash
team-manager grant --all --team synapsem --ttl 0 --note "Full team operator authority"
```

### Set up a new team member with operator authority
```bash
# 1. Add member
team-manager add --team synapsem --member new-agent --kind opencode --role worker --brief "Build features"

# 2. Bind to pane
team-manager bind --team synapsem --member new-agent --pane wF:pX

# 3. Grant operator authority
team-manager grant --team synapsem --member new-agent --ttl 24h --note "Full operator authority"
```

---

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `agent_name_taken` | Member already bound to another pane | Unbind first: `unbind --member <name>` |
| `not_a_member` | Pane not in team roster | Bind pane first: `bind --member <name> --pane <pane_id>` |
| `author_mismatch` | Not operator/human | Run from human pane or get operator grant |
| `member_not_found` | Member not in team | Add member first: `add --member <name>` |

---

## Quick Reference

| Command | Description |
|---------|-------------|
| `bind` | Bind pane to member |
| `unbind` | Unbind member from pane |
| `grant` | Grant operator authority |
| `revoke` | Revoke operator authority |
| `add` | Add member to team |
| `remove` | Remove member from team |
| `members` | List team members |
| `grants` | List operator grants |
| `bindings` | Show pane bindings |
| `info` | Show team overview |
| `charter` | Show team charter |
| `rules` | Show team rules |

---

## Implementation

This skill wraps the following herdr-synapse commands:
- `herdr-synapse bind / unbind`
- `herdr-synapse operator grant / revoke / list`
- `herdr-synapse create / remove / add / remove`
- `herdr-synapse who / charter / knowledge`

The skill provides a unified interface with consistent flags and better error messages.