# Where this project is up to

Last updated 2026-09-21, 13:30.

Read this first, then `IMPLEMENTATION-PLAN.md`. The plan is the roadmap and
records every decision with its reasoning; this file is the bookmark.

## The app

**climp** — watches `C:\Users\adria\Videos\NVIDIA`, waits until each clip has
finished writing, uploads it to the Google Drive folder "Game Clips", and sits
in the system tray. Renamed from ClipSync on 2026-09-16.

All six phases are complete. 186 tests passing.

| Phase | Status |
|---|---|
| 0 — Spike | Complete |
| 1 — Walking skeleton | Complete |
| 2 — Durability | Complete. Gate met, after it first passed for the wrong reason |
| 3 — Livable | Complete. Tray, logs, encrypted token, opt-in autostart |
| 4 — Hardening | Complete. Defer while gaming, retention, sleep detection |
| 5 — Polish | Complete. README, CI, pinned requirements |

## Open, and unexplained

**The running app cannot see three files in its own app folder.**

`%LOCALAPPDATA%\climp` contains eight entries. A climp process launched by Kip
lists only five: `clips.db`, `clips.db-shm`, `clips.db-wal`, `config.toml`,
`logs`. Invisible to it are `client_secret.json`, `token.bin` and
`spike_state.json` — exactly the three files placed there from outside rather
than created in place.

Same path. Same `os.path.realpath`. Same user account. Same build constant.
Identical ACLs after being recreated. Full control for the user. Defender
reports no ASR rules, no Controlled Folder Access and no blocks. Every launch
reproduced from a tool session — from the repo, from `C:\Windows\System32`,
under `python` and `pythonw`, headless and with the tray — sees all eight.

Ruled out: hidden tray icon, ghost icon, stale bytecode, stale process, wrong
working directory, permissions, path redirection, and a second app folder (a
full-disk search found exactly one).

**The workaround in place:** `Config.client_secret_path` looks in the app
folder first and then beside the climp package. A copy of `client_secret.json`
lives at `C:\Users\adria\code\ClipSync\client_secret.json` — gitignored, but
**do not delete it**; sign-in depends on it on this machine.

**Not verified:** after the workaround Kip reported sign-in working, but
`token.bin` still showed the copy placed there externally, not one written by
his own process. The sign-in may therefore not survive a restart.
**First thing to check next session:** open the tray's Diagnostics and see
whether `token` says yes. If not, give the token the same two-location
treatment the client secret now has.

## Changes on the evening of 2026-09-16

- Renamed everything to **climp**, including the app-data folder, which
  migrates itself from the old `ClipSync` name.
- Icon is a **solid red circle** that pulses red to black and back on a 3s
  cosine fade while work can progress. It does not pulse while paused (D25).
- **Clips folder picker** in the tray (D22). "Source file" was interpreted as
  the source folder, since the app watches a directory, not a file.
- **`climp_launcher.pyw`** replaces `-m climp.main` in both shortcuts. The
  module form resolved the package against the working directory, which
  Explorer did not supply, producing a silent `ModuleNotFoundError` with no
  console, no log and no dialog (D24).
- **Single-instance guard** (D23), with a topmost dialog.
- **A credential problem no longer stops startup.** The queue runs and only
  uploading waits, deferring rather than failing so no retry budget is burnt.
- **Diagnostics submenu** in the tray: build, interpreter, cwd, resolved app
  folder, its contents, and what the process can actually see. Built because an
  instance that cannot write its log cannot be diagnosed through its log.
- **`climp.BUILD`** is a literal constant. An earlier marker computed source
  mtimes at runtime, which a stale process reports identically to a fresh one,
  so it could never have detected what it was added to detect.

## Facts established by measurement

| | |
|---|---|
| Clips folder | `C:\Users\adria\Videos\NVIDIA`, nested per game |
| Library | 56 clips, about 11 GB, average 225 MB |
| ShadowPlay write speed | 181 MB in roughly one second |
| Detection to in-Drive | about 95 seconds for a 225 MB clip |
| Raw watchdog events per clip | 3, debounced to 1 |
| Drive | 13.2 GB free of 15 GB, 5 clips uploaded |
| Retention | on, keep newest 40, 24h floor |
| Resume after a real timeout | resumed at byte 184,549,376 rather than restarting |

## Running it

Double-click **climp launcher.bat** on the Desktop. There is also a
`climp.lnk`; both run the launcher now. `tools/autostart.py --enable` starts it
at login.

Tray menu: queue summary, build and start time, sign-in state, quota expressed
in clips, failed clips with per-clip retry, **Clips from**, **Keep in Drive**,
**Diagnostics**, Open Drive folder, Open log file, Quit.

Logs: `%LOCALAPPDATA%\climp\logs\climp.log` and `launch.log`.

**After any code change, quit from the tray and relaunch.** A running instance
keeps the version it loaded at startup; several confusing hours came from
testing fixes against an instance that predated them.

## Published, 2026-09-21

The repo is public at **https://github.com/kipyrr/climp**, all 30 commits on
`main`. CI ran green on the first push (Windows, Python 3.12 and 3.13).

Checked before publishing: the git index carried 43 files and none of them were
secrets, and `git log --all --name-only` across the whole history matched no
`client_secret`, `token`, `*.bin`, `config.toml` or `*.db`. The repo-root copy
of `client_secret.json` is untracked and stayed local. **Do not delete it** --
see the open item above.

The README's CI badge and clone URL now point at `kipyrr/climp` rather than the
`OWNER` placeholder.

## Picking it back up

1. Check Diagnostics. Does `token` say yes? If not, see the open item above.
2. Set up the second computer. See `docs/second-computer.md`.
3. Now that the repo is public, move the Google OAuth consent screen from
   *Testing* to *In production* (D13). It asks for a homepage and a privacy
   policy URL; the repo URL serves as the homepage. This ends the weekly
   re-sign-in, and `drive.file` alone triggers no verification review.
4. Retention is per-instance -- it only deletes rows in its own queue, so two
   machines can never delete each other's clips. But two instances both keeping
   the newest 40 means up to 80 clips in a 15 GB Drive. Drop one to 20.
5. D1 to D25 are in section 7 of `IMPLEMENTATION-PLAN.md`, each with the
   reasoning behind it.
