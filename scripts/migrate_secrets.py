"""Migrate plaintext .env secrets into the OS keyring.

Run once after ``pip install keyring``::

    python scripts/migrate_secrets.py            # actual migration
    python scripts/migrate_secrets.py --dry-run  # preview only
    python scripts/migrate_secrets.py --check    # report what's where

After successful migration you can wipe the secret values from .env;
the launcher's ``populate_env_from_keyring()`` call at startup pulls
them back into env transparently for the rest of the codebase.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_secrets")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would happen without writing")
    parser.add_argument("--check", action="store_true",
                        help="Print where each known secret lives now")
    args = parser.parse_args(argv)

    # .env values must be in os.environ before we can migrate them.
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    from renee import secrets

    if args.check:
        for name in secrets.KNOWN_SECRETS:
            sources = []
            kr = secrets._keyring()
            if kr is not None:
                try:
                    if kr.get_password(secrets.SERVICE_NAME, name):
                        sources.append("keyring")
                except Exception:
                    pass
            import os
            if os.environ.get(name):
                sources.append("env")
            if not sources:
                print(f"  {name:<24}  (none)")
            else:
                print(f"  {name:<24}  {' + '.join(sources)}")
        return 0

    summary = secrets.migrate_env_to_keyring(dry_run=args.dry_run)
    width = max(len(k) for k in summary) if summary else 16
    for name, status in summary.items():
        print(f"  {name:<{width}}  {status}")
    if args.dry_run:
        print("\n(dry run — no changes made)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
