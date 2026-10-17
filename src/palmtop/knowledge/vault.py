"""Markdown vault sync for the knowledge base.

Push writes KB entries to `.md` files. Pull reads those files back. The
knowledge base stays the store; this module only moves markdown. Vault
files are plaintext outside the encrypted backup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from palmtop.config.settings import VaultConfig
from palmtop.knowledge.store import KBEntry, KnowledgeBase

log = logging.getLogger(__name__)

_FLAVORS = frozenset({"plain", "obsidian", "logseq"})
_DIRECTIONS = frozenset({"push", "pull", "two-way"})
_CONFLICTS = frozenset({"newest-wins", "vault-wins", "kb-wins"})
_SKIP = {"obsidian": frozenset({".obsidian"}), "logseq": frozenset({"logseq"}), "plain": frozenset()}
_SLUG = re.compile(r"[^a-z0-9]+")


class VaultError(Exception):
    """The vault is disabled, misconfigured, or pointed outside its root."""


@dataclass
class VaultResult:
    written: int = 0
    updated: int = 0
    imported: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def vault_root(cfg: VaultConfig) -> Path:
    """Resolve the vault directory and reject paths that leave it."""
    if not cfg.enabled:
        raise VaultError("Knowledge vault is disabled. Set [knowledge.vault] enabled = true.")
    if cfg.flavor not in _FLAVORS:
        raise VaultError(f"Unknown vault flavor: {cfg.flavor}")
    if cfg.direction not in _DIRECTIONS:
        raise VaultError(f"Unknown vault direction: {cfg.direction}")
    if cfg.conflict not in _CONFLICTS:
        raise VaultError(f"Unknown vault conflict policy: {cfg.conflict}")
    if not cfg.path:
        raise VaultError("Vault path is required.")

    base = Path(cfg.path).expanduser().resolve()
    sub = cfg.subfolder.strip()
    if not sub:
        return base
    parts = Path(sub).parts
    if Path(sub).is_absolute() or ".." in parts:
        raise VaultError("Vault subfolder must stay inside the vault path.")
    root = (base / sub).resolve()
    try:
        root.relative_to(base)
    except ValueError as e:
        raise VaultError("Vault subfolder escapes the vault path.") from e
    return root


def _content_hash(title: str, content: str, tags: str, source: str) -> str:
    payload = json.dumps(
        {"content": content, "source": source, "tags": tags, "title": title},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_note(entry: KBEntry) -> str:
    """Frontmatter subset this sync owns, plus the markdown body."""
    tags = [t.strip() for t in (entry.tags or "").split(",") if t.strip()]
    lines = ["---", f"title: {_quote(entry.title)}"]
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {_quote(tag)}" for tag in tags)
    else:
        lines.append("tags: []")
    lines.append(f"source: {_quote(entry.source or '')}")
    lines.append(f"created: {_quote(entry.created_at or '')}")
    lines.append(f"palmtop_id: {entry.id}")
    lines.append("---")
    body = entry.content or ""
    if body and not body.endswith("\n"):
        body += "\n"
    return "\n".join(lines) + "\n" + body


def parse_note(text: str) -> tuple[dict, str]:
    """Parse the frontmatter subset `render_note` writes. Other files yield no id."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    if body.startswith("\n"):
        body = body[1:]
    meta: dict = {}
    tags: list[str] = []
    in_tags = False
    for line in raw.splitlines():
        if in_tags:
            stripped = line.strip()
            if stripped.startswith("- "):
                tags.append(_scalar(stripped[2:].strip()))
                continue
            in_tags = False
            meta["tags"] = tags
        if not line.strip() or line.startswith(" "):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "tags" and value == "":
            in_tags = True
            tags = []
            continue
        if key == "tags" and value == "[]":
            meta["tags"] = []
            continue
        meta[key] = _scalar(value)
    if in_tags:
        meta["tags"] = tags
    return meta, body.rstrip("\n")


def _scalar(value: str):
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    return value


def _tags_str(meta: dict) -> str:
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        return tags
    return ", ".join(str(t) for t in tags)


def _slug(title: str) -> str:
    slug = _SLUG.sub("-", (title or "note").lower()).strip("-")
    return slug or "note"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {"entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"entries": {}}
    return data


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _iter_markdown(root: Path, flavor: str) -> list[tuple[str, Path]]:
    skip = _SKIP.get(flavor, frozenset())
    found: list[tuple[str, Path]] = []
    if not root.exists():
        return found
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            log.warning("Skipping note outside the vault root: %s", path)
            continue
        if any(part in skip for part in rel.parts):
            continue
        found.append((rel.as_posix(), resolved))
    return found


def _entry_fields(entry: KBEntry) -> tuple[str, str, str, str]:
    return entry.title or "", entry.content or "", entry.tags or "", entry.source or ""


async def vault_push(kb: KnowledgeBase, cfg: VaultConfig, data_dir: Path) -> VaultResult:
    if cfg.direction == "pull":
        raise VaultError("Vault direction is pull. Refusing vault-push.")
    return await _sync(kb, cfg, data_dir, command="push")


async def vault_pull(kb: KnowledgeBase, cfg: VaultConfig, data_dir: Path) -> VaultResult:
    if cfg.direction == "push":
        raise VaultError("Vault direction is push. Refusing vault-pull.")
    return await _sync(kb, cfg, data_dir, command="pull")


async def _sync(kb: KnowledgeBase, cfg: VaultConfig, data_dir: Path, command: str) -> VaultResult:
    root = vault_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "vault_state.json"
    state = _load_state(state_path)
    entries = state["entries"]
    result = VaultResult()

    files = _iter_markdown(root, cfg.flavor)
    by_id: dict[str, tuple[str, Path, dict, str]] = {}
    fresh: list[tuple[str, Path, dict, str]] = []
    for rel, path in files:
        meta, body = parse_note(path.read_text(encoding="utf-8"))
        pid = meta.get("palmtop_id")
        if isinstance(pid, int):
            by_id[str(pid)] = (rel, path, meta, body)
        else:
            fresh.append((rel, path, meta, body))

    kb_entries = {str(e.id): e for e in await kb.list_all()}

    if command == "pull":
        for rel, path, meta, body in fresh:
            title = str(meta.get("title") or path.stem)
            created = meta.get("created")
            new_id = await kb.add(
                title,
                body,
                _tags_str(meta),
                str(meta.get("source") or ""),
                created_at=str(created) if created else None,
            )
            entry = await kb.get(new_id)
            assert entry is not None
            _remember(entries, entry, rel, path)
            result.imported += 1

    shared = set(kb_entries) | set(by_id)
    for sid in sorted(shared, key=int):
        entry = kb_entries.get(sid)
        file_info = by_id.get(sid)
        prior = entries.get(sid) or {}
        if entry and not file_info:
            if command != "push":
                result.skipped += 1
                continue
            rel = _assign_path(root, entry, entries)
            path = root / rel
            _write(path, entry)
            _remember(entries, entry, rel, path)
            result.written += 1
            continue
        if file_info and not entry:
            if command != "pull":
                result.skipped += 1
                continue
            rel, path, meta, body = file_info
            created = meta.get("created")
            new_id = await kb.add(
                str(meta.get("title") or Path(rel).stem),
                body,
                _tags_str(meta),
                str(meta.get("source") or ""),
                created_at=str(created) if created else None,
            )
            imported = await kb.get(new_id)
            assert imported is not None
            _remember(entries, imported, rel, path)
            result.imported += 1
            continue

        assert entry is not None and file_info is not None
        rel, path, meta, body = file_info
        kb_hash = _content_hash(*_entry_fields(entry))
        file_title = str(meta.get("title") or entry.title)
        file_tags = _tags_str(meta)
        file_source = str(meta.get("source") or "")
        file_hash = _content_hash(file_title, body, file_tags, file_source)
        if kb_hash == file_hash:
            if prior.get("hash") != kb_hash:
                _remember(entries, entry, rel, path)
            result.skipped += 1
            continue
        prior_hash = prior.get("hash")
        kb_changed = kb_hash != prior_hash
        file_changed = file_hash != prior_hash
        if not kb_changed and not file_changed:
            result.skipped += 1
            continue
        take_vault = _prefer_vault(cfg.conflict, kb_changed, file_changed, path, prior.get("mtime"))
        if take_vault:
            if command == "push" and not file_changed:
                result.skipped += 1
                continue
            await kb.update(entry.id, title=file_title, content=body, tags=file_tags, source=file_source)
            updated = await kb.get(entry.id)
            assert updated is not None
            _remember(entries, updated, rel, path)
            result.updated += 1
        else:
            if command == "pull" and not kb_changed:
                result.skipped += 1
                continue
            _write(path, entry)
            _remember(entries, entry, rel, path)
            result.written += 1

    _save_state(state_path, state)
    return result


def _prefer_vault(policy: str, kb_changed: bool, file_changed: bool, path: Path, prior_mtime) -> bool:
    if file_changed and not kb_changed:
        return True
    if kb_changed and not file_changed:
        return False
    if policy == "vault-wins":
        return True
    if policy == "kb-wins":
        return False
    try:
        previous = float(prior_mtime)
    except (TypeError, ValueError):
        previous = 0.0
    return path.stat().st_mtime > previous


def _assign_path(root: Path, entry: KBEntry, entries: dict) -> str:
    prior = entries.get(str(entry.id)) or {}
    rel = prior.get("relpath")
    if isinstance(rel, str) and rel:
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            pass
        else:
            return rel
    slug = _slug(entry.title)
    candidate = f"{slug}.md"
    taken = {info.get("relpath") for info in entries.values()}
    if (root / candidate).exists() or candidate in taken:
        candidate = f"{slug}-{entry.id}.md"
    return candidate


def _write(path: Path, entry: KBEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_note(entry)
    path.write_text(text, encoding="utf-8")


def _remember(entries: dict, entry: KBEntry, rel: str, path: Path) -> None:
    entries[str(entry.id)] = {
        "hash": _content_hash(*_entry_fields(entry)),
        "mtime": path.stat().st_mtime if path.exists() else 0,
        "relpath": rel,
    }


def format_result(result: VaultResult) -> str:
    return f"written={result.written} updated={result.updated} imported={result.imported} skipped={result.skipped}"
