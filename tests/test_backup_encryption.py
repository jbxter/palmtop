"""Tests for at-rest backup/export encryption — issue #48 (option A)."""

from __future__ import annotations

import sqlite3

import pytest

from palmtop.backup import (
    _encrypt_to,
    _read_maybe_encrypted,
    create_backup,
    restore_backup,
)


def _make_db(data_dir):
    """A minimal memories.db with one sensitive row."""
    conn = sqlite3.connect(data_dir / "memories.db")
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, user_id TEXT, category TEXT, content TEXT, created_at TEXT)"
    )
    conn.execute("INSERT INTO memories (user_id, category, content) VALUES ('u', 'c', 'secret note')")
    conn.commit()
    conn.close()


class TestHelpers:
    def test_encrypt_decrypt_roundtrip(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        monkeypatch.setenv("PALMTOP_DB_KEY", "a-long-random-key")
        p = tmp_path / "f.json"
        p.write_bytes(b"hello secret")
        enc = _encrypt_to(p)
        assert enc.name == "f.json.enc"
        assert not p.exists()
        assert b"hello secret" not in enc.read_bytes()  # ciphertext
        assert _read_maybe_encrypted(enc) == b"hello secret"

    def test_no_key_leaves_plaintext(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PALMTOP_DB_KEY", raising=False)
        p = tmp_path / "f.txt"
        p.write_bytes(b"data")
        out = _encrypt_to(p)
        assert out == p  # unchanged
        assert _read_maybe_encrypted(out) == b"data"


class TestBackupRoundtrip:
    def test_encrypted_backup_roundtrip(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        monkeypatch.setenv("PALMTOP_DB_KEY", "test-secret-key")
        data = tmp_path / "data"
        data.mkdir()
        _make_db(data)

        archive = create_backup(data, tmp_path / "out")
        assert archive.name.endswith(".tar.gz.enc")
        assert b"secret note" not in archive.read_bytes()  # not plaintext on disk

        dest = tmp_path / "restored"
        dest.mkdir()
        restored = restore_backup(archive, dest)
        assert "memories.db" in restored
        conn = sqlite3.connect(dest / "memories.db")
        assert conn.execute("SELECT content FROM memories").fetchone()[0] == "secret note"
        conn.close()

    def test_no_key_backup_is_plaintext_and_restorable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PALMTOP_DB_KEY", raising=False)
        data = tmp_path / "data"
        data.mkdir()
        _make_db(data)
        archive = create_backup(data, tmp_path / "out")
        assert archive.name.endswith(".tar.gz")
        assert not archive.name.endswith(".enc")
        assert "memories.db" in restore_backup(archive, tmp_path / "r")


class TestFailClosed:
    def _encrypted_archive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PALMTOP_DB_KEY", "right-key")
        data = tmp_path / "data"
        data.mkdir()
        _make_db(data)
        return create_backup(data, tmp_path / "out")

    def test_wrong_key_fails(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        archive = self._encrypted_archive(tmp_path, monkeypatch)
        monkeypatch.setenv("PALMTOP_DB_KEY", "wrong-key")
        with pytest.raises(RuntimeError, match="Decryption failed"):
            restore_backup(archive, tmp_path / "r2")

    def test_missing_key_fails(self, tmp_path, monkeypatch):
        pytest.importorskip("cryptography")
        archive = self._encrypted_archive(tmp_path, monkeypatch)
        monkeypatch.delenv("PALMTOP_DB_KEY", raising=False)
        with pytest.raises(RuntimeError, match="encrypted"):
            restore_backup(archive, tmp_path / "r3")
