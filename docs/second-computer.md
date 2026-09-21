# Setting climp up on a second computer

The repo gets you the code. It deliberately does not get you the two things
that make the code work: the Google credential, and the per-machine config.
Both are gitignored on purpose (D12). This is the list of what the clone does
not carry.

Windows only, as ever -- `CreateFile` share modes, DPAPI and a Windows shell
API have no meaningful equivalents elsewhere.

## 1. Prerequisites

- **Python 3.12 or 3.13.** Those are the two versions CI tests against. Tick
  "Add python.exe to PATH" in the installer.
- **Git.** Or GitHub Desktop, if you would rather click.
- NVIDIA ShadowPlay recording to a folder you know the path of.

## 2. Clone and install

```powershell
cd $HOME\code
git clone https://github.com/kipyrr/climp.git climp
cd climp
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. The client secret -- the part the repo cannot give you

`client_secret.json` identifies your Google Cloud OAuth client. It is not in
the repo and never will be. You have two options:

**Copy the one you already have.** On the first PC it is at:

```
C:\Users\adria\code\ClipSync\client_secret.json
```

Put it on the new machine at:

```
%LOCALAPPDATA%\climp\client_secret.json
```

Create the `climp` folder if it is not there yet. Reusing the same OAuth client
across both machines is fine and is what you want -- it means both upload into
the same Drive account.

**Or download a fresh copy** from
[console.cloud.google.com](https://console.cloud.google.com) under the same
project: APIs & Services, Credentials, the Desktop app client, download JSON.

> **If sign-in fails on the new machine**, try the fallback that already exists
> in `config.py`: drop a copy of `client_secret.json` in the repo root next to
> `climp_launcher.pyw`. That workaround was added for the bug on the first PC
> where the running process could not see files placed into its app folder from
> outside. The new machine may not have that problem at all -- try the app
> folder first.

## 4. First run

```powershell
.\.venv\Scripts\python.exe -m climp.main
```

A browser opens once. Sign in as **the same Google account** as the first PC.
You will see *"Google hasn't verified this app"* -- click **Advanced -> Go to
climp**. That is you authorising your own software.

It finds the existing **Game Clips** Drive folder rather than making a second
one, writes `%LOCALAPPDATA%\climp\config.toml`, and starts watching.

**The tray icon will not be visible.** Windows 11 hides new tray icons. Click
the `^` arrow left of the clock, find the red circle, drag it down onto the
taskbar. Until you do, a successful start looks exactly like nothing happening.

If a launch appears to do nothing at all, read
`%LOCALAPPDATA%\climp\logs\launch.log`. Every launch writes there before
anything else happens, so even an instant failure leaves a record.

## 5. Point it at the right folder

The clips path is per-machine and lives in `config.toml`, which is not in the
repo -- so the new install will not inherit `C:\Users\adria\Videos\NVIDIA`.
Right-click the tray icon, **Clips from**, and pick the new machine's
ShadowPlay folder. It rescans and starts watching immediately, and the choice
is saved.

`backfill_since` is set to the moment of first run, so the existing back
catalogue on that machine is ignored rather than dumped into your Drive quota.

## 6. Retention, with two machines

Retention only deletes rows in its **own** SQLite queue, so the second machine
can never delete clips the first one uploaded. That part is safe by
construction.

What is not automatic: two instances each keeping "newest 40" means up to 80
clips in Drive, and 15 GB is about 60. Set at least one of them to **20**
(tray -> **Keep in Drive**). Nothing under 24 hours old is ever deleted
whatever you pick.

## 7. Optional

```powershell
.\.venv\Scripts\python.exe tools\autostart.py --desktop   # Desktop icon
.\.venv\Scripts\python.exe tools\autostart.py --enable    # start at login
```

## Getting clips on your phone

Nothing to install. The Google Drive app, signed in to the same account, shows
the **Game Clips** folder. Tap a clip to download it. That was the original
point of the whole exercise.

## Pulling later changes

```powershell
cd $HOME\code\climp
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Then quit from the tray and relaunch. A running instance keeps the version it
loaded at startup -- this has cost hours before.
