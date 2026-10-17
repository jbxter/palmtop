"""Signed, append-only fact log — the source of truth for structured memory (issue #55).

One JSON line per fact under ``data_dir/facts.jsonl``. Every writer (agent) owns
its own hash chain; a merged log is a *set* of chains, not one global chain.
Each line carries:

  id      writer-generated id (never a SQLite rowid)
  agent   agent id = first 16 hex of sha256(public key) — cannot be spoofed
  key     Ed25519 public key (hex) that signed the line
  prev    hash of the previous line *of the same writer* ("" for the first)
  sig     Ed25519 signature over the canonical body

``memories.db`` is a projection rebuilt from this log (see ``StructuredMemory``).

Trust model:
  - A valid signature is **not** permission. Permission is the operator
    allowlist (``[facts] allowed_keys`` / ``PALMTOP_FACT_ALLOWED_KEYS``) plus the
    local agent's own key. Lines from other keys stay in the log, are reported
    as unverified, and are never projected into SQLite or the prompt.
  - A broken chain or bad signature is refused (``FactLogError``), never
    silently repaired.
  - The signing key lives outside ``data_dir/docs`` (the only place the agent's
    file tool can write): ``PALMTOP_FACT_SIGNING_KEY`` (dotenvx-managed), else a
    0600 key file (``PALMTOP_FACT_KEY_FILE`` or ``data_dir/fact_signing.key``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

try:  # POSIX only; appends are still single O_APPEND writes without it
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

CATEGORIES = ("preference", "contact", "project", "fact")
FACT_LOG_FILE = "facts.jsonl"
KEY_FILE_NAME = "fact_signing.key"
SIGNING_KEY_ENV = "PALMTOP_FACT_SIGNING_KEY"
KEY_FILE_ENV = "PALMTOP_FACT_KEY_FILE"
ALLOWED_KEYS_ENV = "PALMTOP_FACT_ALLOWED_KEYS"
DEFAULT_MAX_LOG_BYTES = 32 * 1024 * 1024

_VERSION = 1
_DOMAIN = b"palmtop-fact-log-v1\n"
_FACT_FIELDS = frozenset(
    {"v", "type", "id", "agent", "key", "user_id", "category", "content", "ts", "prev", "legacy", "orig_ts"}
)
_TOMBSTONE_FIELDS = frozenset({"v", "type", "id", "agent", "key", "user_id", "target", "ts", "prev"})
_HEX_KEY = re.compile(r"^[0-9a-f]{64}$")
_AGENT = re.compile(r"^[0-9a-f]{16}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSET = object()


class FactLogError(RuntimeError):
    """The fact log failed verification (broken chain, bad signature, bad line)."""


class FactLogFullError(FactLogError):
    """The log reached its byte cap; ``store`` refuses rather than dropping lines."""


def _crypto():
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ModuleNotFoundError as e:  # pragma: no cover
        raise RuntimeError("The signed fact log needs the 'cryptography' package.") from e
    return InvalidSignature, serialization, ed25519


def agent_id_for(public_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


def normalize_key(value: str) -> str | None:
    """Return a lowercase hex Ed25519 public key, or None if malformed."""
    v = (value or "").strip().lower()
    return v if _HEX_KEY.match(v) else None


def normalize_allowed_keys(keys: Iterable[str], *, env: Mapping[str, str] | None = None) -> set[str]:
    """Merge config keys with ``PALMTOP_FACT_ALLOWED_KEYS``; malformed entries are ignored."""
    e = env if env is not None else os.environ
    raw = list(keys) + [k for k in e.get(ALLOWED_KEYS_ENV, "").replace(",", " ").split() if k]
    out: set[str] = set()
    for k in raw:
        n = normalize_key(k)
        if n is None:
            log.warning("Ignoring malformed fact allowlist key: %r", str(k)[:16])
        else:
            out.add(n)
    return out


class Signer:
    """The local agent's Ed25519 identity."""

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise ValueError("fact signing key must be 32 bytes (64 hex chars)")
        _, serialization, ed25519 = _crypto()
        self._key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        raw = self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_hex = raw.hex()
        self.agent_id = agent_id_for(self.public_hex)

    @classmethod
    def generate(cls) -> tuple[Signer, str]:
        seed = os.urandom(32)
        return cls(seed), seed.hex()

    def sign(self, message: bytes) -> str:
        return self._key.sign(message).hex()


def load_signer(data_dir: Path, env: Mapping[str, str] | None = None) -> Signer:
    """Load the signing key: env var (dotenvx) first, else a 0600 key file (created once)."""
    e = env if env is not None else os.environ
    from_env = e.get(SIGNING_KEY_ENV, "").strip()
    if from_env:
        return Signer(bytes.fromhex(from_env))
    path = Path(e.get(KEY_FILE_ENV, "") or data_dir / KEY_FILE_NAME)
    if path.exists():
        return Signer(bytes.fromhex(path.read_text().strip()))
    signer, seed_hex = Signer.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed_hex + "\n")
    log.warning("Generated fact signing key at %s — back it up; set %s to manage it via .env", path, SIGNING_KEY_ENV)
    return signer


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _verify_sig(public_hex: str, sig_hex: str, body: dict) -> bool:
    invalid, _, ed25519 = _crypto()
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pub.verify(bytes.fromhex(sig_hex), _DOMAIN + _canonical(body))
    except (invalid, ValueError):
        return False
    return True


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Entry:
    body: dict
    sig: str
    hash: str
    line: str

    @property
    def id(self) -> str:
        return self.body["id"]

    @property
    def type(self) -> str:
        return self.body["type"]

    @property
    def agent(self) -> str:
        return self.body["agent"]

    @property
    def key(self) -> str:
        return self.body["key"]

    @property
    def prev(self) -> str:
        return self.body["prev"]

    @property
    def user_id(self) -> str:
        return self.body["user_id"]


def _entry_hash(body: dict, sig: str) -> str:
    return hashlib.sha256(_DOMAIN + _canonical(body) + bytes.fromhex(sig)).hexdigest()


def _make_entry(body: dict, sig: str) -> Entry:
    line = json.dumps({**body, "sig": sig}, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Entry(body=body, sig=sig, hash=_entry_hash(body, sig), line=line)


def _validate_body(d: dict, n: int) -> None:
    def bad(why: str) -> FactLogError:
        return FactLogError(f"line {n}: {why}")

    kind = d.get("type")
    if kind == "fact":
        fields = _FACT_FIELDS
    elif kind == "tombstone":
        fields = _TOMBSTONE_FIELDS
    else:
        raise bad("unknown line type")
    if set(d) != fields:
        raise bad("unexpected or missing fields")
    if d["v"] != _VERSION:
        raise bad("unsupported version")
    strings = ("id", "agent", "key", "user_id", "ts", "prev") + (
        ("category", "content") if kind == "fact" else ("target",)
    )
    for f in strings:
        if not isinstance(d[f], str):
            raise bad(f"{f} must be a string")
    if not _ID.match(d["id"]):
        raise bad("malformed id")
    if not _HEX_KEY.match(d["key"]) or not _AGENT.match(d["agent"]):
        raise bad("malformed key or agent")
    if agent_id_for(d["key"]) != d["agent"]:
        raise bad("agent id does not match signing key")
    try:
        datetime.fromisoformat(d["ts"])
    except ValueError:
        raise bad("bad timestamp") from None
    if kind == "fact":
        if d["category"] not in CATEGORIES:
            raise bad("unknown category")
        if not isinstance(d["legacy"], bool) or not (d["orig_ts"] is None or isinstance(d["orig_ts"], str)):
            raise bad("bad legacy fields")
    elif not _ID.match(d["target"]):
        raise bad("malformed target")


def parse_entries(data: bytes, *, anchored: bool = True) -> list[Entry]:
    """Parse + verify a log. Raises FactLogError on any break; never repairs.

    ``anchored=False`` lets each writer's first line carry a non-empty ``prev``
    (a slice that continues a chain the caller already holds).
    """
    if data and not data.endswith(b"\n"):
        raise FactLogError("log ends with a partial line (torn write?) — refusing to serve")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise FactLogError("log is not valid UTF-8") from e
    entries: list[Entry] = []
    tips: dict[str, str] = {}
    ids: set[str] = set()
    for n, line in enumerate(text.splitlines(), start=1):
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            raise FactLogError(f"line {n}: not valid JSON") from e
        if not isinstance(d, dict) or not isinstance(d.get("sig"), str):
            raise FactLogError(f"line {n}: missing signature")
        sig = d.pop("sig")
        _validate_body(d, n)
        try:
            bytes.fromhex(sig)
        except ValueError:
            raise FactLogError(f"line {n}: malformed signature") from None
        if not _verify_sig(d["key"], sig, d):
            raise FactLogError(f"line {n}: bad signature")
        agent = d["agent"]
        if agent in tips:
            if d["prev"] != tips[agent]:
                raise FactLogError(f"line {n}: broken chain for writer {agent}")
        elif anchored and d["prev"] != "":
            raise FactLogError(f"line {n}: chain for writer {agent} does not start at genesis")
        if d["id"] in ids:
            raise FactLogError(f"line {n}: duplicate id")
        ids.add(d["id"])
        entry = _make_entry(d, sig)
        tips[agent] = entry.hash
        entries.append(entry)
    return entries


def retracted_ids(entries: Iterable[Entry], allowed: set[str], local_key: str) -> set[str]:
    """Fact ids hidden by tombstones. A tombstone counts only if its key is allowed and
    it was signed by the target's own writer or by the local agent (the owner)."""
    entries = list(entries)
    writer_of = {e.id: e.key for e in entries if e.type == "fact"}
    out: set[str] = set()
    for e in entries:
        if e.type != "tombstone" or e.key not in allowed:
            continue
        owner = writer_of.get(e.body["target"])
        if owner is not None and e.key in (owner, local_key):
            out.add(e.body["target"])
    return out


@dataclass
class ImportResult:
    appended: int = 0
    skipped: int = 0
    unverified: int = 0  # appended lines whose key is not on the allowlist


class FactLog:
    def __init__(
        self,
        path: Path,
        signer: Signer,
        *,
        allowed_keys: Iterable[str] = (),
        max_bytes: int = DEFAULT_MAX_LOG_BYTES,
    ) -> None:
        self.path = Path(path)
        self._signer = signer
        self.agent_id = signer.agent_id
        self.local_key = signer.public_hex
        self.allowed: set[str] = set(allowed_keys) | {signer.public_hex}
        self.max_bytes = max_bytes
        self._entries: list[Entry] = []
        self._tips: dict[str, str] = {}
        self._seen: object = _UNSET
        self._error: FactLogError | None = None
        self.version = 0  # bumps on every reload or write, so projections know to catch up

    # ── reading ──────────────────────────────────────────────────────
    def _stat(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def refresh(self) -> bool:
        """Re-verify the whole log if the file changed. True if (re)loaded; raises if broken."""
        st = self._stat()
        if st == self._seen:
            if self._error:
                raise self._error
            return False
        self._seen = st
        self._error = None
        self.version += 1
        self._entries, self._tips = [], {}
        if st is None:
            return True
        try:
            entries = parse_entries(self.path.read_bytes())
        except FactLogError as e:
            self._error = e
            raise
        self._entries = entries
        self._tips = {e.agent: e.hash for e in entries}
        return True

    def entries(self) -> list[Entry]:
        self.refresh()
        return list(self._entries)

    def tip(self, agent: str | None = None) -> str:
        self.refresh()
        return self._tips.get(agent or self.agent_id, "")

    def size(self) -> int:
        st = self._stat()
        return st[0] if st else 0

    def is_allowed(self, key: str) -> bool:
        return key in self.allowed

    def retracted(self) -> set[str]:
        return retracted_ids(self.entries(), self.allowed, self.local_key)

    def writers(self) -> dict[str, dict]:
        """Per-writer summary: key, line count, allowed flag."""
        out: dict[str, dict] = {}
        for e in self.entries():
            w = out.setdefault(e.agent, {"key": e.key, "lines": 0, "allowed": e.key in self.allowed})
            w["lines"] += 1
        return out

    # ── writing ──────────────────────────────────────────────────────
    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            self.refresh()  # another process may have appended
            yield fd
        finally:
            os.close(fd)  # releases the flock

    def _write(self, fd: int, entries: list[Entry]) -> None:
        data = "".join(e.line + "\n" for e in entries).encode()
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
        st = os.fstat(fd)
        self._seen = (st.st_size, st.st_mtime_ns)
        self.version += 1
        self._entries.extend(entries)
        for e in entries:
            self._tips[e.agent] = e.hash

    def _sign(self, body: dict) -> Entry:
        body = {**body, "v": _VERSION, "id": uuid.uuid4().hex, "agent": self.agent_id, "key": self.local_key}
        body["prev"] = self._tips.get(self.agent_id, "")
        return _make_entry(body, self._signer.sign(_DOMAIN + _canonical(body)))

    def append_fact(
        self, user_id: str, category: str, content: str, *, legacy: bool = False, orig_ts: str | None = None
    ) -> Entry:
        with self._locked() as fd:
            entry = self._sign(
                {
                    "type": "fact",
                    "user_id": user_id,
                    "category": category,
                    "content": content,
                    "ts": _now(),
                    "legacy": legacy,
                    "orig_ts": orig_ts,
                }
            )
            if self.size() + len(entry.line) + 1 > self.max_bytes:
                raise FactLogFullError(
                    f"Fact log is full ({self.size()} of {self.max_bytes} bytes) — new memories are refused. "
                    "Raise [facts] max_log_bytes or archive the log; nothing was dropped."
                )
            self._write(fd, [entry])
        return entry

    def append_tombstone(self, user_id: str, target_id: str) -> Entry:
        """Retract a fact. Not capped: forgetting must stay possible when the log is full."""
        with self._locked() as fd:
            entry = self._sign({"type": "tombstone", "user_id": user_id, "target": target_id, "ts": _now()})
            self._write(fd, [entry])
        return entry

    # ── exchange (owner-copied file; no socket) ──────────────────────
    def export_bytes(self) -> bytes:
        self.refresh()
        return self.path.read_bytes() if self.path.exists() else b""

    def import_bytes(self, data: bytes) -> ImportResult:
        """Verify an incoming file, then append lines that extend our chains.

        The allowlist is applied at projection time: lines from unlisted keys are kept
        (marked unverified) but never reach SQLite or the prompt. A file that forks or
        contradicts a chain we already hold is refused whole.
        """
        incoming = parse_entries(data, anchored=False)
        result = ImportResult()
        with self._locked() as fd:
            known = {e.hash for e in self._entries}
            ids = {e.id for e in self._entries}
            tips = dict(self._tips)
            fresh: list[Entry] = []
            for e in incoming:
                if e.hash in known:
                    result.skipped += 1
                    continue
                if e.prev != tips.get(e.agent, ""):
                    raise FactLogError(f"import refused: writer {e.agent} does not continue the chain we hold")
                if e.id in ids:
                    raise FactLogError("import refused: duplicate id")
                ids.add(e.id)
                tips[e.agent] = e.hash
                fresh.append(e)
            if fresh and self.size() + sum(len(e.line) + 1 for e in fresh) > self.max_bytes:
                raise FactLogFullError("import refused: it would exceed the fact log byte cap")
            if fresh:
                self._write(fd, fresh)
            result.appended = len(fresh)
            result.unverified = sum(1 for e in fresh if e.key not in self.allowed)
        return result
