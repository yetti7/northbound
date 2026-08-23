import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import connection


def data_root():
    return Path(settings.DATABASES["default"]["NAME"]).resolve().parent


def pending_restore_path():
    return data_root() / "restore.pending.zip"


def automatic_backup_directory():
    return data_root() / "backups"


def list_automatic_backups():
    directory = automatic_backup_directory()
    if not directory.exists():
        return []
    return sorted(directory.glob("northbound-automatic-*.zip"), key=lambda path: path.stat().st_mtime, reverse=True)


def create_automatic_backup(retention_count):
    if connection.vendor != "sqlite":
        raise ValueError("Automatic in-app backups require SQLite.")
    directory = automatic_backup_directory()
    directory.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    final_path = directory / f"northbound-automatic-{created_at:%Y%m%d-%H%M%S}.zip"
    temporary_archive = final_path.with_suffix(".creating")
    with tempfile.TemporaryDirectory(dir=data_root(), prefix="backup-") as temporary_directory:
        database_copy = Path(temporary_directory) / "northbound.sqlite3"
        connection.ensure_connection()
        destination = sqlite3.connect(database_copy)
        try:
            connection.connection.backup(destination)
        finally:
            destination.close()
        with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as backup_zip:
            backup_zip.write(database_copy, "northbound.sqlite3")
            media_root = Path(settings.MEDIA_ROOT)
            if media_root.exists():
                for media_file in media_root.rglob("*"):
                    if media_file.is_file():
                        backup_zip.write(media_file, Path("media") / media_file.relative_to(media_root))
            backup_zip.writestr("northbound-backup.json", json.dumps({
                "created_at": created_at.isoformat(),
                "database": "sqlite",
                "contents": ["northbound.sqlite3", "media/"],
                "automatic": True,
            }, indent=2))
    os.replace(temporary_archive, final_path)
    for expired_backup in list_automatic_backups()[retention_count:]:
        expired_backup.unlink(missing_ok=True)
    return final_path


def validate_backup(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            archive_path = PurePosixPath(name)
            if archive_path.is_absolute() or ".." in archive_path.parts:
                raise ValueError("The backup contains an unsafe path.")
        if "northbound.sqlite3" not in names or "northbound-backup.json" not in names:
            raise ValueError("This is not a complete Northbound SQLite backup.")
        metadata = json.loads(archive.read("northbound-backup.json"))
        if metadata.get("database") != "sqlite":
            raise ValueError("This backup does not contain a SQLite database.")
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "northbound.sqlite3"
            with archive.open("northbound.sqlite3") as source, database_path.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            database = sqlite3.connect(database_path)
            try:
                result = database.execute("PRAGMA integrity_check").fetchone()
            finally:
                database.close()
            if not result or result[0] != "ok":
                raise ValueError("The backup database failed its integrity check.")
    return metadata


def stage_restore(uploaded_file):
    target = pending_restore_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target.with_suffix(".uploading")
    with temporary_path.open("wb") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)
    try:
        validate_backup(temporary_path)
        os.replace(temporary_path, target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return target


def apply_pending_restore():
    pending = pending_restore_path()
    if not pending.exists():
        return None
    validate_backup(pending)
    root = data_root()
    database_path = Path(settings.DATABASES["default"]["NAME"])
    media_path = Path(settings.MEDIA_ROOT)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    rollback = root / f"pre-restore-{timestamp}"
    rollback.mkdir()
    if database_path.exists():
        shutil.copy2(database_path, rollback / "northbound.sqlite3")
    if media_path.exists():
        shutil.copytree(media_path, rollback / "media")

    with tempfile.TemporaryDirectory(dir=root, prefix="restore-") as temporary_directory:
        staged = Path(temporary_directory)
        with zipfile.ZipFile(pending) as archive:
            archive.extract("northbound.sqlite3", staged)
            for name in archive.namelist():
                if name.startswith("media/") and not name.endswith("/"):
                    archive.extract(name, staged)
        os.replace(staged / "northbound.sqlite3", database_path)
        replacement_media = staged / "media"
        old_media = root / ".media-before-restore"
        if old_media.exists():
            shutil.rmtree(old_media)
        if media_path.exists():
            os.replace(media_path, old_media)
        if replacement_media.exists():
            os.replace(replacement_media, media_path)
        else:
            media_path.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(old_media, ignore_errors=True)

    pending.unlink()
    (root / "last-restore.json").write_text(json.dumps({
        "restored_at": datetime.now(timezone.utc).isoformat(),
        "rollback_directory": str(rollback),
    }, indent=2))
    return rollback
