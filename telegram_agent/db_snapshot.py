"""
Create point-in-time SQLite DB snapshots (safe under WAL).

Uses SQLite's online backup API to produce a consistent copy of the database file
while it may be in use.

Examples:

    python -m telegram_agent.db_snapshot --out-dir telegram_agent/db_snapshots
    python -m telegram_agent.db_snapshot --label before_historical_csv_import
    python -m telegram_agent.db_snapshot --keep 10
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram_agent.config import DATA_DIR, load_config

logger = logging.getLogger(__name__)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _sanitize_label(label: str) -> str:
    s = (label or "").strip()
    if not s:
        return ""
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch.isspace():
            out.append("_")
    return "".join(out).strip("_")


@dataclass
class SnapshotResult:
    src_db: str
    snapshot_path: str
    bytes: int
    created_utc: str


def snapshot_sqlite_db(src_db: Path, dest_path: Path) -> SnapshotResult:
    if not src_db.is_file():
        raise FileNotFoundError(f"DB not found: {src_db}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        raise FileExistsError(f"Refusing to overwrite: {dest_path}")

    # Source can be in WAL mode; backup() provides a consistent snapshot.
    src = sqlite3.connect(str(src_db))
    try:
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    st = dest_path.stat()
    return SnapshotResult(
        src_db=str(src_db),
        snapshot_path=str(dest_path),
        bytes=int(st.st_size),
        created_utc=datetime.now(timezone.utc).isoformat(),
    )


def _apply_retention(out_dir: Path, *, keep: int) -> int:
    if keep <= 0:
        return 0
    snaps = sorted(out_dir.glob("agent_*.sqlite"))
    if len(snaps) <= keep:
        return 0
    to_delete = snaps[: max(0, len(snaps) - keep)]
    for p in to_delete:
        try:
            p.unlink()
        except OSError:
            pass
    return len(to_delete)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Create a consistent snapshot of the agent SQLite DB")
    p.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help=f"Directory to store snapshots (default: {DATA_DIR}/db_snapshots)",
    )
    p.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to SQLite DB (default: AGENT_DB_PATH / config agent_db_path)",
    )
    p.add_argument("--label", type=str, default="", help="Optional label appended to filename")
    p.add_argument(
        "--keep",
        type=int,
        default=0,
        help="If >0, keep only newest N snapshots in out dir (best-effort)",
    )
    args = p.parse_args()

    repo = Path(__file__).resolve().parents[1]
    _load_env_file(repo / ".env")
    _load_env_file(Path(__file__).resolve().parent / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(repo / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    cfg = load_config()
    src_db = Path(args.db_path) if args.db_path else Path(cfg.get("agent_db_path") or (DATA_DIR / "agent.sqlite"))
    out_dir = Path(args.out_dir) if args.out_dir else (DATA_DIR / "db_snapshots")
    label = _sanitize_label(str(args.label))

    fname = f"agent_{_utc_stamp()}"
    if label:
        fname += f"_{label}"
    fname += ".sqlite"
    dest = out_dir / fname

    res = snapshot_sqlite_db(src_db, dest)
    deleted = _apply_retention(out_dir, keep=int(args.keep))
    if deleted:
        logger.info("Retention: deleted %s old snapshot(s)", deleted)
    print(
        {
            "src_db": res.src_db,
            "snapshot_path": res.snapshot_path,
            "bytes": res.bytes,
            "created_utc": res.created_utc,
            "deleted_old": deleted,
        }
    )


if __name__ == "__main__":
    main()

