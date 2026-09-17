"""climp — automatic upload of game clips to Google Drive.

See IMPLEMENTATION-PLAN.md for the architecture. The short version:
one clip is one row in one SQLite table, and every component's only job is to
move that row from one state to the next. No component imports another.
"""

__version__ = "0.0.1"

# A literal, not a computed timestamp. An earlier attempt read the source
# files' mtimes at runtime, which a stale process reports just as happily as a
# fresh one -- it proved nothing. This string is compiled into whatever code is
# actually loaded, so if a running instance reports an old value, it really is
# running old code.
BUILD = "2026-09-17 00:15"
