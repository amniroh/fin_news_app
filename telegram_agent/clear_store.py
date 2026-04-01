#!/usr/bin/env python3
"""Clear the seen-items store so previously seen items will be summarized again."""
import sys
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from telegram_agent.config import load_config


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")

    config = load_config()
    path = Path(config["seen_ids_file"])

    if path.exists():
        path.write_text('{"ids": []}')
        print(f"Cleared store: {path}")
    else:
        print(f"Store empty or not created yet: {path}")


if __name__ == "__main__":
    main()
