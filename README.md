# pytest-testinfra-fabric

A [pytest-testinfra](https://testinfra.readthedocs.io/) backend implemented
with [Fabric](https://www.fabfile.org/) instead of testinfra's own paramiko
backend, registered under the `fabric` connection scheme:

```bash
pytest --hosts=fabric://<ssh-config-alias>
```

Once this package is installed, `--hosts=fabric://...` resolves without any
project needing to import anything — the registration
(`testinfra.backend.BACKENDS["fabric"] = ...`) happens through a `pytest11`
entry point (`pytest_testinfra_fabric.plugin`) that pytest loads automatically
for every session that has the package installed.

## Why not testinfra's built-in `paramiko` backend?

`ParamikoBackend`'s own `ssh_config` handling only understands an explicit
`ProxyCommand` line, not `ProxyJump` — it has no case for the `proxyjump` key
`paramiko.SSHConfig.lookup()` already hands it (see
[fabric/fabric#1541](https://github.com/fabric/fabric/issues/1541)). Fabric's
own `Connection` resolves `ProxyJump`, including multi-hop, directly from a
bare `~/.ssh/config` `Host` alias, so a target reached through a jump host
works without any extra configuration.

## Beyond the testinfra contract

testinfra's `Host` API is built for *asserting* about a host that already
exists. A test suite that has to **stand one up** needs a layer underneath
that — transfer files, launch a daemon that outlives the command starting it,
kill it again, open a tunnel to it — and `Host` has no equivalent for any of
it. So `FabricBackend` exposes those directly as methods, and a consumer that
wants them instantiates `FabricBackend` itself rather than going through
testinfra's `host` fixture.

Everything below rides the one `Connection`, multiplexed as channels over it.
That matters for a target behind a `ProxyJump`: a fresh connection per command
would pay a full multi-hop handshake each time, and a suite that polls once a
second pays it hundreds of times.

### Running commands

`execute()` is the backend's own command verb, alongside the `run()` that
satisfies the testinfra contract. It returns Fabric's own `Result` and always
carries a timeout — an unbounded remote command whose channel never closes
blocks the calling process forever, with nothing local to kill and no error to
read.

Its `check` flag governs *every* way a command can fail, not just the exit
code:

| | nonzero exit | timeout | dead connection |
|---|---|---|---|
| `check=True` (default) | `AssertionError` | `AssertionError` | `AssertionError` |
| `check=False` | `Result`, real exit code | `Result`, `exited=255` | `Result`, `exited=255` |

The `check=False` row is what makes a predicate safe to poll: a lost
connection reads as "not yet" and gets retried, instead of aborting the poll
with an exception the caller would have to import paramiko to catch. `255` is
the code ssh itself uses for its own failures, and the reason lands in
`stderr` either way.

### Files

- `put_tar(source, remote_dir)` — tar a directory up in memory, `put` the one
  archive, extract it remotely. Fabric's answer to `rsync` for a small,
  changing tree, and it needs no `rsync` binary locally. A large unchanging
  prebuilt tree is still better served by real `rsync`.
- `stash(local, remote_dir, remote_name)` — put a file only if what's already
  there doesn't hash the same, for content meant to outlive one deployment.
  Comparing content rather than mere presence is what keeps re-use safe across
  a version or fixture bump; a presence-only check would serve the old file
  forever.
- `mktemp_dir(template)` — a remote scratch directory.

### Processes

- `kill_pid_file(pid_file)` — signal the PID a file records; a missing or
  stale file is not an error, since teardown always wants to proceed.
- `kill_stale_pid_files(dir_glob, pid_filename)` — the same for every run
  directory a hard-killed previous run left behind.

### Tunnels

`forward(local_port, remote_port)` is a context manager giving the local
process a route to a port on the host, and it yields only once that route
actually works. The readiness wait is the reason it exists rather than callers
using `Connection.forward_local` directly: fabric binds the listening socket
on a thread *after* its context manager yields, so on return the port is not
up yet. Skipping the wait produces no error — just a client that mysteriously
times out connecting to a tunnel it was told existed.

### Waiting, and the local side

Two modules that never touch the connection, exported because everything above
that waits is built on them and a consumer needs the same shapes for its own
predicates:

- `waiting.eventually(predicate, timeout, message)` — poll until truthy and
  **return the predicate's own value**, so a caller that polled for a thing
  doesn't read it a second time (a race over a network). On timeout it names
  the last value it saw, because "did not happen within 60s" alone says
  nothing about what *was* happening.
- `waiting.steadily(predicate, seconds)` — whether something holds for a whole
  window. The only way to show that nothing happened, when the thing you are
  checking for produces no event to wait on.
- `local.free_port()` / `local.local_port_open(port)` — for the near end of a
  tunnel. Note `local_port_open` says nothing about the far end: a forward
  accepts as soon as its socket is bound, whether or not anything answers
  behind it.

## Development

```bash
uv sync
uv run pytest
```

`tests/test_backend.py` exercises `FabricBackend` against the mocked
`Connection`/`Runner` fixtures from `fabric[pytest]` (`fabric.testing.base`) —
unrelated to `pytest-testinfra`, which stays the runtime dependency providing
the `host.*` assertion DSL this package plugs into; `fabric[pytest]` only
helps test this package's own code without a live host. `tests/test_waiting.py`
and `tests/test_local.py` need neither, being pure time-and-socket functions.

Assertions are mostly on the **exact command string** a method emits, because
that is where the behaviour lives: a wrong redirect in a detached launch, or a
missing `-` before a PGID, is invisible to any coarser check and only shows up
against a real host.
