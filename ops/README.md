# Operations artifacts

OCBrain installs only the on-demand local MCP launcher. It does not install
recurring launchd work; an operator who wants a scheduled loop opts in
explicitly (see `docs/SCHEDULED_MAINTENANCE.md`).

`hooks/pre-push` is the tracked git hook that runs
`ocbrain public-safety-check` over the outgoing commit range and blocks a push
that would carry private paths, denylisted identifiers, or new secrets into the
public repo. Install it with `ocbrain install-hooks` or
`scripts/install-hooks.sh`.

The three retired `com.jonathangu.ocbrain.*.plist` placeholders are gone. They
named the autopilot and stall-diagnostic loops, both deleted in v2 along with
the code they pointed at; every table those loops wrote was empty. An operator
upgrading from a legacy install should still unload and delete any
`com.jonathangu.ocbrain.*` agent left in `~/Library/LaunchAgents`.
