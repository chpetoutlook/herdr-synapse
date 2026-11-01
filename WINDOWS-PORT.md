# herdr-synapse on Windows (local port, 2026-10-20)

This checkout is a **local Windows port** of `vitalysim/herdr-synapse` v0.23.0.
Nothing was pushed upstream: the changes live as uncommitted working-tree
edits only.

The unix code path is intact — every platform difference is a branch on
`os.name == "nt"` inside the module that needs it, and the unix launchers
(`bin/herdr-synapse`, `bin/hook`, `console.sh`) are untouched.

## What changed

| Area | Unix | Windows |
|---|---|---|
| Launcher | `bin/herdr-synapse` (sh) | `bin/herdr-synapse.cmd` (+ `.ps1`) |
| Event gate | `bin/hook` (sh) | `bin/hook.cmd` → `bin/hook.ps1` |
| Console pane | `console.sh` | `console.cmd` |
| Server transport | `AF_UNIX` socket | Win32 named pipe `\\.\pipe\<socket path>` |
| Daemonization | double `fork` + `setsid` | detached `subprocess` + status file |
| Locking | `fcntl.flock` | `msvcrt.locking` (one byte) |
| Stop signal | `SIGTERM` | stop-request file the daemon polls (+ `taskkill` fallback) |
| Daemon identity | `ps -o lstart=` | process creation time, `YYYY-MM-DDTHH:MM:SSZ` (UTC) |
| Config dir | `$XDG_CONFIG_HOME` / `~/.config` | `%APPDATA%\<app>` |
| State dir | `$XDG_STATE_HOME` / `~/.local/state` | `%LOCALAPPDATA%\<app>\plugins\herdr-synapse` |
| Home | `$HOME` | `$HOME`, else `$USERPROFILE` |

The shared shims live in `herdr_team/platform_compat.py`.

## The named pipe

Herdr's Windows server listens on a named pipe whose name is the socket path
**verbatim**, prefixed with `\\.\pipe\`:

```
C:\Users\LOU\AppData\Roaming\herdr\herdr.sock
  -> \\.\pipe\C:\Users\LOU\AppData\Roaming\herdr\herdr.sock
```

The pipe speaks the same line-JSON protocol the unix socket spoke. A pipe
endpoint has **no filesystem presence**, so `Path.exists()` is meaningless for
it and is never used as an existence test.

The channel is opened with the raw Win32 API (`CreateFileW` + `WriteFile` /
`ReadFile`) rather than Python's `open()`. The CRT's `_read`/`_write` pair
refuses a write once a read has happened on the same handle (`EINVAL`), and
the daemon keeps a subscription handle open; the raw API does not.

## The vendored curses

The TUI panes (console, picker, compose, asks, usage, knowledge, mission) are
curses programs. Windows has no curses in the standard library, so
`windows-curses` is vendored into `vendor/win_curses/` (a local, untracked
build artefact) and the launcher puts that directory on `PYTHONPATH`. Install
it with:

```sh
python -m pip install --no-deps --target vendor/win_curses windows-curses
```

The launcher only adds the directory when it exists, so a checkout without it
still runs every non-curses command.

## Manifest

`herdr-plugin.toml` now declares `platforms = ["macos", "linux", "windows"]`.
The unix commands are kept verbatim and carry `platforms = ["macos", "linux"]`;
every entry has a Windows twin with `platforms = ["windows"]` that resolves the
plugin root first (the command's cwd is not reliable on Windows) and then
calls the launcher under that root.

Herdr refuses two actions or two panes with the same id
(`duplicate_plugin_action_id`, `duplicate_plugin_pane_id`), so the Windows
entrypoints carry a `-windows` suffix: the action is `team-up-windows` and the
pane is `picker-windows`. `herdr_team/paths.py:entrypoint_id()` and
`entrypoint_matches()` keep the plugin's own code in step with that.

The untouched unix manifest is `herdr-plugin.toml.orig`.

## Running it on Windows

```sh
herdr plugin link "C:\Users\LOU\AppData\Local\Programs\herdr-synapse"
herdr plugin enable herdr-synapse
herdr plugin action invoke herdr-synapse.team-up-windows   # the team-up popup
herdr plugin action invoke herdr-synapse.console-windows   # the team console
```

The CLI itself:

```sh
bin\herdr-synapse.cmd daemon start
bin\herdr-synapse.cmd daemon status
bin\herdr-synapse.cmd doctor --json
```

`HERDR_TEAM_PYTHON` overrides the interpreter; otherwise the launcher tries
`py -3` and then `python`, and refuses anything older than 3.9.

## Known gaps

* **The authority boundary is an environment fact on Windows, not a kernel
  fact.** Upstream decides "which pane is this process in?" by the process
  tree, which cannot be unset. Windows has no `ps`, and a Windows process
  table *misattributes* (the agent server is shared across panes, so the
  tree leads to a different pane's agent), so the port uses the **harness
  session id** instead: Herdr reports each pane's `agent_session.value`,
  and the harness puts the same id in the environment of every command it
  runs. A member that strips `HERDR_PANE_ID` is rerouted to its member
  rather than trusted as the operator. **The bound:** no environment signal
  is unforgeable, so this raises the bar from "clear one variable" to
  "clear two" — it stops the accidental case, not the class. The structural
  equivalent would be the console/window handle, which Herdr would have to
  report per pane. See §8a of the port README for the measurements.
* The **shell-ancestry verification** still stays unverified on Windows
  (the author is marked `cli-unverified`), because Herdr reports no
  foreground process group there. A human in a plain shell pane is
  therefore a *human* but not a *verified* one; the operator's own path is
  the console, a focused pane, or the outside tier.
* The console pane's `SIGTERM` handler never fires on Windows; herdr closes
  the pane instead.
* The hook's fast path costs a PowerShell start (~0.9 s) instead of the unix
  ~10 ms; it still avoids the Python reconciler.
* A **symlink needs a privilege** on Windows (developer mode, or an
  administrator token) and this machine has none, so every symlink the
  product makes is refused. The refusal is now the CLI's own error -
  `symlink_unavailable`, exit 1, with the platform's reason - and not a raw
  `WinError 1314` traceback: `herdr_team/platform_compat.py` owns the call
  (`symlink_or_raise`), and the install path writes a `.cmd` shim on Windows
  instead of a symlink, so nothing the port ships needs the privilege.
