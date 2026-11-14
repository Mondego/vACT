"""Load environment variables for vact.

Loads, in order: a .env discovered from the cwd upward, and the vACT repo's
``config/.env`` if present (so existing API keys keep working). Real environment
variables always take precedence.
"""

from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# 1) nearest .env from cwd upward (no error if missing)
load_dotenv(find_dotenv(usecwd=True))

# 2) repo config/.env relative to this package, if it exists
for base in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
    candidate = base / "config" / ".env"
    if candidate.exists():
        load_dotenv(dotenv_path=str(candidate))
        break
