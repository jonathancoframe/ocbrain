"""Transcript windows, and the pointers that let the store stop keeping them.

Importing a transcript records a *window* of it: a redacted head plus a sliding
tail, joined by an omission marker. That window is not a slice of the file and
cannot be reconstructed from one; it is a function of the file's length and of
the exact bytes at both ends at the moment of import.

Keeping those windows inline was 99.13% of all evidence body bytes and 82.4% of
the event ledger -- ~127 MB of a 177 MB core, growing ~87 MB/month. So the
window text now lives where it always lived (the file on disk) and the store
keeps a pointer: the recorded head excerpt, and enough of a description to
rebuild the window byte-for-byte and *prove* it rebuilt correctly.

**What the digests are, exactly.** ``window_sha256`` covers the produced window
text, and it is checked after every rebuild -- that is the serving guarantee,
and nothing is returned to a caller without it. ``window_input_sha256`` covers
the inputs the window was built from: the file's length plus the head and tail
bytes actually sampled. It is *not* a whole-file digest, and it is not named
like one. The transcript corpus behind this store is 8.6 GB across 2,357 files,
the largest 376 MB, re-harvested every fifteen minutes; hashing every changed
file end to end would read gigabytes per cycle to answer a question the
``window_sha256`` check already answers for free. A mid-file edit that leaves
the length and both sampled ends untouched is therefore not detected here --
but it cannot change the window either, so the rebuilt text still verifies.

**When rebuilding fails, it says so.** A transcript that has since grown, been
rotated, or been deleted -- 12.4% of recorded source URIs already dangle -- is
reported as a typed unavailability, never an exception and never a silently
different window.
"""

from __future__ import annotations

import hashlib
import mmap
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ocbrain.text import redact_secrets

BODY_REF_SCHEMA = "ocbrain.body_ref.v1"
BODY_REF_STORAGE = "source_file_window"

# Bumped whenever the window construction below changes shape. An older
# pointer then fails its window hash and reports unavailable, which is the
# correct answer: this build can no longer reproduce what that one recorded.
WINDOW_BUILDER = "history_text_window.v1"

# The head excerpt kept in the row. Two jobs: it is what import compares to
# decide a re-import is the same transcript re-windowed rather than a new one,
# and it is the only text a reader gets when the source file is gone.
# ``deslop.REWINDOW_HEAD_CHARS`` is this value; they must not drift.
HISTORY_HEAD_CHARS = 2_000

UNAVAILABLE_SOURCE_MISSING = "source_file_missing"
UNAVAILABLE_SOURCE_UNREADABLE = "source_unreadable"
UNAVAILABLE_WINDOW_CHANGED = "source_window_changed"
UNAVAILABLE_UNSUPPORTED_REF = "unsupported_body_ref"

_PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def _iter_redacted_lines(lines, *, in_private_key: bool = False):
    for raw_line in lines:
        remaining = raw_line
        while remaining:
            if in_private_key:
                end = _PRIVATE_KEY_END_RE.search(remaining)
                if end is None:
                    break
                remaining = remaining[end.end() :]
                in_private_key = False
                continue

            begin = _PRIVATE_KEY_BEGIN_RE.search(remaining)
            if begin is None:
                yield redact_secrets(remaining)
                break

            prefix = remaining[: begin.start()]
            if prefix:
                yield redact_secrets(prefix)
            yield "[REDACTED_PRIVATE_KEY]"
            remaining = remaining[begin.end() :]
            in_private_key = True


def iter_redacted_history(path: Path):
    """Yield redacted history text without loading the whole file at once."""
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        yield from _iter_redacted_lines(handle)


def _private_key_state_at(handle, offset: int) -> bool:
    """Return whether ``offset`` lies inside a PEM private-key block."""
    if offset <= 0:
        return False
    with mmap.mmap(handle.fileno(), length=0, access=mmap.ACCESS_READ) as mapped:
        state = False
        cursor = 0
        needle = b"PRIVATE KEY-----"
        while True:
            found = mapped.find(needle, cursor, offset)
            if found < 0:
                return state
            prefix = mapped[max(0, found - 80) : found]
            begin = prefix.rfind(b"-----BEGIN ")
            end = prefix.rfind(b"-----END ")
            if begin >= 0 and begin > end:
                state = True
            elif end >= 0:
                state = False
            cursor = found + len(needle)


def _redact_history_fragment(raw: bytes, *, in_private_key: bool = False) -> bytes:
    text = raw.decode("utf-8", errors="replace")
    redacted = "".join(
        _iter_redacted_lines(text.splitlines(keepends=True), in_private_key=in_private_key)
    )
    return redacted.encode("utf-8", errors="replace")


@dataclass(frozen=True)
class _WindowSource:
    """Everything the window is a function of, and nothing else."""

    mode: str
    source_bytes: int
    whole: bytes = b""
    head_raw: bytes = b""
    tail_raw: bytes = b""
    tail_private: bool = False


def _read_window_source(path: Path, *, max_bytes: int) -> _WindowSource:
    source_bytes = path.stat().st_size
    sample_span = max(max_bytes * 4, 65_536)
    if source_bytes <= sample_span:
        complete = "".join(iter_redacted_history(path)).encode("utf-8", errors="replace")
        return _WindowSource(mode="whole_file", source_bytes=source_bytes, whole=complete)

    with path.open("rb") as handle:
        head_raw = handle.read(sample_span)
        # Drop a partially sampled history record; a credential could cross
        # that arbitrary byte boundary and evade pattern-based redaction.
        newline = head_raw.rfind(b"\n")
        head_raw = head_raw[: newline + 1] if newline >= 0 else b""

        tail_offset = max(source_bytes - sample_span, 0)
        tail_private = _private_key_state_at(handle, tail_offset)
        handle.seek(tail_offset)
        tail_raw = handle.read()
        newline = tail_raw.find(b"\n")
        if tail_offset:
            tail_raw = tail_raw[newline + 1 :] if newline >= 0 else b""
    return _WindowSource(
        mode="head_tail",
        source_bytes=source_bytes,
        head_raw=head_raw,
        tail_raw=tail_raw,
        tail_private=tail_private,
    )


def _compose_window(source: _WindowSource, *, max_bytes: int) -> str:
    head_len = max_bytes // 2
    tail_len = max_bytes - head_len
    if source.mode == "whole_file":
        complete = source.whole
        if len(complete) <= max_bytes:
            return complete.decode("utf-8", errors="replace")
        marker = f"\n\n[... {len(complete) - max_bytes} bytes omitted from middle ...]\n\n".encode()
        return (complete[:head_len] + marker + complete[-tail_len:]).decode(
            "utf-8", errors="replace"
        )
    head = _redact_history_fragment(source.head_raw)[:head_len]
    tail = _redact_history_fragment(source.tail_raw, in_private_key=source.tail_private)[-tail_len:]
    omitted = max(source.source_bytes - len(source.head_raw) - len(source.tail_raw), 0)
    marker = f"\n\n[... {omitted} source bytes omitted from middle ...]\n\n".encode()
    return (head + marker + tail).decode("utf-8", errors="replace")


def _window_input_digest(source: _WindowSource, *, max_bytes: int) -> str:
    digest = hashlib.sha256()
    digest.update(
        f"{WINDOW_BUILDER}\0{source.mode}\0{source.source_bytes}\0{max_bytes}\0".encode()
    )
    if source.mode == "whole_file":
        digest.update(source.whole)
        return digest.hexdigest()
    digest.update(f"{len(source.head_raw)}\0".encode())
    digest.update(source.head_raw)
    digest.update(f"{len(source.tail_raw)}\0".encode())
    digest.update(source.tail_raw)
    digest.update(b"1" if source.tail_private else b"0")
    return digest.hexdigest()


def history_text_window(path: Path, *, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    return _compose_window(_read_window_source(path, max_bytes=max_bytes), max_bytes=max_bytes)


@dataclass(frozen=True)
class HistoryWindow:
    """A built window, plus what the store keeps instead of its text."""

    text: str
    head: str
    body_ref: dict[str, Any]

    @property
    def window_sha256(self) -> str:
        return str(self.body_ref["window_sha256"])

    @property
    def source_content_hash(self) -> str:
        return str(self.body_ref["window_input_sha256"])


def build_history_window(path: Path, *, max_bytes: int, source_uri: str) -> HistoryWindow:
    """Build the window and the pointer that can rebuild it later."""
    source = _read_window_source(path, max_bytes=max_bytes)
    text = _compose_window(source, max_bytes=max_bytes) if max_bytes > 0 else ""
    return HistoryWindow(
        text=text,
        head=text[:HISTORY_HEAD_CHARS],
        body_ref={
            "schema_version": BODY_REF_SCHEMA,
            "storage": BODY_REF_STORAGE,
            "source_uri": source_uri,
            "source_bytes": source.source_bytes,
            "window_builder": WINDOW_BUILDER,
            "window_max_bytes": max_bytes,
            # Covers the window inputs (length + both sampled ends), not the
            # whole file. See the module docstring for why.
            "window_input_sha256": _window_input_digest(source, max_bytes=max_bytes),
            "window_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "window_chars": len(text),
            "head_chars": min(len(text), HISTORY_HEAD_CHARS),
        },
    )


@dataclass(frozen=True)
class RehydratedWindow:
    """Either the window text, or a named reason there is none. Never both."""

    text: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.text is not None


def is_body_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("schema_version") == BODY_REF_SCHEMA
        and value.get("storage") == BODY_REF_STORAGE
        and isinstance(value.get("source_uri"), str)
    )


def rehydrate_history_window(body_ref: Any) -> RehydratedWindow:
    """Rebuild a pointed-at window from disk, or say why it cannot be rebuilt.

    Returns rather than raises for every failure mode. A caller expanding a
    source needs a typed answer it can hand to a model; an exception here would
    surface as a tool error and tell the model nothing about whether the
    transcript is gone, grown, or merely different.
    """
    if not is_body_ref(body_ref):
        return RehydratedWindow(reason=UNAVAILABLE_UNSUPPORTED_REF)
    if body_ref.get("window_builder") != WINDOW_BUILDER:
        return RehydratedWindow(reason=UNAVAILABLE_WINDOW_CHANGED)
    path = Path(str(body_ref["source_uri"]))
    max_bytes = int(body_ref.get("window_max_bytes") or 0)
    try:
        if not path.is_file():
            return RehydratedWindow(reason=UNAVAILABLE_SOURCE_MISSING)
        # Length is in the digest, so a grown transcript is already a miss.
        # Checking it first turns the common case into one stat instead of a
        # re-read of both ends of the file.
        if path.stat().st_size != int(body_ref.get("source_bytes", -1)):
            return RehydratedWindow(reason=UNAVAILABLE_WINDOW_CHANGED)
        source = _read_window_source(path, max_bytes=max_bytes)
    except OSError:
        return RehydratedWindow(reason=UNAVAILABLE_SOURCE_UNREADABLE)
    if _window_input_digest(source, max_bytes=max_bytes) != body_ref.get("window_input_sha256"):
        return RehydratedWindow(reason=UNAVAILABLE_WINDOW_CHANGED)
    text = _compose_window(source, max_bytes=max_bytes) if max_bytes > 0 else ""
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != body_ref.get("window_sha256"):
        # The inputs matched but the output did not: this build composes
        # windows differently from the one that recorded the pointer. Refuse
        # rather than serve text under a hash it does not have.
        return RehydratedWindow(reason=UNAVAILABLE_WINDOW_CHANGED)
    return RehydratedWindow(text=text)


__all__ = [
    "BODY_REF_SCHEMA",
    "BODY_REF_STORAGE",
    "HISTORY_HEAD_CHARS",
    "UNAVAILABLE_SOURCE_MISSING",
    "UNAVAILABLE_SOURCE_UNREADABLE",
    "UNAVAILABLE_UNSUPPORTED_REF",
    "UNAVAILABLE_WINDOW_CHANGED",
    "WINDOW_BUILDER",
    "HistoryWindow",
    "RehydratedWindow",
    "build_history_window",
    "history_text_window",
    "is_body_ref",
    "iter_redacted_history",
    "rehydrate_history_window",
]
