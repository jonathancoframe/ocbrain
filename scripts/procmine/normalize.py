"""Turn a raw tool call into a shape.

The whole point of the miner is that two sessions doing "the same thing" should
produce the same token even though their literal arguments differ. So every
adapter funnels through :func:`arg_signature`, which throws away the payload and
keeps the shape: which command, which notable flags, which *class* of path.

Nothing here may emit a raw argument value. Free text that survives into a
signature is redacted with the core's own ``redact_secrets`` and then re-checked
with ``find_probable_secret_leaks``; anything still flagged is dropped rather
than shipped. A signature that leaks a token is worse than no signature.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from ocbrain.text import find_probable_secret_leaks, redact_secrets

HOME = os.path.expanduser("~")

# Ordered longest-prefix-first: the first match wins, so the specific
# directories must precede the generic ``<path:home>``.
_PATH_CLASSES: list[tuple[str, str]] = [
    (f"{HOME}/Developer/ocbrain-wt", "<path:repo:ocbrain-wt>"),
    (f"{HOME}/Developer/ocbrain", "<path:repo:ocbrain>"),
    (f"{HOME}/Developer", "<path:repo:dev>"),
    (f"{HOME}/coframe", "<path:repo:coframe>"),
    (f"{HOME}/HermesWork", "<path:hermeswork>"),
    (f"{HOME}/Documents/coframe_local_data", "<path:lake>"),
    (f"{HOME}/Documents/Codex", "<path:codexwork>"),
    (f"{HOME}/Documents", "<path:documents>"),
    (f"{HOME}/.ocbrain", "<path:brainstore>"),
    (f"{HOME}/.hermes", "<path:hermesstore>"),
    (f"{HOME}/.claude", "<path:claudestore>"),
    (f"{HOME}/.codex", "<path:codexstore>"),
    (f"{HOME}/.cursor", "<path:cursorstore>"),
    (HOME, "<path:home>"),
    ("/private/tmp", "<path:tmp>"),
    ("/tmp", "<path:tmp>"),
    ("/var", "<path:var>"),
    ("/etc", "<path:etc>"),
    ("/usr", "<path:usr>"),
    ("/opt", "<path:opt>"),
]

_LOOKS_LIKE_PATH = re.compile(r"^(~|/|\./|\.\./)|/")
_URLISH = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_FLAGGY = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$")

# Flags whose *value* is meaningless to a procedure but whose presence is the
# whole point (``--mode=ro``, ``-n``). Values are stripped, names are kept.
_MAX_FLAGS = 4
_MAX_PATHS = 3

# Shell verbs whose first positional argument is really part of the verb.
_SUBCOMMAND_VERBS = {
    "git", "gh", "uv", "pip", "npm", "pnpm", "yarn", "cargo", "go", "docker",
    "kubectl", "gcloud", "aws", "brew", "systemctl", "launchctl", "tmux",
    "ocbrain", "hermes", "poetry", "ruff", "pytest", "python", "python3",
}

# Wrappers that prefix the command the operator actually ran. Some of them take
# their own options, and swallowing those is what makes `env -u PYTHONPATH python`
# read as a command called "-u".
_WRAPPERS = {"sudo", "env", "time", "nohup", "command", "exec", "xargs", "nice"}
_WRAPPER_VALUE_FLAGS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S"},
    "nice": {"-n"},
    "xargs": {"-I", "-n", "-P", "-L", "-d"},
    "sudo": {"-u", "-g", "-U"},
}

# ``uv run pytest`` is a run of pytest. Keeping the runner as the verb would put
# every tool in the repo behind one indistinguishable ``uv run`` node.
_RUNNERS = {("uv", "run"), ("poetry", "run"), ("pnpm", "exec"), ("npm", "exec"), ("pdm", "run")}
# Runner options that take a separate value; `uv run --python 3.13 x.py` would
# otherwise report a command called "3.13".
_RUNNER_VALUE_FLAGS = {"--python", "-p", "--with", "--index", "--extra", "--group", "--project"}
_INTERPRETERS = {"python", "python3", "python3.11", "python3.12", "uv"}

_RESULT_CLASSES = ("ok", "error", "empty", "refused", "timeout")
_SNIFF_CHARS = 600

# Harness-emitted phrases only. Looser wording ("not allowed", "blocked by")
# appears inside ordinary diffs and log output and produced false refusals.
_REFUSAL_MARKERS = (
    "permission denied",
    "requested permissions",
    "user doesn't want to proceed",
    "user rejected",
    "user cancelled",
    "operation not permitted",
    "requires approval",
    "sandbox denied",
    "rejected mcp tool call",
)
_TIMEOUT_MARKERS = (
    "timed out",
    "deadline exceeded",
    "etimedout",
    "timeout after",
    "timeout exceeded",
)
_ERROR_MARKERS = (
    "traceback (most recent call last)",
    "command not found",
    "no such file or directory",
    "fatal:",
    "error:",
    "exception:",
    "syntaxerror",
    "modulenotfounderror",
    "is not a git repository",
    "exit code 1",
    "exit code 2",
)
_EMPTY_MARKERS = (
    "no matches found",
    "no files found",
    "(no content)",
    "0 results",
    "returned no results",
    "no items",
)


def classify_path(raw: str) -> str:
    """Map a filesystem-ish token to a small, stable class token."""
    token = raw.strip().strip("'\"")
    if not token:
        return "<path:none>"
    if _URLISH.match(token):
        scheme = token.split("://", 1)[0].lower()
        return f"<url:{scheme}>"
    expanded = token
    if expanded.startswith("~"):
        expanded = HOME + expanded[1:]
    extension = os.path.splitext(expanded)[1].lower()
    suffix = f":{extension.lstrip('.')}" if extension and len(extension) <= 6 else ""
    for prefix, label in _PATH_CLASSES:
        if expanded == prefix or expanded.startswith(prefix + "/"):
            return f"{label[:-1]}{suffix}>" if suffix else label
    root = "<path:abs>" if expanded.startswith("/") else "<path:rel>"
    return f"{root[:-1]}{suffix}>" if suffix else root


# Absolute-ish paths embedded in prose. `classify_path` maps one whole token; a
# captured error message is free text with paths inside it, which is how a raw
# `/Users/<name>/...` reached a committed artifact and tripped the repo's own
# public-safety scan.
_PATH_IN_TEXT = re.compile(r"(?:~|/(?:Users|home|private|tmp|var|etc|usr|opt))[\w.\-+/~]*")


def _class_paths_in_text(text: str) -> str:
    """Replace every embedded filesystem path with its stable class token."""
    return _PATH_IN_TEXT.sub(lambda match: classify_path(match.group(0)), text)


def _safe(text: str) -> str | None:
    """Redact, class any embedded path, then refuse what still trips the detector.

    Path classing belongs here rather than at each call site: this is the single
    choke point every free-text field passes through, so a new field cannot
    reintroduce the leak by forgetting to call it.
    """
    cleaned = _class_paths_in_text(redact_secrets(text))
    if find_probable_secret_leaks(cleaned):
        return None
    return cleaned


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Peel `VAR=x`, `env -u VAR`, `sudo`, `uv run` ... down to the real verb."""
    out = list(tokens)
    while out:
        head = os.path.basename(out[0])
        if head in _WRAPPERS:
            value_flags = _WRAPPER_VALUE_FLAGS.get(head, set())
            out = out[1:]
            while out:
                token = out[0]
                if token.split("=", 1)[0] in value_flags and "=" not in token:
                    out = out[2:]  # flag plus its value
                elif token.startswith("-"):
                    out = out[1:]
                elif "=" in token and "/" not in token.split("=", 1)[0]:
                    out = out[1:]
                else:
                    break
            continue
        if "=" in out[0] and not out[0].startswith("-") and "/" not in out[0].split("=", 1)[0]:
            out = out[1:]
            continue
        if len(out) >= 2 and (head, out[1]) in _RUNNERS:
            rest = out[2:]
            while rest and rest[0].startswith("-"):
                takes_value = rest[0] in _RUNNER_VALUE_FLAGS
                rest = rest[2:] if takes_value and len(rest) > 1 else rest[1:]
            if rest:
                out = rest
                continue
        break
    return out


def shell_signature(command: str) -> str:
    """Shape a shell command line.

    Only the leading segment of a pipeline/chain is shaped; the rest is summarized
    as a chain marker. Real command lines in this corpus are frequently 300-char
    ``&&``-chains, and keeping all of it would make every signature unique, which
    defeats the mining.
    """
    text = _safe(command)
    if text is None:
        return "bash:<redacted>"
    text = text.strip()
    if not text:
        return "bash:<empty>"

    chained = bool(re.search(r"(\|\||&&|;|\|)", text))
    head_segment = re.split(r"\|\||&&|;|\|", text, maxsplit=1)[0].strip()
    tokens = _strip_wrappers(_split_command(head_segment))
    if not tokens:
        return "bash:<empty>"

    verb = os.path.basename(tokens[0])
    parts = [verb]
    rest = tokens[1:]

    if verb in _INTERPRETERS and "-m" in rest:
        # `python -m pytest` is a pytest run, not a python run.
        slot = rest.index("-m")
        if slot + 1 < len(rest):
            parts.append(f"-m {rest[slot + 1]}")
            rest = rest[:slot] + rest[slot + 2 :]
    elif verb in _SUBCOMMAND_VERBS and rest and not rest[0].startswith("-"):
        parts.append(rest[0])
        rest = rest[1:]

    flags: list[str] = []
    paths: list[str] = []
    for token in rest:
        if token.startswith("-"):
            name = token.split("=", 1)[0]
            if _FLAGGY.match(name) and name not in flags:
                flags.append(name)
        elif _LOOKS_LIKE_PATH.search(token) or _URLISH.match(token):
            cls = classify_path(token)
            if cls not in paths:
                paths.append(cls)

    if flags:
        parts.append(" ".join(sorted(flags)[:_MAX_FLAGS]))
    if paths:
        parts.append(" ".join(paths[:_MAX_PATHS]))
    if chained:
        parts.append("<chain>")
    return "bash:" + " ".join(parts)


def _file_signature(kind: str, path: str) -> str:
    cls = classify_path(path)
    return f"{kind}:{cls}"


def _mcp_signature(server: str, tool: str) -> str:
    server = re.sub(r"[^A-Za-z0-9_.-]", "", server)[:40] or "unknown"
    tool = re.sub(r"[^A-Za-z0-9_.-]", "", tool)[:60] or "unknown"
    return f"mcp:{server}.{tool}"


def _first_str(args: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = args.get(name)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list) and value and isinstance(value[0], str):
            return " ".join(str(v) for v in value)
    return None


def arg_signature(tool: str, args: Any) -> str:
    """Shape any tool call into a stable, payload-free token.

    ``tool`` is the raw tool name as the runtime recorded it. ``args`` is
    whatever the runtime stored (dict, JSON string, list, or None).
    """
    if isinstance(args, str):
        import json

        try:
            args = json.loads(args)
        except Exception:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}

    name = (tool or "").strip()
    lowered = name.lower()

    # MCP tools arrive as ``mcp__server__tool`` (Claude) or already split.
    if lowered.startswith("mcp__"):
        chunks = name.split("__")
        if len(chunks) >= 3:
            return _mcp_signature(chunks[1], "__".join(chunks[2:]))
        return _mcp_signature("unknown", name)

    if lowered in {"bash", "terminal", "shell", "run_terminal_cmd", "local_shell", "exec"}:
        command = _first_str(args, "command", "cmd", "script", "text", "input", "_raw")
        if command is None:
            return "bash:<unknown>"
        return shell_signature(command)

    if lowered in {"read", "read_file", "view", "cat"}:
        path = _first_str(args, "file_path", "path", "target_file", "filename", "file")
        return _file_signature("read", path or "")
    if lowered in {"write", "write_file", "create_file"}:
        path = _first_str(args, "file_path", "path", "target_file", "filename", "file")
        return _file_signature("write", path or "")
    if lowered in {"edit", "patch", "apply_patch", "str_replace", "multiedit", "notebookedit"}:
        path = _first_str(args, "file_path", "path", "target_file", "filename", "file")
        if path is None:
            return "edit:<multi>"
        return _file_signature("edit", path)
    if lowered in {"grep", "search_files", "grep_search", "ripgrep", "codebase_search"}:
        path = _first_str(args, "path", "dir", "directory", "glob", "target_directories")
        return _file_signature("grep", path or "")
    if lowered in {"glob", "file_search", "list_dir", "ls"}:
        path = _first_str(args, "path", "dir", "directory", "pattern", "glob")
        return _file_signature("glob", path or "")
    if lowered in {"webfetch", "web_extract", "fetch"}:
        return "web:fetch"
    if lowered in {"websearch", "web_search"}:
        return "web:search"
    if lowered in {"task", "agent", "delegate_task", "subagent"}:
        subtype = _first_str(args, "subagent_type", "agent_type", "role") or "generic"
        return f"agent:spawn:{re.sub(r'[^A-Za-z0-9_-]', '', subtype)[:32]}"
    if lowered in {"todowrite", "todo", "todo_write", "update_plan"}:
        return "plan:todo"

    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", name)[:60] or "unknown"
    return f"tool:{safe_name}"


def result_class(
    *,
    is_error: bool | None = None,
    exit_code: int | None = None,
    text: str | None = None,
) -> str:
    """Bucket a tool result into one of five classes.

    Explicit signals decide *whether* the call failed; text only decides *how*.
    Getting that order wrong is the classic bug here — a successful ``find`` whose
    output happens to contain "Permission denied" on one line is not a refusal,
    and a failed call whose text says "timed out" is a timeout rather than a
    generic error.
    """
    body = (text or "").strip()
    # Only the head is sniffed. A harness puts its verdict at the top, while a
    # long successful output is exactly what accidentally contains the word
    # "error" somewhere in the middle.
    lowered = body[:_SNIFF_CHARS].lower()
    timed_out = any(marker in lowered for marker in _TIMEOUT_MARKERS)
    refused = any(marker in lowered for marker in _REFUSAL_MARKERS)
    failed = is_error is True or (exit_code is not None and exit_code != 0)
    succeeded = is_error is False or exit_code == 0

    if failed:
        if timed_out:
            return "timeout"
        return "refused" if refused else "error"
    if succeeded:
        if not body or any(marker in lowered for marker in _EMPTY_MARKERS):
            return "empty"
        return "ok"

    # No explicit signal: the text is all we have.
    if timed_out:
        return "timeout"
    if refused:
        return "refused"
    if not body:
        return "empty"
    if any(marker in lowered for marker in _ERROR_MARKERS):
        return "error"
    if any(marker in lowered for marker in _EMPTY_MARKERS):
        return "empty"
    return "ok"


_CLASS_PASSTHROUGH = ("mcp:", "web:", "agent:spawn", "plan:")
_SHELL_FAMILIES: dict[str, str] = {
    "cat": "read", "head": "read", "tail": "read", "less": "read", "sed": "read",
    "ls": "list", "find": "list", "tree": "list", "stat": "list",
    "grep": "search", "rg": "search", "ag": "search", "ack": "search",
    "pytest": "test", "tox": "test", "jest": "test", "vitest": "test",
    "ruff": "lint", "mypy": "lint", "eslint": "lint", "black": "lint",
    "flake8": "lint", "tsc": "lint",
    "mkdir": "write", "touch": "write", "cp": "write", "mv": "write",
    "tee": "write", "chmod": "write", "rm": "write", "ln": "write",
    "ssh": "remote", "scp": "remote", "rsync": "remote", "curl": "remote",
    "wget": "remote", "gcloud": "remote", "aws": "remote", "kubectl": "remote",
    "sqlite3": "query", "jq": "query", "psql": "query", "trino": "query",
    "echo": "shell-misc", "printf": "shell-misc", "cd": "shell-misc",
    "export": "shell-misc", "true": "shell-misc", "set": "shell-misc",
}

# Named tools, across every runtime, mapped onto the same abstract steps as the
# shell verbs above. Without this a hermes `read_file` and a claude `Read` sit on
# different nodes and no cross-runtime procedure can ever be found.
_TOOL_FAMILIES: dict[str, str] = {
    "read_file": "read", "read": "read", "view": "read", "cat": "read",
    "write_file": "write", "create_file": "write", "write": "write",
    "patch": "edit", "edit": "edit", "apply_patch": "edit", "multiedit": "edit",
    "str_replace": "edit", "notebookedit": "edit",
    "search_files": "search", "grep": "search", "grep_search": "search",
    "codebase_search": "search", "session_search": "search",
    "list_dir": "list", "glob": "list", "file_search": "list", "ls": "list",
    "terminal": "shell", "bash": "shell", "shell": "shell", "exec": "shell",
    "execute_code": "shell", "run_terminal_cmd": "shell", "exec_command": "shell",
    "process": "process", "wait": "wait", "wait_agent": "wait",
    "todo": "plan", "todowrite": "plan", "update_plan": "plan",
    "web_search": "web:search", "web_extract": "web:fetch", "webfetch": "web:fetch",
    "skill_view": "skill", "skill_manage": "skill",
    "spawn_agent": "agent:spawn", "delegate_task": "agent:spawn",
    "list_agents": "agent:inspect", "interrupt_agent": "agent:inspect",
    "send_message": "agent:message",
}


def step_class(signature: str) -> str:
    """Collapse a signature to an abstract step.

    Signatures are deliberately specific, which is right for spotting a gotcha
    ("this exact invocation fails 46% of the time") and wrong for aligning two
    sessions that did the same job against different paths. DAG induction runs at
    this coarser level; reliability and repair mining stay at the fine one.
    """
    if signature.startswith(_CLASS_PASSTHROUGH):
        return signature.split(" ")[0]
    for prefix in ("read:", "write:", "edit:", "grep:", "glob:"):
        if signature.startswith(prefix):
            return {"grep:": "search", "glob:": "list"}.get(prefix, prefix.rstrip(":"))
    if signature.startswith("toolonly:"):
        name = signature[9:].lower()
        return _TOOL_FAMILIES.get(name, f"tool:{name}")
    if signature.startswith("codex:exec:"):
        return "codex:exec"
    if signature.startswith("tool:"):
        name = signature[5:].lower()
        return _TOOL_FAMILIES.get(name, signature)
    if signature.startswith("bash:"):
        body = signature[5:]
        verb = body.split(" ", 1)[0]
        if verb in {"git", "gh"}:
            sub = body.split(" ")[1] if " " in body else ""
            return f"{verb}:{sub}" if sub and not sub.startswith("-") else verb
        family = _SHELL_FAMILIES.get(verb)
        if family:
            return family
        return f"shell:{verb}"
    return signature


_ID_LIKE = re.compile(r"\b(?:[0-9a-f]{8,}|\d{4,})\b", re.I)
_FINGERPRINT_CHARS = 160
# Codex wraps every exec result in a fixed preamble; taking the literal first
# line would fingerprint thousands of distinct failures as "Script completed".
_BOILERPLATE = re.compile(
    r"^(?:script completed|wall time\b|output:|stdout:|stderr:|-{2,}|={2,}|\[.*\]$)",
    re.I,
)
_ERRORISH = re.compile(
    r"error|fail|denied|refus|cannot|can't|no such|not found|timed out|invalid|"
    r"traceback|exception|missing|unable|rejected|abort",
    re.I,
)


def error_fingerprint(text: str | None) -> str | None:
    """A short, redacted, id-stripped shape of a failure message.

    Knowing that a step fails 46% of the time is only half a gotcha; the other
    half is *how*. Keeping the first line of the error, with secrets redacted and
    identifiers blanked, makes failures groupable without shipping payloads. If
    the leak detector still flags the result, nothing is kept.
    """
    if not text:
        return None
    lines = [
        stripped
        for line in text.strip().splitlines()[:40]
        if (stripped := line.strip()) and not _BOILERPLATE.match(stripped)
    ]
    if not lines:
        return None
    # Prefer the line that actually says what went wrong; fall back to the first.
    candidate = next((line for line in lines[:12] if _ERRORISH.search(line)), lines[0])
    cleaned = _safe(candidate)
    if cleaned is None:
        return None
    cleaned = _ID_LIKE.sub("<id>", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_FINGERPRINT_CHARS] or None


@dataclass(slots=True)
class TraceEvent:
    """One normalized tool call inside a session."""

    step_index: int
    tool: str
    arg_signature: str
    result_class: str
    at: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "step_index": self.step_index,
            "tool": self.tool,
            "arg_signature": self.arg_signature,
            "result_class": self.result_class,
            "at": self.at,
        }
        if self.error:
            data["error"] = self.error
        return data


@dataclass(slots=True)
class Trace:
    """A normalized session: identity plus an ordered event stream."""

    trace_id: str
    runtime: str
    source_uri: str
    started_at: str | None
    ended_at: str | None
    cwd: str | None
    events: list[TraceEvent] = field(default_factory=list)
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "runtime": self.runtime,
            "source_uri": self.source_uri,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cwd": classify_path(self.cwd) if self.cwd else None,
            # The last two path components name the project/worktree and are what
            # disambiguates concurrent sessions. The rest of the path is dropped
            # so no cache line carries a full home path.
            "cwd_tail": "/".join(self.cwd.rstrip("/").split("/")[-2:]) if self.cwd else None,
            "truncated": self.truncated,
            "n_events": len(self.events),
            "events": [event.as_dict() for event in self.events],
        }


__all__ = [
    "Trace",
    "TraceEvent",
    "arg_signature",
    "classify_path",
    "result_class",
    "shell_signature",
    "step_class",
    "_RESULT_CLASSES",
]
