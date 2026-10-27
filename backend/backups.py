"""Daily SQLite snapshots in the deployment backup directory; secrets stay encrypted."""

import asyncio
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import sqlite3

from backend.models import now

NAME = re.compile(r"teammanager-auto-\d{8}T\d{12}Z\.db$")


def snapshot_database(source, directory, keep):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "teammanager-auto-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".db"
    target = directory / name
    partial = directory / (name + ".partial")
    try:
        with closing(
            sqlite3.connect("file:" + Path(source).resolve().as_posix() + "?mode=ro", uri=True)
        ) as src:
            with closing(sqlite3.connect(partial)) as dst:
                src.backup(dst)
                if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("backup_integrity")
        if os.name != "nt":
            partial.chmod(0o600)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    previous = sorted(
        p for p in directory.iterdir() if NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()
    )
    for old in previous[:-keep]:
        if old.resolve().parent != directory:
            raise RuntimeError("backup_path")
        old.unlink()
    return target


class AutoBackups:
    def __init__(self, settings):
        self.hours, self.keep, self.directory = (
            settings.backup_hours,
            settings.backup_keep,
            settings.backup_dir,
        )
        self.source = settings.database_url.removeprefix("sqlite:///")
        self.supported = settings.database_url.startswith("sqlite:///")
        self.pending, self.retry_at = False, 0
        self.state = {
            "enabled": bool(self.hours),
            "status": "disabled" if not self.hours else "pending",
            "last_success_at": None,
            "interval_hours": self.hours,
            "keep": self.keep,
        }
        if self.hours and self.directory.is_dir():
            previous = [
                p.stat().st_mtime for p in self.directory.iterdir() if NAME.fullmatch(p.name) and p.is_file()
            ]
            if previous:
                self.state.update(last_success_at=max(previous), status="success")

    async def run(self):
        try:
            if not self.supported:
                raise RuntimeError("unsupported_database")
            self.state["status"] = "running"
            await asyncio.to_thread(snapshot_database, self.source, self.directory, self.keep)
            self.state.update(last_success_at=now(), status="success")
        except Exception:
            self.state["status"] = "failed"
            self.retry_at = now() + 3600
        finally:
            self.pending = False

    def tick(self, services):
        if (
            self.hours
            and not self.pending
            and self.retry_at <= now()
            and now() - (self.state["last_success_at"] or 0) >= self.hours * 3600
        ):
            self.pending = True
            services.spawn(self.run())
