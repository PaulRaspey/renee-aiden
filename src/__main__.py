"""`python -m src` entry point — forwards to the CLI dispatcher."""
from src.cli.main import main


if __name__ == "__main__":
    raise SystemExit(main())
