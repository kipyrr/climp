"""ClipSync — automatic upload of game clips to Google Drive.

See IMPLEMENTATION-PLAN.md for the architecture. The short version:
one clip is one row in one SQLite table, and every component's only job is to
move that row from one state to the next. No component imports another.
"""

__version__ = "0.0.1"
