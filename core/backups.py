import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from django.conf import settings


def data_root():
    return Path(settings.DATABASES["default"]["NAME"]).resolve().parent


def pending_restore_path():
    return data_root() / "restore.pending.zip"


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
