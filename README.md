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

`FabricBackend` (`src/pytest_testinfra_fabric/backend.py`) also exposes file
transfer, tarball extraction, and run-directory/PID-file primitives a real
deployment needs — `put_tar`, `stash`, `mktemp_dir`, `kill_pid_file`,
`kill_stale_pid_files` — directly as methods, since testinfra's `Host` API has
no equivalent for those. A consumer that wants them can instantiate
`FabricBackend` directly rather than going through testinfra's `host` fixture.

## Development

```bash
uv sync
uv run pytest
```

The test suite (`tests/test_backend.py`) exercises `FabricBackend` against the
mocked `Connection`/`Runner` fixtures from `fabric[pytest]`
(`fabric.testing.fixtures`) — unrelated to `pytest-testinfra`, which stays the
runtime dependency providing the `host.*` assertion DSL this package plugs
into; `fabric[pytest]` only helps test this package's own code without a live
host.
