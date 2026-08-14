import glob
import os

import backup
from database import Database


async def test_create_backup_produces_valid_snapshot(db, tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    service_id = await db.add_service("Стрижка", 1000, 60)

    path = await backup.create_backup(db)

    assert os.path.exists(path)
    snapshot = Database(path)
    await snapshot.connect()
    try:
        service = await snapshot.get_service(service_id)
        assert service is not None
        assert service["name"] == "Стрижка"
    finally:
        await snapshot.close()


def test_prune_old_backups_keeps_only_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(backup, "BACKUP_KEEP_COUNT", 2)
    names = [f"{backup._DB_STEM}_2026010{i}_000000.db" for i in range(1, 6)]
    for name in names:
        (tmp_path / name).write_bytes(b"x")

    backup.prune_old_backups()

    remaining = sorted(os.path.basename(p) for p in glob.glob(str(tmp_path / f"{backup._DB_STEM}_*.db")))
    assert remaining == sorted(names)[-2:]


async def test_run_backup_job_sends_document_to_admin(db, tmp_path, monkeypatch, bot, session):
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(backup, "ADMIN_IDS", [777])

    await backup.run_backup_job(bot, db)

    docs = session.calls_named("SendDocument")
    assert docs and docs[0].chat_id == 777


async def test_run_backup_job_survives_send_failure(db, tmp_path, monkeypatch, bot, session):
    """A failed delivery to one admin must not prevent the backup file from being written
    or crash the scheduled job."""
    monkeypatch.setattr(backup, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(backup, "ADMIN_IDS", [777])
    session.responses["SendDocument"] = RuntimeError("network down")

    await backup.run_backup_job(bot, db)

    assert glob.glob(str(tmp_path / f"{backup._DB_STEM}_*.db"))
