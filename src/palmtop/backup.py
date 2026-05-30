"""Backup and restore for Palmtop's SQLite databases.

CLI:
    palmtop backup               # Create timestamped backup archive
    palmtop backup --output /p   # Specify output directory
    palmtop restore <archive>    # Restore from backup
    palmtop export               # Export memory to JSON
    palmtop import <file>        # Import from JSON

All agent state lives in SQLite databases under data_dir/:
  - conversations.db  (chat history)
  - memories.db       (structured facts)
  - plans.db          (plans and goals)
  - knowledge.db      (knowledge base)
  - reminders.db      (scheduled reminders)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

log = logging.getLogger(__name__)

# Databases that make up the agent's full state
DB_FILES = [
    "conversations.db",
    "memories.db",
    "plans.db",
    "knowledge.db",
    "reminders.db",
]

# Suffix marking an encrypted backup/export artifact (issue #48).
ENC_SUFFIX = ".enc"


def _db_key() -> str:
    return os.environ.get("PALMTOP_DB_KEY", "").strip()


def _fernet(passphrase: str):
    """Build a Fernet from PALMTOP_DB_KEY (any passphrase → derived 32-byte key)."""
    try:
        from cryptography.fernet import Fernet
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "Backup/export encryption needs the 'cryptography' package. Install it with "
            "`uv sync --extra encryption` (or `pkg install python-cryptography` on Termux)."
        ) from e
    import base64
    import hashlib

    key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
    return Fernet(key)


def _encrypt_to(path: Path) -> Path:
    """Encrypt `path` in place when PALMTOP_DB_KEY is set; return the (new) path.

    With no key set the artifact is left plaintext and a warning is logged — so
    existing workflows keep working, but the operator is told it's unencrypted.
    """
    key = _db_key()
    if not key:
        log.warning(
            "PALMTOP_DB_KEY not set — %s is UNENCRYPTED. Set it to encrypt backups/exports.",
            path.name,
        )
        return path
    token = _fernet(key).encrypt(path.read_bytes())
    enc_path = path.with_name(path.name + ENC_SUFFIX)
    enc_path.write_bytes(token)
    path.unlink()
    log.info("Encrypted: %s", enc_path.name)
    return enc_path


def _read_maybe_encrypted(path: Path) -> bytes:
    """Read `path`, transparently decrypting it if it's an encrypted artifact.

    Fails closed: an encrypted file with no/incorrect key raises rather than
    silently producing garbage.
    """
    raw = path.read_bytes()
    if not path.name.endswith(ENC_SUFFIX):
        return raw
    key = _db_key()
    if not key:
        raise RuntimeError(f"{path.name} is encrypted — set PALMTOP_DB_KEY to restore it.")
    fernet = _fernet(key)  # raises a clear error if cryptography is missing
    from cryptography.fernet import InvalidToken

    try:
        return fernet.decrypt(raw)
    except InvalidToken as e:
        raise RuntimeError("Decryption failed — wrong PALMTOP_DB_KEY?") from e


def create_backup(
    data_dir: Path,
    output_dir: Path | None = None,
    *,
    keep: int = 0,
) -> Path:
    """Create a tar.gz backup of all SQLite databases.

    Args:
        data_dir: Path to the data directory containing .db files.
        output_dir: Where to write the archive. Defaults to data_dir/backups/.
        keep: Number of backups to retain (0 = keep all).

    Returns:
        Path to the created archive.
    """
    if output_dir is None:
        output_dir = data_dir / "backups"
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    archive_name = f"palmtop_backup_{timestamp}.tar.gz"
    archive_path = output_dir / archive_name

    # Collect existing DB files
    db_files = [data_dir / name for name in DB_FILES if (data_dir / name).exists()]

    if not db_files:
        raise FileNotFoundError(f"No database files found in {data_dir}")

    # Create safe SQLite copies (using backup API to avoid corruption)
    tmp_dir = output_dir / f".tmp_backup_{timestamp}"
    tmp_dir.mkdir(exist_ok=True)

    try:
        for db_path in db_files:
            dst = tmp_dir / db_path.name
            _safe_copy_db(db_path, dst)

        # Create tar.gz from the safe copies
        with tarfile.open(archive_path, "w:gz") as tar:
            for f in tmp_dir.iterdir():
                tar.add(f, arcname=f.name)

    finally:
        # Clean up temp dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Encrypt at rest if a key is configured (issue #48) — backups are the
    # artifact most likely to leave the device's storage encryption.
    archive_path = _encrypt_to(archive_path)

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    log.info("Backup created: %s (%.2f MB, %d databases)", archive_path, size_mb, len(db_files))

    # Rotate old backups
    if keep > 0:
        _rotate_backups(output_dir, keep)

    return archive_path


def restore_backup(archive_path: Path, data_dir: Path) -> list[str]:
    """Restore databases from a backup archive.

    Args:
        archive_path: Path to the tar.gz backup.
        data_dir: Target data directory.

    Returns:
        List of restored database filenames.
    """
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    data_dir.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []

    # Transparently decrypt if the archive is encrypted (#48).
    raw = _read_maybe_encrypted(archive_path)
    with tarfile.open(fileobj=BytesIO(raw), mode="r:gz") as tar:
        # Security: only extract known DB files
        filter_kwarg = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        for member in tar.getmembers():
            if member.name in DB_FILES:
                tar.extract(member, path=data_dir, **filter_kwarg)
                restored.append(member.name)
                log.info("Restored: %s", member.name)
            else:
                log.warning("Skipped unknown file in archive: %s", member.name)

    if not restored:
        raise ValueError("No recognized database files found in archive")

    log.info("Restore complete: %d databases", len(restored))
    return restored


def export_json(data_dir: Path, output_path: Path | None = None) -> Path:
    """Export all structured memory to a portable JSON file.

    Args:
        data_dir: Path to the data directory.
        output_path: Where to write JSON. Defaults to data_dir/export_<timestamp>.json.

    Returns:
        Path to the exported JSON file.
    """
    if output_path is None:
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
        output_path = data_dir / f"export_{timestamp}.json"

    export_data: dict = {
        "version": "1.0",
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "memories": [],
        "conversations": [],
        "plans": [],
    }

    # Export structured memories
    memories_db = data_dir / "memories.db"
    if memories_db.exists():
        conn = sqlite3.connect(memories_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT user_id, category, content, created_at FROM memories ORDER BY id").fetchall()
            export_data["memories"] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            log.warning("memories.db has no 'memories' table — skipping")
        finally:
            conn.close()

    # Export conversations (summary only — full messages can be large)
    conv_db = data_dir / "conversations.db"
    if conv_db.exists():
        conn = sqlite3.connect(conv_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT user_id, role, content, created_at FROM messages ORDER BY id DESC LIMIT 1000"
            ).fetchall()
            export_data["conversations"] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            log.warning("conversations.db schema not found — skipping")
        finally:
            conn.close()

    # Export plans
    plans_db = data_dir / "plans.db"
    if plans_db.exists():
        conn = sqlite3.connect(plans_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT user_id, title, content, status, created_at FROM plans ORDER BY id").fetchall()
            export_data["plans"] = [dict(r) for r in rows]
        except sqlite3.OperationalError:
            log.warning("plans.db schema not found — skipping")
        finally:
            conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export_data, indent=2, default=str))
    output_path = _encrypt_to(output_path)  # encrypt at rest if PALMTOP_DB_KEY set (#48)
    n_mem, n_conv, n_plans = len(export_data["memories"]), len(export_data["conversations"]), len(export_data["plans"])
    log.info("Exported to %s (%d memories, %d messages, %d plans)", output_path, n_mem, n_conv, n_plans)
    return output_path


def import_json(json_path: Path, data_dir: Path) -> dict[str, int]:
    """Import structured memory from a JSON export.

    Args:
        json_path: Path to the exported JSON file.
        data_dir: Target data directory.

    Returns:
        Dict with counts of imported items per category.
    """
    if not json_path.exists():
        raise FileNotFoundError(f"Import file not found: {json_path}")

    data = json.loads(_read_maybe_encrypted(json_path).decode("utf-8"))
    counts: dict[str, int] = {"memories": 0, "conversations": 0, "plans": 0}

    # Import memories
    if data.get("memories"):
        memories_db = data_dir / "memories.db"
        conn = sqlite3.connect(memories_db)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for mem in data["memories"]:
                conn.execute(
                    "INSERT INTO memories (user_id, category, content, created_at) VALUES (?, ?, ?, ?)",
                    (mem["user_id"], mem["category"], mem["content"], mem.get("created_at")),
                )
            conn.commit()
            counts["memories"] = len(data["memories"])
        finally:
            conn.close()

    log.info("Import complete: %s", counts)
    return counts


def _safe_copy_db(src: Path, dst: Path) -> None:
    """Copy a SQLite database using the backup API (safe even if db is open)."""
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    try:
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
        dst_conn.close()


def _rotate_backups(backup_dir: Path, keep: int) -> int:
    """Delete old backups, keeping only the most recent `keep` files."""
    archives = sorted(
        backup_dir.glob("palmtop_backup_*.tar.gz*"),  # matches plaintext and .enc
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for old in archives[keep:]:
        old.unlink()
        removed += 1
        log.debug("Rotated old backup: %s", old.name)
    return removed
