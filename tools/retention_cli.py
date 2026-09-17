"""Preview or run the retention sweep by hand.

Retention deletes files from Drive, so the default here is a dry run. You have
to ask twice to actually remove anything.

    python tools/retention_cli.py                    # show what is in Drive
    python tools/retention_cli.py --keep 40          # preview, deletes nothing
    python tools/retention_cli.py --keep 40 --apply  # actually remove them
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from climp import config as config_module  # noqa: E402
from climp.db import DONE, Db  # noqa: E402
from climp.drive import DriveClient  # noqa: E402
from climp.retention import sweep  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, help="How many of the newest clips to keep in Drive.")
    ap.add_argument("--min-age-hours", type=float, help="Never remove anything younger than this.")
    ap.add_argument("--apply", action="store_true", help="Actually delete. Without this it is a preview.")
    args = ap.parse_args()

    cfg = config_module.load()
    db = Db(cfg.db_path)
    db.init_schema()

    live = [c for c in db.by_state(DONE) if c.retired_at is None]
    retired = db.retired_count()
    total = sum(c.size_bytes or 0 for c in live)

    print(f"In Drive : {len(live)} clips, {total / 1024**3:.2f} GB")
    print(f"Retired  : {retired} previously removed")
    print(f"Config   : retention_enabled={cfg.retention_enabled}, "
          f"keep_newest={cfg.retention_keep_newest}, min_age_hours={cfg.retention_min_age_hours:.0f}")

    if args.keep is None:
        print("\nPass --keep N to preview what would be removed.")
        return 0

    keep = args.keep
    min_age = args.min_age_hours if args.min_age_hours is not None else cfg.retention_min_age_hours

    candidates = db.retention_candidates(keep_newest=keep, min_age_seconds=min_age * 3600, now=time.time())
    if not candidates:
        print(f"\nNothing eligible with keep={keep}, min_age={min_age:.0f}h.")
        return 0

    print(f"\nEligible for removal (keep={keep}, nothing younger than {min_age:.0f}h):")
    for c in candidates:
        age_days = (time.time() - (c.mtime or c.first_seen_at)) / 86400
        print(f"  {(c.size_bytes or 0) / 1024**2:7.0f} MB  {age_days:5.1f} days old  {c.path.name}")

    freed = sum(c.size_bytes or 0 for c in candidates)
    print(f"\n  {len(candidates)} clips, {freed / 1024**3:.2f} GB would be freed")

    if not args.apply:
        print("\nThis was a preview. Nothing was deleted. Add --apply to do it for real.")
        return 0

    client = DriveClient(cfg.client_secret_path, cfg.token_path)
    client.authorise()
    result = sweep(db, client, keep_newest=keep, min_age_hours=min_age)
    print(f"\n{result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
