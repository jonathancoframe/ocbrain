"""Server-observed identity for one MCP connection.

Everything OCBrain knew about *who* was calling used to be text the model typed
into ``context``: 129 distinct ``served_to_runtime`` spellings across the
corpus, no session id at all on 44.7% of retrievals and 37.4% of closeouts, and
only 13 closeouts that identity-join to a transcript. A whole
``canonical_runtime`` subsystem existed to guess afterwards at what the model
should have said. Guessing is not identity.

This module captures what the process can observe for itself, and keeps the
three kinds of identity in three separate fields rather than collapsing them
into one column that nobody can later un-mix:

``server_connection_id``
    A UUID this process mints when a connection opens. Authoritative: nothing
    outside the server can forge it, and two connections never share one. It
    names a *connection*, not a conversation -- a client that reconnects gets a
    new id even if the human is still in the same chat.

``client_session_hint``
    Whatever the harness put in the environment of the MCP child process.
    **Harness-attested, not server-verified.** The server reads it from its own
    environment, so no model can type it in, but the server cannot check that
    the value still names the conversation it is serving: its stability across
    ``/resume``, ``/clear``, and compaction is unverified, and Claude Code
    subagents inherit the parent session's value, so a stamped hint resolves to
    the parent transcript. That is exactly why it is stored beside the
    connection id and never merged into it.

``client_runtime_key``
    Which client is on the other end, observed rather than asserted, in
    descending order of trust: ``OCBRAIN_CLIENT`` (set deliberately by the
    operator in a client's launch config), ``AI_AGENT`` (the harness describing
    itself), then the ``clientInfo.name`` from the MCP handshake.

The model-supplied ``context.session`` and ``context.runtime`` keep being
recorded exactly as sent, in the columns they always used. They are a fourth,
weakest source, and now they can be told apart from the rest.

Per-client reality, measured on this install by reading live process
environments:

* **Claude Code** -- one MCP child per session carrying
  ``CLAUDE_CODE_SESSION_ID``, byte-identical to the transcript filename. Full
  benefit.
* **Hermes** -- one MCP child per *gateway profile* with every session
  multiplexed over it, so a per-connection id names the gateway, not the
  session. Its consolation is ``OCBRAIN_CLIENT=hermes:<profile>`` in the
  profile's ``env:`` block, which at least makes the runtime key exact.
* **Codex** -- unverified; no Codex process was running when this was written.
  Everything here degrades to ``None`` when the environment is bare.
* **Cursor** -- no benefit. Its exporter captures chat bubbles, not a tool log,
  so there is no trace for a session id to join to.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

# Read in order; the first non-empty value wins and its name is recorded.
# ``OCBRAIN_SESSION_ID`` comes first as the harness-agnostic escape hatch: a
# client that can export a per-session variable but is not Claude Code has
# somewhere to put it without this module learning a new vendor name.
SESSION_HINT_ENV_VARS: tuple[str, ...] = (
    "OCBRAIN_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
)
RUNTIME_KEY_ENV_VARS: tuple[str, ...] = (
    "OCBRAIN_CLIENT",
    "AI_AGENT",
)

# One value, so the honesty is machine-readable and not only in this docstring.
HARNESS_ATTESTED = "harness_attested"
SERVER_MINTED = "server_minted"


@dataclass(frozen=True)
class Provenance:
    """What the server observed about the caller, separate from what it claimed.

    Deliberately *not* part of :class:`~ocbrain.scope.ScopeContext`. That value's
    ``to_dict()`` feeds both ``retrieval_uses.context_json`` and the retrieval
    ``stable_id``; adding a per-connection UUID to it would make every retrieval
    id depend on which process served it, so two identical reads would no longer
    be the same read. Provenance travels beside the context, never inside it.
    """

    server_connection_id: str | None = None
    client_session_hint: str | None = None
    client_session_hint_source: str | None = None
    client_runtime_key: str | None = None
    client_runtime_key_source: str | None = None
    client_name: str | None = None

    @classmethod
    def capture(
        cls,
        *,
        client_name: str | None = None,
        env: dict[str, str] | None = None,
        connection_id: str | None = None,
    ) -> Provenance:
        """Mint a connection id and read the environment the harness handed us."""
        environ = os.environ if env is None else env
        hint, hint_source = _first_env(environ, SESSION_HINT_ENV_VARS)
        runtime_key, runtime_source = _first_env(environ, RUNTIME_KEY_ENV_VARS)
        name = _clean(client_name)
        if runtime_key is None and name is not None:
            runtime_key, runtime_source = name, "client_info.name"
        return cls(
            server_connection_id=connection_id or uuid.uuid4().hex,
            client_session_hint=hint,
            client_session_hint_source=hint_source,
            client_runtime_key=runtime_key,
            client_runtime_key_source=runtime_source,
            client_name=name,
        )

    def with_client_name(self, client_name: str | None) -> Provenance:
        """Re-resolve the runtime key once ``initialize`` names the client.

        An environment-supplied key still wins: the operator wrote it down on
        purpose, and a client naming itself is the weaker witness.
        """
        name = _clean(client_name)
        if name is None or name == self.client_name:
            return self
        if self.client_runtime_key_source in {None, "client_info.name"}:
            return Provenance(
                server_connection_id=self.server_connection_id,
                client_session_hint=self.client_session_hint,
                client_session_hint_source=self.client_session_hint_source,
                client_runtime_key=name,
                client_runtime_key_source="client_info.name",
                client_name=name,
            )
        return Provenance(
            server_connection_id=self.server_connection_id,
            client_session_hint=self.client_session_hint,
            client_session_hint_source=self.client_session_hint_source,
            client_runtime_key=self.client_runtime_key,
            client_runtime_key_source=self.client_runtime_key_source,
            client_name=name,
        )

    def to_dict(self) -> dict[str, Any]:
        """The full record, including how much each field is worth."""
        payload: dict[str, Any] = {}
        if self.server_connection_id:
            payload["server_connection_id"] = self.server_connection_id
            payload["server_connection_id_trust"] = SERVER_MINTED
        if self.client_session_hint:
            payload["client_session_hint"] = self.client_session_hint
            payload["client_session_hint_source"] = self.client_session_hint_source
            payload["client_session_hint_trust"] = HARNESS_ATTESTED
        if self.client_runtime_key:
            payload["client_runtime_key"] = self.client_runtime_key
            payload["client_runtime_key_source"] = self.client_runtime_key_source
        if self.client_name:
            payload["client_name"] = self.client_name
        return payload

    def is_empty(self) -> bool:
        return not self.to_dict()


EMPTY_PROVENANCE = Provenance()


def connection_provenance(
    session_state: dict[str, Any] | None,
    *,
    client_name: str | None = None,
) -> Provenance:
    """Return this connection's provenance, minting it on first use.

    ``session_state`` is the per-connection dict :func:`ocbrain.mcp.serve` keeps
    for the life of one stdio transport, so the id is stable for every call on
    that connection and different on the next one. A caller with no session
    state -- the CLI, a direct ``call_tool`` in a test -- is not a connection
    and gets the empty value rather than a fresh id per call, because a
    "connection id" that changes between two calls of the same process would be
    worse than none.
    """
    if session_state is None:
        return EMPTY_PROVENANCE
    existing = session_state.get("provenance")
    if isinstance(existing, Provenance):
        updated = existing.with_client_name(client_name or session_state.get("client_name"))
        if updated is not existing:
            session_state["provenance"] = updated
        return updated
    captured = Provenance.capture(client_name=client_name or session_state.get("client_name"))
    session_state["provenance"] = captured
    return captured


def _first_env(environ: Any, names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        value = _clean(environ.get(name))
        if value is not None:
            return value, f"env:{name}"
    return None, None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:256] if text else None


__all__ = [
    "EMPTY_PROVENANCE",
    "HARNESS_ATTESTED",
    "RUNTIME_KEY_ENV_VARS",
    "SERVER_MINTED",
    "SESSION_HINT_ENV_VARS",
    "Provenance",
    "connection_provenance",
]
