"""Create an online-consistent SQLite backup, with optional deployment secrets."""

import argparse
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from dotenv import dotenv_values


def backup(env_path: Path, output_dir: Path, include_secrets=False):
    values = dotenv_values(env_path)
    url = values.get("TM_DATABASE_URL", "sqlite:///data/teammanager.db")
    if not url.startswith("sqlite:///"):
        raise ValueError("This backup command supports SQLite installations only")
    source = Path(url.removeprefix("sqlite:///"))
    if not source.is_absolute():
        source = env_path.parent / source
    if not source.is_file():
        raise FileNotFoundError("Database file does not exist")
    output_dir.mkdir(parents=True, exist_ok=True)
    name = "teammanager-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".zip"
    target = output_dir / name
    with tempfile.TemporaryDirectory() as directory:
        snapshot = Path(directory) / "teammanager.db"
        with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "teammanager.db")
            if include_secrets:
                archive.write(env_path, ".env")
    if os.name != "nt":
        target.chmod(0o600)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=Path(".env").resolve())
    parser.add_argument("--output", type=Path, default=Path("backups"))
    parser.add_argument("--include-secrets", action="store_true")
    args = parser.parse_args()
    print(backup(args.env, args.output, args.include_secrets))
