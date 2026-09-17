"""Phase 1 step 1 — watch a folder and print what the watcher notices.

The plan's instruction for this step is literally "watcher.py printing paths.
Clip in-game, confirm you get an event and see how many." The event count is
the point: it is how roadblock 9 stops being theoretical.

    python run_watcher.py --root "C:\\Users\\adria\\Videos\\NVIDIA"
    python run_watcher.py --root C:\\tmp\\clips --seconds 30
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from climp.watcher import Watcher


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ["USERPROFILE"]) / "Videos" / "NVIDIA",
        help="Folder to watch, recursively.",
    )
    ap.add_argument("--seconds", type=float, help="Stop after N seconds. Omit to run until Ctrl-C.")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show debounced events too.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    seen: list[Path] = []
    watcher = Watcher(args.root, on_candidate=seen.append)
    watcher.start()

    started = time.monotonic()
    try:
        while args.seconds is None or (time.monotonic() - started) < args.seconds:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()

    print("\n--- candidates (one per path, after debounce) ---")
    for p in seen:
        print(f"  {p}")
    print(f"\n--- raw watchdog events per path ---")
    for key, count in watcher.handler.raw_event_counts.items():
        print(f"  {count:3d}  {Path(key).name}")
    print(f"\ncandidates: {len(seen)}   distinct paths: {len(watcher.handler.raw_event_counts)}")


if __name__ == "__main__":
    main()
