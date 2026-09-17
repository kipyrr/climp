# Where this project is up to

Last updated 2026-09-16, end of the first working session.

Read this first, then `IMPLEMENTATION-PLAN.md`. The plan is the roadmap and
records every decision with its reasoning; this file is just the bookmark.

## Done

| Phase | Status |
|---|---|
| 0 — Spike | Complete. Gate met: a hand-picked clip is in Drive. |
| 1 — Walking skeleton | Complete. Watcher and settle checker, verified against a simulated recording. |
| 2 — Durability | Complete. Gate met 2026-09-16 after it first failed silently — see below. |
| 3 — Livable | Not started. Next up. |
| 4 — Hardening | Not started. |
| 5 — Polish | Not started. |

All five blueprint components exist except the tray:
`watcher.py`, `settle.py`, `reconciler.py`, `uploader.py`, plus `db.py`,
`drive.py`, `config.py` and `main.py`. 83 tests passing.

## What the Phase 2 gate found

The gate passed, but only on the second attempt, and the first attempt is the
interesting part.

Killing the app mid-upload and restarting produced a `done` row and a
byte-exact file in Drive. That looked like a pass. It was not. The "resume" had
restarted from byte zero and re-sent all 120 MB under the old session — the
file was right because everything went up twice.

Setting `resumable_uri` on a rebuilt request is not enough: its progress
counter is zero and the client library never asks the server for the real
offset. Fixed with `DriveClient.received_bytes()`, which queries it explicitly.

Proof after the fix: resumed at byte 58,458,112, which is not a multiple of the
16 MB chunk size and so could only have come from the server, and the second
leg took 27.1s against 40.2s for a full re-upload.

`tools/gate_test.py` reruns this end to end and cleans up after itself.

## Facts established by measurement, not assumption

| | |
|---|---|
| Clips folder | `C:\Users\adria\Videos\NVIDIA`, nested per game |
| Library at pause | 49 clips, 10.75 GB, average 225 MB |
| Recording rate | ~24 clips/month |
| ShadowPlay write speed | 181 MB in about 1 second (buffer dump, not a slow flush) |
| Raw watchdog events per real clip | 3, debounced to 1 |
| Drive free | 13.98 GB of 15 GB, ~62 clips of headroom |
| Drive folder ID | `1B3-qC7-aXJvh-H7jOrY-UsbP3yCrgnWa` |
| SQLite | 3.50.4, so `RETURNING` is available |
| Python | 3.13.15, venv at `.venv/` |

## Machine-specific state, deliberately outside this repo

`%LOCALAPPDATA%\ClipSync\` holds `client_secret.json`, `token.json`,
`config.toml` and `clips.db`. None of it is in git, and none of it should be.

`config.toml` carries `backfill_since`, pinned to first launch. That is what
keeps the existing 49 clips local — only clips recorded after that moment are
eligible. Lower the value to pull older ones in.

## Decisions settled so far

D1–D16 are in section 7 of `IMPLEMENTATION-PLAN.md` with the reasoning for each.
The four that most shape the code:

- **D4** — the retry button resets to `candidate`, not `ready`, keeping the session URI.
- **D13** — OAuth stays in Testing mode, so the token dies weekly and `drive.py` owns a re-auth path. Reversible in seconds once the repo has a public URL.
- **D15** — the `backfill_since` gate lives in the settle checker, not the watcher. Forced by a real watcher run: browsing the clips folder in Explorer fires `modified` events for old clips.
- **D16** — `path_key` is a separate normalised column; `path` keeps its original spelling.

## Picking it back up

Next task is Phase 3: `tray.py`, the quota display, structured logging, launch
at login, and DPAPI encryption for the token, which currently sits in plaintext.
