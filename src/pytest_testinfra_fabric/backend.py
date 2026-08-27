"""A testinfra backend backed by Fabric (fabric/fabric) instead of raw ssh or
testinfra's own paramiko backend -- registered under the `fabric` connection
scheme so `--hosts=fabric://<ssh-config-alias>` resolves to it.

The main reason this exists rather than reusing testinfra's built-in
`paramiko` backend: ParamikoBackend's own ssh_config handling
(`ParamikoBackend._load_ssh_config`) only understands an explicit
`ProxyCommand` line, not `ProxyJump` -- it has no case for the `proxyjump` key
`paramiko.SSHConfig.lookup()` already hands it. Fabric's own `Connection`
resolves `ProxyJump` (including multi-hop) automatically once given a bare
`~/.ssh/config` Host alias, which is exactly what `waypoint-incus` is here (see
fabric/fabric#1541).

This backend satisfies testinfra's `host.run()` contract for assertions, and
also exposes the file-transfer, tarball-extraction, and run-directory/PID-file
primitives an actual deployment needs (see remote_backend_server.py in a
consuming project) directly as methods here, since testinfra's Host API has no
equivalent for those.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

from fabric import Connection
from invoke.exceptions import CommandTimedOut, UnexpectedExit

from testinfra.backend import base

logger = logging.getLogger(__name__)


class FabricBackend(base.BaseBackend):
    NAME = "fabric"

    def __init__(
        self,
        hostspec: str,
        timeout: int = 15,
        connect_timeout: int | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.host = self.parse_hostspec(hostspec)
        self.timeout = int(timeout)
        self.connect_timeout = int(connect_timeout) if connect_timeout is not None else self.timeout
        super().__init__(self.host.name, *args, **kwargs)

    @functools.cached_property
    def connection(self) -> Connection:
        connect_kwargs: dict[str, Any] = {"look_for_keys": True, "allow_agent": True}
        if self.host.password:
            connect_kwargs["password"] = self.host.password
        conn = Connection(
            host=self.host.name,
            user=self.host.user,
            port=int(self.host.port) if self.host.port else None,
            connect_timeout=self.connect_timeout,
            connect_kwargs=connect_kwargs,
        )
        logger.debug("opening connection to %s@%s:%s", self.host.user, self.host.name, self.host.port)
        conn.open()
        logger.debug("connection to %s open", self.host.name)
        return conn

    def run(self, command: str, *args: str, **kwargs: Any) -> base.CommandResult:
        command = self.get_command(command, *args)
        cmd = self.encode(command)
        logger.debug("run: %s", command)
        try:
            # in_stream=False: no interactive input is ever sent through this
            # backend, and Invoke's default stdin-mirroring thread raises
            # under any capture-based runner (pytest included) -- the same
            # gotcha remote_backend_server.py's own Fabric calls work around.
            result = self.connection.run(command, hide=True, warn=True, in_stream=False)
        except CommandTimedOut as exc:
            logger.debug("run timed out after %ss: %s", self.timeout, command)
            raise TimeoutError(f"command timed out after {self.timeout}s: {command}") from exc
        logger.debug(
            "run exited %s: %s\nstdout: %s\nstderr: %s",
            result.exited,
            command,
            result.stdout,
            result.stderr,
        )
        return self.result(result.exited, cmd, result.stdout, result.stderr)

    def run_checked(self, command: str, *, timeout: float | None = None, check: bool = True):
        """`self.connection.run` with a mandatory timeout, turning a hang into
        a clear CommandTimedOut-derived failure instead of blocking forever.

        Deliberately separate from `run()` (the testinfra contract method,
        which returns a `base.CommandResult` and never raises): callers here
        want Fabric's own `Result` (for `.stdout`) and a raise on
        failure/timeout by default. `check`/`warn` are inverted: `warn=not
        check` so `check=True` (the default) still raises on a nonzero exit
        instead of silently continuing.
        """
        logger.debug("run_checked: %s", command)
        try:
            # in_stream=False: without it, Invoke spawns a thread that mirrors
            # this process's own stdin to the remote command, and under
            # pytest's output capture that thread's read raises "OSError:
            # reading from stdin while output is captured!" -- there is no
            # interactive input to forward here regardless.
            result = self.connection.run(
                command, hide=True, warn=not check, timeout=timeout or self.timeout, in_stream=False
            )
        except CommandTimedOut as exc:
            logger.debug("run_checked timed out after %ss: %s", timeout or self.timeout, command)
            raise AssertionError(f"command timed out after {timeout or self.timeout}s: {command}") from exc
        except UnexpectedExit as exc:
            logger.debug("run_checked failed: %s\nstderr: %s", command, exc.result.stderr)
            raise AssertionError(f"command failed: {command}\n{exc.result.stderr}") from exc
        logger.debug(
            "run_checked exited %s: %s\nstdout: %s\nstderr: %s",
            result.exited,
            command,
            result.stdout,
            result.stderr,
        )
        return result

    def put_tar(self, source: Path, remote_dir: str, *, excludes: set[str] = frozenset()) -> None:
        """tar `source` up in memory (skipping `excludes`), `put()` the single
        archive over this connection, then extract it remotely into
        `remote_dir`.

        This is Fabric's answer to shipping a directory over `rsync`: both are
        small in the deployments that use this, so a one-shot tar+put+extract
        costs nothing extra and needs no local `rsync` binary. A much larger,
        unchanging prebuilt tree (e.g. a Node.js install) is better served by
        `rsync` directly -- re-uploading tens of MB through this path on every
        deploy would be pure waste compared to rsync's own incremental
        behavior.
        """
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in source.rglob("*"):
                if path.is_dir():
                    continue
                if any(part in excludes for part in path.relative_to(source).parts):
                    continue
                tar.add(path, arcname=path.relative_to(source))
        buffer.seek(0)
        size = buffer.getbuffer().nbytes
        logger.debug("put_tar: %s -> %s (%d bytes)", source, remote_dir, size)

        remote_tar = f"{remote_dir}.tar.gz"
        self.connection.put(buffer, remote=remote_tar)
        self.run_checked(f"mkdir -p {remote_dir} && tar -xzf {remote_tar} -C {remote_dir} && rm -f {remote_tar}")

    def stash(self, local: Path, remote_dir: str, remote_name: str) -> str:
        """Put `local` at `remote_dir/remote_name` on this host, but only if
        what's already there doesn't hash the same as `local`.

        This is the persistent counterpart to `put_tar`: meant for content
        that a caller wants to survive across deployments -- a pinned release
        binary, a small fixture file -- rather than get removed with a single
        run's own directory, so `remote_dir` is typically a fixed path rather
        than one scoped to a deployment. Comparing content rather than mere
        presence is what makes re-use safe across a version or fixture bump
        too: a presence-only check would keep serving the old file forever.
        """
        remote_path = f"{remote_dir}/{remote_name}"
        local_hash = self._sha256(local)
        self.run_checked(f"mkdir -p {remote_dir}")
        existing = self.run_checked(f"sha256sum {remote_path}", check=False)
        remote_hash = existing.stdout.split()[0] if existing.ok and existing.stdout.strip() else None
        if remote_hash == local_hash:
            logger.debug("stash: %s already up to date at %s, skipping copy", remote_name, remote_path)
            return remote_path
        logger.debug("stash: copying %s -> %s", local, remote_path)
        self.connection.put(str(local), remote=remote_path)
        self.run_checked(f"chmod +x {remote_path}")
        return remote_path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def mktemp_dir(self, template: str) -> str:
        path = self.run_checked(f"mktemp -d {template}").stdout.strip()
        logger.debug("mktemp_dir: %s -> %s", template, path)
        return path

    def kill_pid_file(self, pid_file: str, *, check: bool = False) -> None:
        """Kill the PID recorded in `pid_file`, if any -- a missing or stale
        file is not an error, since teardown always wants to proceed."""
        logger.debug("kill_pid_file: %s", pid_file)
        self.run_checked(f'kill "$(cat {pid_file} 2>/dev/null)" 2>/dev/null; true', check=check)

    def kill_stale_pid_files(self, dir_glob: str, pid_filename: str) -> None:
        """For every stale run directory a hard-killed previous run left
        behind, kill the PID its `pid_filename` records, if the file is still
        there, before the directory itself is removed."""
        logger.debug("kill_stale_pid_files: %s/%s", dir_glob, pid_filename)
        self.run_checked(
            f'for d in {dir_glob}; do [ -f "$d/{pid_filename}" ] && '
            f'kill "$(cat "$d/{pid_filename}")" 2>/dev/null; done; true',
            check=False,
        )
