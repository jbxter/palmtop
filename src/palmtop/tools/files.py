"""Local file persistence tool for the agent.

Manages files in data/docs/ — the agent can create, read, update, list,
search, and delete documents. Sandboxed to the docs directory.

Allowed extensions: .md, .txt, .py, .json, .csv, .toml, .yaml, .yml
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

from palmtop.tools.base import Tool

log = logging.getLogger(__name__)

_ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".py", ".json", ".csv", ".toml", ".yaml", ".yml",
}

# Max file size the agent can write (64 KB — plenty for docs, safe for memory)
_MAX_WRITE_BYTES = 65_536

# Max files returned from list/search
_MAX_LIST = 50


class FileTool(Tool):
    name = "files"
    description = (
        "Create and manage local documents. Usage:\n"
        "  [TOOL:files] write <path> | <content> — create or overwrite a file\n"
        "  [TOOL:files] read <path> — read file contents\n"
        "  [TOOL:files] append <path> | <content> — append to an existing file\n"
        "  [TOOL:files] list [subdir] — list files with sizes and dates\n"
        "  [TOOL:files] search <query> — search text across all docs\n"
        "  [TOOL:files] delete <path> — delete a file\n"
        "  [TOOL:files] tree — show full directory tree\n\n"
        "Paths are relative to docs/ (e.g. 'strategy/q3-plan.md'). "
        "Allowed types: .md .txt .py .json .csv .toml .yaml .yml"
    )

    def __init__(self, data_dir: Path) -> None:
        self._root = (data_dir / "docs").resolve()

    def _resolve(self, path_str: str) -> Path | None:
        """Resolve and validate a path within the docs root.

        Returns None if the path escapes the sandbox or has a
        disallowed extension.
        """
        path_str = path_str.strip().strip("'\"")
        if not path_str:
            return None

        # Block absolute paths and parent traversal
        if path_str.startswith("/") or ".." in path_str:
            return None

        resolved = (self._root / path_str).resolve()

        # Must stay inside the docs root
        try:
            resolved.relative_to(self._root.resolve())
        except ValueError:
            return None

        # Check extension
        if resolved.suffix.lower() not in _ALLOWED_EXTENSIONS:
            return None

        return resolved

    async def run(self, query: str) -> str:
        parts = query.strip().split(None, 1)
        if not parts:
            return self._usage()

        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if action == "write":
                return self._write(rest)
            elif action == "read":
                return self._read(rest)
            elif action == "append":
                return self._append(rest)
            elif action == "list":
                return self._list(rest)
            elif action == "search":
                return self._search(rest)
            elif action == "delete":
                return self._delete(rest)
            elif action == "tree":
                return self._tree()
            else:
                # Maybe they passed a path directly — try reading it
                full_query = f"{action} {rest}".strip() if rest else action
                path = self._resolve(full_query)
                if path and path.exists():
                    return self._read(full_query)
                return self._usage()
        except Exception as e:
            log.exception("File tool error")
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _write(self, text: str) -> str:
        path_str, _, content = text.partition("|")
        path_str = path_str.strip()
        content = content.strip()

        if not path_str or not content:
            return "Format: write <path> | <content>"

        path = self._resolve(path_str)
        if path is None:
            return self._bad_path(path_str)

        if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            return f"Content too large (max {_MAX_WRITE_BYTES // 1024}KB)."

        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")

        verb = "Updated" if existed else "Created"
        size = len(content.encode("utf-8"))
        log.info("File %s: %s (%d bytes)", verb.lower(), path_str, size)
        return f"{verb} {path_str} ({size:,} bytes)"

    def _read(self, path_str: str) -> str:
        path_str = path_str.strip()
        path = self._resolve(path_str)
        if path is None:
            return self._bad_path(path_str)

        if not path.exists():
            return f"File not found: {path_str}"

        content = path.read_text(encoding="utf-8")
        size = len(content.encode("utf-8"))
        # Truncate very large files in output
        if len(content) > 8000:
            content = content[:8000] + f"\n\n... (truncated, {size:,} bytes total)"
        return f"--- {path_str} ({size:,} bytes) ---\n{content}"

    def _append(self, text: str) -> str:
        path_str, _, content = text.partition("|")
        path_str = path_str.strip()
        content = content.strip()

        if not path_str or not content:
            return "Format: append <path> | <content>"

        path = self._resolve(path_str)
        if path is None:
            return self._bad_path(path_str)

        if not path.exists():
            return f"File not found: {path_str} (use 'write' to create it first)"

        # Check combined size
        current_size = path.stat().st_size
        new_bytes = len(content.encode("utf-8"))
        if current_size + new_bytes > _MAX_WRITE_BYTES:
            return f"Would exceed max file size ({_MAX_WRITE_BYTES // 1024}KB)."

        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + content)

        total = current_size + new_bytes + 1  # +1 for the newline
        log.info("File appended: %s (+%d bytes)", path_str, new_bytes)
        return f"Appended to {path_str} ({total:,} bytes total)"

    def _list(self, subdir: str) -> str:
        root = self._root
        if subdir:
            sub = (root / subdir.strip()).resolve()
            try:
                sub.relative_to(root.resolve())
            except ValueError:
                return "Invalid directory."
            if not sub.is_dir():
                return f"Not a directory: {subdir}"
            root = sub

        if not root.exists():
            return "No documents yet. Use 'write' to create your first file."

        files = sorted(root.rglob("*"))
        files = [f for f in files if f.is_file() and f.suffix.lower() in _ALLOWED_EXTENSIONS]

        if not files:
            return "No documents yet. Use 'write' to create your first file."

        lines = []
        for f in files[:_MAX_LIST]:
            rel = f.relative_to(self._root)
            size = f.stat().st_size
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {rel}  ({_human_size(size)}, {mtime})")

        header = f"Documents ({len(files)} files)"
        if len(files) > _MAX_LIST:
            header += f" — showing first {_MAX_LIST}"
        return header + ":\n" + "\n".join(lines)

    def _search(self, query: str) -> str:
        if not query.strip():
            return "Need a search query."

        if not self._root.exists():
            return "No documents to search."

        pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)
        matches = []

        for f in sorted(self._root.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in _ALLOWED_EXTENSIONS:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

            for i, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    rel = f.relative_to(self._root)
                    preview = line.strip()[:120]
                    matches.append(f"  {rel}:{i}  {preview}")
                    if len(matches) >= _MAX_LIST:
                        break
            if len(matches) >= _MAX_LIST:
                break

        if not matches:
            return f"No matches for: {query}"

        return f"Search results for '{query}' ({len(matches)} hits):\n" + "\n".join(matches)

    def _delete(self, path_str: str) -> str:
        path_str = path_str.strip()
        path = self._resolve(path_str)
        if path is None:
            return self._bad_path(path_str)

        if not path.exists():
            return f"File not found: {path_str}"

        path.unlink()
        log.info("File deleted: %s", path_str)

        # Clean up empty parent dirs (but not the docs root itself)
        parent = path.parent
        while parent != self._root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

        return f"Deleted {path_str}"

    def _tree(self) -> str:
        if not self._root.exists():
            return "No documents yet."

        lines = [f"docs/"]
        self._tree_walk(self._root, lines, prefix="")

        if len(lines) == 1:
            return "No documents yet. Use 'write' to create your first file."

        return "\n".join(lines)

    def _tree_walk(self, directory: Path, lines: list[str], prefix: str) -> None:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        visible = [
            e for e in entries
            if e.is_dir() or (e.is_file() and e.suffix.lower() in _ALLOWED_EXTENSIONS)
        ]

        for i, entry in enumerate(visible):
            is_last = (i == len(visible) - 1)
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                self._tree_walk(entry, lines, prefix + child_prefix)
            else:
                size = _human_size(entry.stat().st_size)
                lines.append(f"{prefix}{connector}{entry.name}  ({size})")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _bad_path(path_str: str) -> str:
        if ".." in path_str:
            return "Path cannot contain '..' — files are sandboxed to docs/."
        if path_str.startswith("/"):
            return "Use relative paths (e.g. 'notes/ideas.md', not '/notes/ideas.md')."
        ext = Path(path_str).suffix
        if ext and ext.lower() not in _ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
            return f"Extension '{ext}' not allowed. Supported: {allowed}"
        return f"Invalid path: {path_str}"

    @staticmethod
    def _usage() -> str:
        return (
            "Usage:\n"
            "  write <path> | <content>\n"
            "  read <path>\n"
            "  append <path> | <content>\n"
            "  list [subdir]\n"
            "  search <query>\n"
            "  delete <path>\n"
            "  tree"
        )


def _human_size(nbytes: int) -> str:
    if nbytes < 1024:
        return f"{nbytes}B"
    elif nbytes < 1024 * 1024:
        return f"{nbytes / 1024:.1f}KB"
    else:
        return f"{nbytes / (1024 * 1024):.1f}MB"
