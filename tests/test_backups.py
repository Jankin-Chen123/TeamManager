import sqlite3
from types import SimpleNamespace

from backend.backups import snapshot_database, AutoBackups


def test_online_snapshot_restore_and_retention_only_generated_files(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE test (value TEXT)")
        db.execute("INSERT INTO test VALUES ('persisted')")
    directory = tmp_path / "snapshots"
    directory.mkdir()
    manual = directory / "manual-backup.db"
    manual.write_bytes(b"keep manually saved backups")
    first = snapshot_database(source, directory, 1)
    latest = snapshot_database(source, directory, 1)
    assert manual.exists() and latest.exists() and not first.exists()
    with sqlite3.connect(latest) as restored:
        assert restored.execute("SELECT value FROM test").fetchone()[0] == "persisted"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


async def test_failed_scheduled_backup_reports_failure_and_backoff(tmp_path):
    manager = AutoBackups(
        SimpleNamespace(
            backup_hours=24,
            backup_keep=14,
            backup_dir=tmp_path / "backups",
            database_url="sqlite:///" + str(tmp_path / "missing.db"),
        )
    )
    await manager.run()
    assert manager.state["status"] == "failed" and manager.retry_at > 0
    spawned = []
    manager.tick(SimpleNamespace(spawn=lambda c: spawned.append(c)))
    assert not spawned


async def test_backup_restart_detects_last_success(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE test (id INTEGER)")
    settings = SimpleNamespace(
        backup_hours=24,
        backup_keep=14,
        backup_dir=tmp_path / "backups",
        database_url="sqlite:///" + str(source),
    )
    manager = AutoBackups(settings)
    await manager.run()
    assert manager.state["status"] == "success"
    restarted = AutoBackups(settings)
    assert restarted.state["last_success_at"] is not None
    spawned = []
    restarted.tick(SimpleNamespace(spawn=lambda c: spawned.append(c)))
    assert not spawned
