"""A fake ShadowPlay, so the settle logic can be tested without booting a game.

Reproduces the three phases of a real recording:

  1. The file appears immediately and grows over time.
  2. Growth stops -- but the handle is still held. A size-only check would
     wrongly call the file finished here. This is the case that matters.
  3. The handle is released. Only now is the file safe to upload.

The handle is opened with FILE_SHARE_READ, which denies write sharing. That is
what makes an exclusive open fail while the "recording" is in progress, exactly
as NVIDIA's does. Python's builtin open() shares freely and would not reproduce
this, which is why win32file is used directly.

Usage:
    python tools/simulate_clip.py "C:\\tmp\\clips\\Test\\clip.mp4" --mb 12 --hold 8
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import win32file

MB = 1024 * 1024


def simulate(
    path: Path,
    total_mb: int,
    chunk_mb: int,
    grow_delay: float,
    hold_seconds: float,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    handle = win32file.CreateFile(
        str(path),
        win32file.GENERIC_WRITE,
        win32file.FILE_SHARE_READ,  # deny write sharing: a reader cannot take an exclusive handle
        None,
        win32file.CREATE_ALWAYS,
        win32file.FILE_ATTRIBUTE_NORMAL,
        None,
    )
    try:
        chunk = b"\0" * (chunk_mb * MB)
        written = 0
        target = total_mb * MB
        print(f"[sim] created {path.name}, growing to {total_mb} MB")
        while written < target:
            win32file.WriteFile(handle, chunk)
            win32file.FlushFileBuffers(handle)
            written += len(chunk)
            print(f"[sim]   {written // MB} MB")
            time.sleep(grow_delay)

        print(f"[sim] growth finished; HOLDING handle for {hold_seconds}s")
        print("[sim] (size is now stable but the file is NOT safe -- promoting here would be the bug)")
        time.sleep(hold_seconds)
    finally:
        win32file.CloseHandle(handle)
        print("[sim] handle released; file is now genuinely finished")


def main() -> None:
    ap = argparse.ArgumentParser(description="Write a file the way a capture tool does.")
    ap.add_argument("path", type=Path)
    ap.add_argument("--mb", type=int, default=12, help="Final size in MB.")
    ap.add_argument("--chunk-mb", type=int, default=2, help="MB per write.")
    ap.add_argument("--grow-delay", type=float, default=1.0, help="Seconds between writes.")
    ap.add_argument("--hold", type=float, default=8.0, help="Seconds to hold the handle after growth stops.")
    args = ap.parse_args()

    simulate(args.path, args.mb, args.chunk_mb, args.grow_delay, args.hold)


if __name__ == "__main__":
    main()
