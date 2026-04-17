"""`renee` launcher package. Aliases `src` so `python -m renee ...` works."""
from src.cli.main import main as main  # re-export


__all__ = ["main"]
