"""Knowledge vault push/pull."""

import os
from pathlib import Path

import pytest

from palmtop.config.settings import Config, VaultConfig
from palmtop.knowledge.store import KnowledgeBase
from palmtop.knowledge.vault import VaultError, parse_note, vault_pull, vault_push, vault_root


def _cfg(root: Path, **overrides) -> VaultConfig:
    data = dict(
        enabled=True,
        flavor="plain",
        path=str(root),
        subfolder="",
        direction="two-way",
        conflict="newest-wins",
    )
    data.update(overrides)
    return VaultConfig(**data)


async def _kb(tmp_path: Path) -> KnowledgeBase:
    kb = KnowledgeBase(tmp_path / "data" / "knowledge.db")
    await kb.init()
    return kb


@pytest.mark.asyncio
async def test_plain_round_trip_preserves_body(tmp_path: Path) -> None:
    kb = await _kb(tmp_path)
    vault = tmp_path / "vault"
    body = "See [[Other Note]] and keep the #tag inline."
    await kb.add("Project Plan", body, "work, home", "manual")
    cfg = _cfg(vault)
    await vault_push(kb, cfg, tmp_path / "data")
    note = next(vault.glob("*.md"))
    meta, parsed = parse_note(note.read_text(encoding="utf-8"))
    assert meta["palmtop_id"] == 1
    assert meta["title"] == "Project Plan"
    assert parsed == body
    note.write_text(note.read_text(encoding="utf-8").replace(body, body + "\n\nEdited."), encoding="utf-8")
    await vault_pull(kb, cfg, tmp_path / "data")
    entry = await kb.get(1)
    assert entry is not None
    assert "Edited." in entry.content
    assert "[[Other Note]]" in entry.content
    await kb.close()


@pytest.mark.asyncio
async def test_obsidian_skips_config_dir(tmp_path: Path) -> None:
    kb = await _kb(tmp_path)
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "workspace.md").write_text("secret", encoding="utf-8")
    (vault / "notes").mkdir()
    (vault / "notes" / "idea.md").write_text(
        '---\ntitle: "Idea"\ntags:\n  - "a"\nsource: ""\ncreated: "2020-01-01"\n---\n[[Link]] stays\n',
        encoding="utf-8",
    )
    cfg = _cfg(vault, flavor="obsidian")
    result = await vault_pull(kb, cfg, tmp_path / "data")
    assert result.imported == 1
    rows = await kb.list_all()
    assert len(rows) == 1
    assert rows[0].content == "[[Link]] stays"
    await kb.close()


@pytest.mark.asyncio
async def test_logseq_bullets_survive_and_config_is_skipped(tmp_path: Path) -> None:
    kb = await _kb(tmp_path)
    vault = tmp_path / "vault"
    (vault / "logseq").mkdir(parents=True)
    (vault / "logseq" / "config.md").write_text("ignore", encoding="utf-8")
    (vault / "pages").mkdir()
    bullets = "- parent\n  - child\n- sibling"
    (vault / "pages" / "page.md").write_text(
        f'---\ntitle: "Page"\ntags: []\nsource: ""\ncreated: ""\n---\n{bullets}\n',
        encoding="utf-8",
    )
    cfg = _cfg(vault, flavor="logseq")
    await vault_pull(kb, cfg, tmp_path / "data")
    entry = (await kb.list_all())[0]
    assert entry.content == bullets
    await vault_push(kb, cfg, tmp_path / "data")
    text = (vault / "pages" / "page.md").read_text(encoding="utf-8")
    assert bullets in text
    assert "palmtop_id:" in text
    assert not (vault / "logseq" / "config.md").read_text(encoding="utf-8").startswith("---")
    await kb.close()


@pytest.mark.asyncio
async def test_second_push_does_not_rewrite(tmp_path: Path) -> None:
    kb = await _kb(tmp_path)
    vault = tmp_path / "vault"
    await kb.add("Stable", "same", "", "")
    cfg = _cfg(vault)
    await vault_push(kb, cfg, tmp_path / "data")
    note = next(vault.glob("*.md"))
    mtime = note.stat().st_mtime
    os.utime(note, (mtime - 50, mtime - 50))
    before = note.stat().st_mtime
    result = await vault_push(kb, cfg, tmp_path / "data")
    assert result.written == 0
    assert note.stat().st_mtime == before
    await kb.close()


@pytest.mark.asyncio
async def test_conflict_policies(tmp_path: Path) -> None:
    for policy, expect_kb in (("vault-wins", "from-vault"), ("kb-wins", "from-kb")):
        root = tmp_path / policy
        kb = await _kb(root)
        vault = root / "vault"
        await kb.add("Title", "original", "t", "s")
        cfg = _cfg(vault, conflict=policy)
        await vault_push(kb, cfg, root / "data")
        await kb.update(1, content="from-kb")
        note = next(vault.glob("*.md"))
        text = note.read_text(encoding="utf-8").replace("original", "from-vault")
        note.write_text(text, encoding="utf-8")
        await vault_push(kb, cfg, root / "data")
        entry = await kb.get(1)
        assert entry is not None
        assert expect_kb in entry.content
        if policy == "kb-wins":
            assert "from-kb" in note.read_text(encoding="utf-8")
        await kb.close()


@pytest.mark.asyncio
async def test_newest_wins_follows_file_mtime(tmp_path: Path) -> None:
    kb = await _kb(tmp_path)
    vault = tmp_path / "vault"
    await kb.add("Title", "original", "", "")
    cfg = _cfg(vault, conflict="newest-wins")
    data = tmp_path / "data"
    await vault_push(kb, cfg, data)
    note = next(vault.glob("*.md"))
    await kb.update(1, content="from-kb")
    note.write_text(note.read_text(encoding="utf-8").replace("original", "from-vault"), encoding="utf-8")
    os.utime(note, (1, 1))
    await vault_push(kb, cfg, data)
    entry = await kb.get(1)
    assert entry is not None
    assert entry.content == "from-kb"
    assert "from-kb" in note.read_text(encoding="utf-8")
    await kb.close()


def test_subfolder_cannot_escape(tmp_path: Path) -> None:
    with pytest.raises(VaultError):
        vault_root(_cfg(tmp_path, subfolder="../outside"))
    with pytest.raises(VaultError):
        vault_root(_cfg(tmp_path, subfolder="/etc"))


def test_pull_direction_refuses_push(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, direction="pull")
    with pytest.raises(VaultError):
        import asyncio

        asyncio.run(_refuse(tmp_path, cfg))


async def _refuse(tmp_path: Path, cfg: VaultConfig) -> None:
    kb = await _kb(tmp_path)
    try:
        await vault_push(kb, cfg, tmp_path / "data")
    finally:
        await kb.close()


def test_vault_config_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[knowledge.vault]\nenabled = true\nflavor = "obsidian"\npath = "/tmp/v"\n'
        'subfolder = "Palmtop"\ndirection = "push"\nconflict = "kb-wins"\n',
        encoding="utf-8",
    )
    cfg = Config.load(path)
    assert cfg.vault.enabled is True
    assert cfg.vault.flavor == "obsidian"
    assert cfg.vault.direction == "push"
    assert cfg.vault.conflict == "kb-wins"
