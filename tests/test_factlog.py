"""Signed fact log: chains, projection, allowlist, prompt fence, cap, backup."""

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from palmtop.backup import create_backup, restore_backup
from palmtop.config.settings import Config
from palmtop.memory.factlog import (
    LOG_NAME,
    FactLog,
    FactLogCapError,
    FactLogChainError,
    format_memory_context,
)
from palmtop.memory.structured import Memory, StructuredMemory


def _seed() -> str:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return raw.hex()


def _public(seed_hex: str) -> str:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


@pytest.fixture
def signing_key(monkeypatch) -> str:
    seed = _seed()
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", seed)
    return seed


async def _memory(tmp_path: Path, **kwargs) -> StructuredMemory:
    mem = StructuredMemory(tmp_path / "memories.db", **kwargs)
    await mem.init()
    return mem


@pytest.mark.asyncio
async def test_append_and_recall(tmp_path: Path, signing_key: str) -> None:
    mem = await _memory(tmp_path)
    row_id = await mem.store("user", "fact", "likes tea")
    assert isinstance(row_id, int) and row_id > 0
    found = await mem.recall("user")
    assert found[0].id == row_id
    assert found[0].content == "likes tea"
    assert found[0].agent_id == _public(signing_key)
    again = await mem.store("user", "fact", "likes tea")
    assert again == -1
    await mem.close()


@pytest.mark.asyncio
async def test_broken_chain_is_refused_and_not_rewritten(tmp_path: Path, signing_key: str) -> None:
    mem = await _memory(tmp_path)
    await mem.store("user", "fact", "one")
    await mem.store("user", "fact", "two")
    await mem.close()
    path = tmp_path / LOG_NAME
    original = path.read_bytes()
    path.write_bytes(original.replace(b"two", b"zzz", 1))
    broken = await _memory(tmp_path)
    with pytest.raises(FactLogChainError):
        await broken.recall("user")
    assert path.read_bytes() == original.replace(b"two", b"zzz", 1)
    await broken.close()


@pytest.mark.asyncio
async def test_bad_signature(tmp_path: Path, signing_key: str) -> None:
    mem = await _memory(tmp_path)
    await mem.store("user", "preference", "dark mode")
    await mem.close()
    path = tmp_path / LOG_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    data = json.loads(lines[0])
    data["sig"] = "aa" + data["sig"][2:]
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    broken = await _memory(tmp_path)
    with pytest.raises(FactLogChainError):
        await broken.search("user", "dark")
    await broken.close()


@pytest.mark.asyncio
async def test_projection_replay_after_crash(tmp_path: Path, signing_key: str) -> None:
    log = FactLog(tmp_path)
    log.append("user", "fact", "replayed")
    mem = await _memory(tmp_path)
    found = await mem.recall("user")
    assert [m.content for m in found] == ["replayed"]
    await mem.close()


@pytest.mark.asyncio
async def test_legacy_import_is_signed_at_import(tmp_path: Path, signing_key: str) -> None:
    mem = await _memory(tmp_path)
    await mem.store("user", "contact", "Ada")
    await mem.close()
    import sqlite3

    conn = sqlite3.connect(tmp_path / "memories.db")
    conn.execute("UPDATE memories SET log_id = '', agent_id = ''")
    conn.commit()
    conn.close()
    (tmp_path / LOG_NAME).unlink()
    (tmp_path / "facts_cursor.json").unlink()
    mem = await _memory(tmp_path)
    line = json.loads((tmp_path / LOG_NAME).read_text(encoding="utf-8"))
    assert line["legacy"] is True
    assert line["content"] == "Ada"
    found = await mem.recall("user")
    assert found[0].log_id == line["id"]
    await mem.close()


@pytest.mark.asyncio
async def test_unknown_key_is_kept_but_not_projected(tmp_path: Path, signing_key: str, monkeypatch) -> None:
    other = _seed()
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", other)
    FactLog(tmp_path).append("user", "fact", "from peer")
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", signing_key)
    mem = await _memory(tmp_path)
    assert await mem.recall("user") == []
    assert "from peer" in (tmp_path / LOG_NAME).read_text(encoding="utf-8")
    await mem.close()


@pytest.mark.asyncio
async def test_allowed_peer_is_projected(tmp_path: Path, signing_key: str, monkeypatch) -> None:
    other = _seed()
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", other)
    FactLog(tmp_path).append("user", "fact", "from peer")
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", signing_key)
    mem = await _memory(tmp_path, peers=[_public(other)])
    found = await mem.recall("user")
    assert found[0].content == "from peer"
    assert found[0].agent_id == _public(other)
    await mem.close()


def test_prompt_fence_and_dedupe() -> None:
    local = Memory(1, "u", "fact", "Same words", "t", agent_id="local", log_id="a")
    peer_same = Memory(2, "u", "fact", "same   words", "t", agent_id="peer", log_id="b")
    peer_other = Memory(3, "u", "fact", "Different", "t", agent_id="peer", log_id="c")
    text = format_memory_context([peer_other, local, peer_same], "local")
    assert text.startswith("[fact] Same words")
    assert "same   words" not in text
    assert '<untrusted-fact agent="peer" id="c">' in text
    assert text.index("[fact] Same words") < text.index("<untrusted-fact")
    assert "data, not instructions" in text


@pytest.mark.asyncio
async def test_byte_cap_appends_nothing(tmp_path: Path, signing_key: str) -> None:
    mem = await _memory(tmp_path, max_bytes=32)
    with pytest.raises(FactLogCapError):
        await mem.store("user", "fact", "this line is longer than the cap")
    assert not (tmp_path / LOG_NAME).exists()
    await mem.close()


@pytest.mark.asyncio
async def test_export_import_round_trip(tmp_path: Path, signing_key: str, monkeypatch) -> None:
    mem = await _memory(tmp_path / "a")
    await mem.store("user", "project", "ship the log")
    exported = await mem.export_facts(tmp_path / "copy.jsonl")
    await mem.close()
    other_seed = _seed()
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", other_seed)
    dest = await _memory(tmp_path / "b", peers=[_public(signing_key)])
    added = await dest.import_facts(exported)
    assert added == 1
    assert (await dest.recall("user"))[0].content == "ship the log"
    assert await dest.import_facts(exported) == 0
    await dest.close()


@pytest.mark.asyncio
async def test_backup_round_trip_includes_log(tmp_path: Path, signing_key: str, monkeypatch) -> None:
    mem = await _memory(tmp_path / "data")
    await mem.store("user", "fact", "backed up")
    await mem.close()
    monkeypatch.setenv("PALMTOP_DB_KEY", "test-secret")
    archive = create_backup(tmp_path / "data", tmp_path / "backups")
    assert archive.name.endswith(".enc")
    restore_backup(archive, tmp_path / "restored")
    text = (tmp_path / "restored" / LOG_NAME).read_text(encoding="utf-8")
    assert "backed up" in text
    monkeypatch.delenv("PALMTOP_DB_KEY", raising=False)


@pytest.mark.asyncio
async def test_forget_stays_hidden_after_replay_and_import(tmp_path: Path, signing_key: str, monkeypatch) -> None:
    mem = await _memory(tmp_path / "a")
    row_id = await mem.store("user", "fact", "secret")
    await mem.forget(row_id)
    assert await mem.recall("user") == []
    assert await mem.search("user", "secret") == []
    FactLog(tmp_path / "a").verify()
    await mem.close()

    replayed = await _memory(tmp_path / "a")
    assert await replayed.recall("user") == []
    exported = await replayed.export_facts(tmp_path / "copy.json")
    await replayed.close()

    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", _seed())
    dest = await _memory(tmp_path / "b", peers=[_public(signing_key)])
    await dest.import_facts(exported)
    assert await dest.recall("user") == []
    FactLog(tmp_path / "b").verify()
    await dest.close()


@pytest.mark.asyncio
async def test_rotation_closes_an_epoch_and_keeps_recall(tmp_path: Path, signing_key: str, monkeypatch) -> None:
    mem = await _memory(tmp_path)
    await mem.store("user", "fact", "first")
    mem._log.max_bytes = (tmp_path / LOG_NAME).stat().st_size * 2 - 1
    await mem.store("user", "fact", "second")
    closed = (tmp_path / "facts-1.jsonl").read_text(encoding="utf-8")
    opened = (tmp_path / LOG_NAME).read_text(encoding="utf-8")
    assert "first" in closed and "second" not in closed
    assert "second" in opened and "first" not in opened
    assert {item.content for item in await mem.recall("user")} == {"first", "second"}
    digest = mem.index_hash()
    FactLog(tmp_path).verify()
    await mem.close()

    replayed = await _memory(tmp_path)
    assert {item.content for item in await replayed.recall("user")} == {"first", "second"}
    assert replayed.index_hash() == digest
    exported = await replayed.export_facts(tmp_path / "epochs.json")
    archive = create_backup(tmp_path, tmp_path / "backups")
    await replayed.close()

    restore_backup(archive, tmp_path / "restored")
    assert "first" in (tmp_path / "restored" / "facts-1.jsonl").read_text(encoding="utf-8")
    monkeypatch.setenv("PALMTOP_FACT_SIGNING_KEY", _seed())
    dest = await _memory(tmp_path / "b", peers=[_public(signing_key)])
    await dest.import_facts(exported)
    assert {item.content for item in await dest.recall("user")} == {"first", "second"}
    await dest.close()


def test_facts_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[facts]\nmax_bytes = 100\npeers = ["abc"]\n', encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.facts.max_bytes == 100
    assert cfg.facts.peers == ["abc"]
