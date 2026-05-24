"""Tests for backup.py — backup, restore, export, and import."""

import json
import sqlite3
import tarfile

import pytest

from palmtop.backup import _rotate_backups, create_backup, export_json, import_json, restore_backup


@pytest.fixture
def data_dir(tmp_path):
    """Create a temporary data directory with sample databases."""
    data = tmp_path / "data"
    data.mkdir()

    # Create a conversations.db with sample data
    conn = sqlite3.connect(data / "conversations.db")
    conn.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO messages (user_id, role, content) VALUES ('u1', 'user', 'hello')")
    conn.execute("INSERT INTO messages (user_id, role, content) VALUES ('u1', 'assistant', 'hi there')")
    conn.commit()
    conn.close()

    # Create a memories.db with sample data
    conn = sqlite3.connect(data / "memories.db")
    conn.execute("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO memories (user_id, category, content) VALUES ('u1', 'fact', 'likes coffee')")
    conn.execute("INSERT INTO memories (user_id, category, content) VALUES ('u1', 'preference', 'dark mode')")
    conn.commit()
    conn.close()

    return data


class TestCreateBackup:
    def test_creates_archive(self, data_dir, tmp_path):
        archive = create_backup(data_dir, tmp_path / "backups")
        assert archive.exists()
        assert archive.suffix == ".gz"
        assert "palmtop_backup_" in archive.name

    def test_archive_contains_databases(self, data_dir, tmp_path):
        archive = create_backup(data_dir, tmp_path / "backups")
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert "conversations.db" in names
        assert "memories.db" in names

    def test_raises_on_empty_dir(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            create_backup(empty_dir)

    def test_defaults_to_backups_subdir(self, data_dir):
        archive = create_backup(data_dir)
        assert "backups" in str(archive.parent)


class TestRestoreBackup:
    def test_restores_databases(self, data_dir, tmp_path):
        # Create backup
        archive = create_backup(data_dir, tmp_path / "backups")

        # Restore to a different location
        restore_dir = tmp_path / "restored"
        restored = restore_backup(archive, restore_dir)

        assert "conversations.db" in restored
        assert "memories.db" in restored
        assert (restore_dir / "conversations.db").exists()
        assert (restore_dir / "memories.db").exists()

    def test_restored_data_is_intact(self, data_dir, tmp_path):
        archive = create_backup(data_dir, tmp_path / "backups")
        restore_dir = tmp_path / "restored"
        restore_backup(archive, restore_dir)

        # Verify data
        conn = sqlite3.connect(restore_dir / "memories.db")
        rows = conn.execute("SELECT content FROM memories ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "likes coffee"

    def test_raises_on_missing_archive(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            restore_backup(tmp_path / "nonexistent.tar.gz", tmp_path)


class TestExportJson:
    def test_exports_memories(self, data_dir):
        path = export_json(data_dir)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["version"] == "1.0"
        assert len(data["memories"]) == 2
        assert data["memories"][0]["content"] == "likes coffee"

    def test_exports_conversations(self, data_dir):
        path = export_json(data_dir)
        data = json.loads(path.read_text())
        assert len(data["conversations"]) == 2

    def test_custom_output_path(self, data_dir, tmp_path):
        out = tmp_path / "my_export.json"
        path = export_json(data_dir, out)
        assert path == out
        assert out.exists()


class TestImportJson:
    def test_imports_memories(self, tmp_path):
        # Create an export file
        export_data = {
            "version": "1.0",
            "exported_at": "2024-01-01T00:00:00",
            "memories": [
                {"user_id": "u1", "category": "fact", "content": "born in 1981"},
                {"user_id": "u1", "category": "preference", "content": "vim over emacs"},
            ],
            "conversations": [],
            "plans": [],
        }
        json_path = tmp_path / "import.json"
        json_path.write_text(json.dumps(export_data))

        # Import into a fresh data dir
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        counts = import_json(json_path, data_dir)

        assert counts["memories"] == 2

        # Verify in database
        conn = sqlite3.connect(data_dir / "memories.db")
        rows = conn.execute("SELECT content FROM memories ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[1][0] == "vim over emacs"


class TestRotateBackups:
    def test_removes_old_backups(self, tmp_path):
        # Create 5 fake backup files
        for i in range(5):
            f = tmp_path / f"palmtop_backup_2024010{i}_000000.tar.gz"
            f.write_text(f"fake{i}")

        removed = _rotate_backups(tmp_path, keep=2)
        assert removed == 3
        remaining = list(tmp_path.glob("palmtop_backup_*.tar.gz"))
        assert len(remaining) == 2
