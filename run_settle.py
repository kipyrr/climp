"""Phase 1 steps 2 and 3 — watcher and settle checker wired together.

No database yet, by design: the blueprint keeps Phase 1 in memory so the settle
logic can be got right before anything durable depends on it.

    # watch a test folder, print promotions, upload nothing
    python run_settle.py --root C:\\tmp\\clips --seconds 60

    # the real folder, and actually upload what settles
    python run_settle.py --upload

--upload is off by default on purpose. There is very little free Drive quota
(roadblock 2), so uploading is an explicit choice, not a default.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

from climp.settle import POLL_SECONDS, Candidate, Outcome, SettleLoop
from climp.watcher import Watcher

log = logging.getLogger("run_settle")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(os.environ["USERPROFILE"]) / "Videos" / "NVIDIA")
    ap.add_argument("--seconds", type=float, help="Stop after N seconds. Omit for Ctrl-C.")
    ap.add_argument("--upload", action="store_true", help="Actually upload promoted clips.")
    ap.add_argument(
        "--backfill-since",
        type=float,
        default=None,
        help="Unix time. Files older than this are dropped (D15). Default: now.",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )

    backfill_since = args.backfill_since if args.backfill_since is not None else time.time()
    log.info("backfill_since = %s (older files are dropped)", time.strftime("%H:%M:%S", time.localtime(backfill_since)))

    promoted: list[Candidate] = []
    dropped: list[tuple[str, Outcome, str]] = []

    def on_ready(c: Candidate) -> None:
        promoted.append(c)
        size_mb = (c.size_bytes or 0) / (1024 * 1024)
        log.info("READY  %s  (%.1f MB)", c.path.name, size_mb)
        if args.upload:
            import spike  # Phase 0 code, called from the promotion point

            svc = spike.build("drive", "v3", credentials=spike.authenticate(), cache_discovery=False)
            spike.upload(svc, c.path, spike.ensure_folder(svc))

    def on_dropped(c: Candidate, outcome: Outcome, reason: str) -> None:
        dropped.append((c.path.name, outcome, reason))
        log.info("%-16s %s  (%s)", outcome.value.upper(), c.path.name, reason)

    settle = SettleLoop(on_ready=on_ready, on_dropped=on_dropped, backfill_since=backfill_since)
    watcher = Watcher(args.root, on_candidate=settle.add)
    watcher.start()

    started = time.monotonic()
    try:
        while args.seconds is None or (time.monotonic() - started) < args.seconds:
            settle.poll_once()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()

    print("\n--- promoted to ready ---")
    for c in promoted:
        print(f"  {(c.size_bytes or 0) / (1024 * 1024):8.1f} MB  {c.path.name}")
    print("\n--- dropped or failed ---")
    for name, outcome, reason in dropped:
        print(f"  {outcome.value:16s} {name}  ({reason})")
    print(f"\nstill pending: {settle.pending}")


if __name__ == "__main__":
    main()
