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
goes beyond it in the direction a suite that has to STAND UP a host needs:
file transfer, a per-run scratch directory, detached process launch and the
handle to signal it again, forward tunnels, and the handful of readers a
deployment checks its own progress against. testinfra's `Host` API has no
equivalent for any of those, so they are methods here, and a consumer
instantiates `FabricBackend` directly rather than going through the `host`
fixture.

Everything rides the one `Connection`, multiplexed as channels over it, and
everything carries a timeout: a target several hops away pays a full handshake
per connection, and a command whose channel never closes blocks the calling
process forever with nothing local to kill and no error to read.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import logging
import shlex
import subprocess
import tarfile
from abc import ABCMeta
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from fabric import Connection
from invoke.exceptions import CommandTimedOut, UnexpectedExit
from invoke.runners import Result
from paramiko.ssh_exception import SSHException

from testinfra.backend import base

from .local import local_port_open
from .waiting import eventually

logger = logging.getLogger(__name__)

# The scratch directory on the host, created by the first backend that needs
# one (see `FabricBackend.stash_dir`) and removed by that backend's `close()`.
#
# Per-run rather than a fixed path: it holds only what a single run put there,
# so a suite leaves nothing behind on the host and can never read something a
# previous run left in a state it did not expect. The cost is that anything
# put here is put again next run -- which is why `utility.download_binary`
# fetches on the host rather than uploading, so "again" costs the host's own
# bandwidth and not a transfer over a multi-hop connection.
#
# One per process, deliberately: it is reachable as
# `FabricBackend.STASH_DIRECTORY` precisely so nothing has to thread a backend
# through to name a path under it. Two backends open at once (or pytest-xdist)
# would collide, which `stash_dir` refuses rather than allows.
_stash_directory: str | None = None

# Names this package's own scratch directories when a consumer does not say
# otherwise. A consumer sharing a host with another user of this backend
# should pass its own, so each one's leftovers are distinguishable from the
# other's -- see `FabricBackend.stash_glob`.
DEFAULT_STASH_PREFIX = "fabric"

SAFE_PREFIX_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

# A cold download over whatever route the host has to the internet.
DOWNLOAD_TIMEOUT = 120


class _BackendMeta(ABCMeta):
    """Carries `STASH_DIRECTORY` as a class-level property.

    A metaclass rather than a plain class attribute so that reading it before
    anything has created a scratch directory raises with an explanation,
    instead of handing back a `None` that formats into a plausible-looking
    path like "None/ws_client.py" and fails somewhere else entirely.

    Derives from `ABCMeta` because that is what `testinfra.backend.base`
    already uses; a plain `type` here would be a metaclass conflict.
    """

    @property
    def STASH_DIRECTORY(cls) -> str:
        if _stash_directory is None:
            raise RuntimeError(
                "no scratch directory exists yet -- it is created by the first "
                "backend to touch `stash_dir` (or `utility.download_binary`), and "
                "removed again by that backend's close()"
            )
        return _stash_directory

# Bounds the readiness wait in `FabricBackend.forward`. Generous relative to
# what binding a socket on a thread actually costs, because the cost of being
# wrong here is a confusing client-side connect timeout rather than a clear
# failure.
FORWARD_TIMEOUT = 20

# Bounds a detached launch (see `FabricBackend.launch`). Short on purpose: the
# remote shell forks and returns immediately when the launch string is written
# correctly, so anything approaching this means it failed to detach and is
# still holding the channel open -- a failure in seconds instead of a hang.
LAUNCH_TIMEOUT = 25

# How long `RemoteProcess.wait` gives a signalled process to actually go.
EXIT_TIMEOUT = 30

# Lines `RemoteProcess.tail` and `FabricBackend.tail` read by default.
TAIL_LINES = 60


@dataclass(frozen=True)
class StaleScratch:
    """What an earlier run left behind: its scratch directories, and whatever
    is still running out of them.

    Returned by `FabricBackend.stale_scratch` (which only looks) and by
    `sweep_stale` (which acts). Falsey when there is nothing, so a caller can
    write `if stale:` and print `describe()` -- one rendering, shared by
    whoever refuses to run and whoever cleans up.
    """

    #: Directory paths matching the backend's `stash_glob`, minus its own.
    directories: list[str]
    #: Process group id -> the command lines under it that named one of those
    #: directories. Keyed by group because that is what gets signalled: a
    #: process and everything it forked go together.
    process_groups: dict[str, list[str]]

    def __bool__(self) -> bool:
        return bool(self.directories or self.process_groups)

    def describe(self) -> str:
        lines = []
        for pgid, commands in sorted(self.process_groups.items()):
            lines.append(f"  process group {pgid}:")
            lines.extend(f"    {command}" for command in commands)
        lines.extend(f"  directory {path}" for path in sorted(self.directories))
        return "\n".join(lines) or "  (nothing)"


@dataclass(frozen=True)
class RemoteProcess:
    """A process on the host that outlives the command which started it.

    Produced by `FabricBackend.launch` for something this session started, and
    by `FabricBackend.process` for something an earlier one left behind. Either
    way it is identified by the PID recorded in `pid_file` rather than by
    matching a command line: a `pkill -f` pattern broad enough to survive a
    real deployment's argv shapes risks matching more than the one process
    meant, and a run directory shared with an unrelated daemon makes that a
    live hazard rather than a theoretical one.
    """

    backend: FabricBackend
    pid_file: str
    log: str = "/dev/null"
    #: Whether the recorded PID is also a process group id, which it is for
    #: anything launched under `setsid`. Signalling the group rather than the
    #: bare PID is what reaches a `sudo`/wrapper/interpreter chain whose
    #: members do not reliably forward signals to each other.
    group: bool = True
    #: Whether signalling and reading this process needs `sudo`. Set it when
    #: the launched command elevates (`sudo …`), because the resulting process
    #: and its log are root-owned: an unprivileged `kill` fails with EPERM and
    #: does nothing, and an unprivileged `kill -0` reports the process as gone
    #: while it is still running.
    sudo: bool = False

    @property
    def _target(self) -> str:
        """The kill(1) argument naming this process, group-prefixed or not.

        Quoted rather than interpolated because the PID is read on the far
        side, out of the file, at signal time -- there is nothing local to
        substitute in.
        """
        pid = f'"$(cat {shlex.quote(self.pid_file)})"'
        return f"-{pid}" if self.group else pid

    @property
    def _prefix(self) -> str:
        return "sudo " if self.sudo else ""

    def kill(self, signal: str = "TERM") -> None:
        """Signal the process, tolerating a missing or stale pid file.

        Never raises and never checks: teardown always wants to proceed, and a
        run that was killed before it ever wrote the file is the ordinary case
        rather than an error.
        """
        logger.debug("kill -%s %s", signal, self.pid_file)
        self.backend.execute(
            f"{self._prefix}kill -{signal} {self._target} 2>/dev/null || true", check=False
        )

    def alive(self) -> bool:
        """Whether the process -- or, for a group, anything it forked -- still exists.

        `kill -0` on a PGID succeeds as long as ANY member of the group is
        still around, which is exactly the question worth asking: the recorded
        PID is only ever the group's original leader, but the group persists
        as a whole until every member has exited.
        """
        return self.backend.execute(
            f"{self._prefix}kill -0 {self._target} 2>/dev/null", check=False
        ).ok

    def wait(self, timeout: int = EXIT_TIMEOUT, interval: float = 0.5) -> bool:
        """Wait for a signalled process to actually exit, and report whether it did.

        A signal only asks. This returns a bool rather than raising because
        its caller is usually teardown, and a process that refuses to die must
        not replace the failure a test was already reporting with one of its
        own.
        """
        try:
            eventually(
                lambda: not self.alive(),
                timeout,
                f"the process recorded in {self.pid_file} did not exit after being signalled",
                interval=interval,
            )
            return True
        except AssertionError:
            logger.debug("wait: %s still alive after %ss", self.pid_file, timeout)
            return False

    def tail(self, lines: int = TAIL_LINES) -> str:
        """The tail of this process's own log.

        Worth reaching for on any failure that involved waiting for this
        process: its log says why far more often than a timeout message does.
        """
        return self.backend.tail(self.log, lines, sudo=self.sudo)

    def log_contains(self, text: str) -> bool:
        """Whether this process has logged `text` yet.

        Sometimes the only available evidence: a request a daemon declines to
        act on produces no state change to observe, so its own log is the only
        place that records which request it considered.
        """
        return self.backend.file_contains(self.log, text, sudo=self.sudo)


class FabricBackend(base.BaseBackend, metaclass=_BackendMeta):
    NAME = "fabric"

    def __init__(
        self,
        hostspec: str,
        timeout: int = 15,
        connect_timeout: int | None = None,
        stash_prefix: str = DEFAULT_STASH_PREFIX,
        sudo_cleanup: bool = False,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.host = self.parse_hostspec(hostspec)
        self.timeout = int(timeout)
        self.connect_timeout = int(connect_timeout) if connect_timeout is not None else self.timeout
        # Interpolated into both a `mktemp -d` template and an `rm -rf` glob,
        # so anything that isn't a plain path component has to fail here
        # rather than be quoted into something surprising later.
        assert stash_prefix and not set(stash_prefix) - set(SAFE_PREFIX_CHARS), (
            f"stash_prefix must be a single path component of {SAFE_PREFIX_CHARS!r}, "
            f"not {stash_prefix!r}"
        )
        self.stash_prefix = stash_prefix
        self.sudo_cleanup = sudo_cleanup
        super().__init__(self.host.name, *args, **kwargs)

    @property
    def stash_template(self) -> str:
        """The `mktemp -d` template this backend's scratch directory is made from."""
        return f"/tmp/{self.stash_prefix}.XXXXXXXX"

    @property
    def stash_glob(self) -> str:
        """Every scratch directory this backend's prefix could have created.

        What `stale_scratch` looks for and `sweep_stale` removes. A consumer
        that wants its leftovers distinguishable from another project's on the
        same host gives it its own `stash_prefix` -- two consumers sharing the
        default would each see the other's directories as debris.
        """
        return f"/tmp/{self.stash_prefix}.*"

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

    def execute(self, command: str, *, timeout: float | None = None, check: bool = True) -> Result:
        """`self.connection.run` with a mandatory timeout, collapsing every way
        a remote command can go wrong into one outcome the caller chose.

        Deliberately separate from `run()` (the testinfra contract method,
        which returns a `base.CommandResult` and never raises): callers here
        want Fabric's own `Result` (for `.stdout`) and a raise on failure by
        default. `check`/`warn` are inverted: `warn=not check` so `check=True`
        (the default) still raises on a nonzero exit instead of silently
        continuing.

        `check` governs all three failure modes, not just the exit code:

        - ``check=True`` raises ``AssertionError`` for a nonzero exit, a
          timeout, or a broken connection alike, so a caller that wants "this
          must work" writes one ``except`` and reads one message.
        - ``check=False`` never raises. A timeout or a dead connection comes
          back as an ordinary ``Result`` with ``exited=255`` -- the code ssh
          itself uses for its own failures -- with the reason in ``stderr``.
          That is what lets a predicate polled through ``waiting.eventually``
          see a failed command and retry, instead of aborting the whole poll
          on an exception it would have to special-case.

        A broken connection is translated here rather than left to the caller
        for the same reason the timeout is: a consumer would otherwise have to
        import paramiko itself to catch it, reaching around this backend to do
        so.
        """
        logger.debug("execute: %s", command)
        bound = timeout or self.timeout
        try:
            # in_stream=False: without it, Invoke spawns a thread that mirrors
            # this process's own stdin to the remote command, and under
            # pytest's output capture that thread's read raises "OSError:
            # reading from stdin while output is captured!" -- there is no
            # interactive input to forward here regardless.
            result = self.connection.run(
                command, hide=True, warn=not check, timeout=bound, in_stream=False
            )
        except CommandTimedOut as exc:
            logger.debug("execute timed out after %ss: %s", bound, command)
            message = f"command timed out after {bound}s: {command}"
            if check:
                raise AssertionError(message) from exc
            return Result(command=command, exited=255, stderr=message)
        except UnexpectedExit as exc:
            # Only reachable with check=True: warn=True returns the failed
            # Result below instead of raising.
            logger.debug("execute failed: %s\nstderr: %s", command, exc.result.stderr)
            raise AssertionError(f"command failed: {command}\n{exc.result.stderr}") from exc
        except (OSError, SSHException) as exc:
            logger.debug("execute lost the connection: %s\n%s", command, exc)
            message = f"connection lost running: {command}\n{exc}"
            if check:
                raise AssertionError(message) from exc
            return Result(command=command, exited=255, stderr=message)
        logger.debug(
            "execute exited %s: %s\nstdout: %s\nstderr: %s",
            result.exited,
            command,
            result.stdout,
            result.stderr,
        )
        return result

    def launch(
        self,
        command: str,
        *,
        pid_file: str,
        log: str = "/dev/null",
        cwd: str | None = None,
        extra_paths: Sequence[str] = (),
        detach: str = "setsid",
        sudo: bool = False,
        timeout: float = LAUNCH_TIMEOUT,
    ) -> RemoteProcess:
        """Start something on the host that outlives the command starting it,
        and record its PID so it can be signalled later.

        Every part of the emitted line is load-bearing, and each was
        established the hard way against a real host:

        `exec` inside the `sh -c` replaces that shell's process image with
        `command`'s rather than forking a child for it, so the `$!` captured
        immediately after backgrounding is the real process's own PID and not
        a wrapper's. Pass `cwd` instead of writing `cd … && …` yourself: this
        builds the compound in the one order where `exec` still applies to the
        right thing.

        The redirections are not just about keeping the log. What gets
        backgrounded must be a SINGLE SIMPLE COMMAND with none of its own
        descriptors left pointing at the channel this runs on, or that channel
        stays open for as long as the process lives -- which, for a daemon, is
        forever. Backgrounding a compound `cd … && …` list directly makes the
        shell fork a subshell whose stdio is not redirected away until partway
        through, and the channel then hangs for the full timeout even though
        the process itself started fine.

        `detach="setsid"` (the default) puts the process in a fresh process
        group of its own, with the launched leader's PID doubling as the PGID,
        instead of inheriting the SSH session's. That is what makes a later
        group kill possible: without it, signalling the group either hits
        nothing (wrong PGID) or the whole SSH session (right PGID, wrong
        scope). `detach="nohup"` is available for a process that genuinely
        wants no group of its own, and the returned handle then signals a bare
        PID.

        `extra_paths` is prepended to PATH for the launched process, because a
        non-interactive remote shell sources no rc files -- anything installed
        outside the default PATH is not otherwise reachable by name from here.
        It defaults to empty: callers say what they want on the PATH.

        `timeout` is short by design (see LAUNCH_TIMEOUT): a correct launch
        returns as soon as the shell forks, so approaching it means the
        detachment failed and the channel is still held open.
        """
        assert detach in ("setsid", "nohup"), f"unknown detach mode {detach!r}"

        inner = f"exec {command}"
        if cwd is not None:
            inner = f"cd {shlex.quote(cwd)} && {inner}"
        prefix = f'export PATH="{":".join(extra_paths)}:$PATH"; ' if extra_paths else ""
        # `disown` is belt-and-braces alongside setsid/nohup -- it drops the
        # job from the shell's own table so no SIGHUP is sent on the way out.
        # `true` keeps the whole line's exit status independent of it.
        line = (
            f"{prefix}{detach} sh -c {shlex.quote(inner)} "
            f"< /dev/null >> {shlex.quote(log)} 2>&1 & "
            f"echo $! > {shlex.quote(pid_file)}; disown; true"
        )
        logger.debug("launch: %s", line)
        self.execute(line, timeout=timeout)
        return RemoteProcess(
            backend=self, pid_file=pid_file, log=log, group=detach == "setsid", sudo=sudo
        )

    def stale_scratch(self) -> StaleScratch:
        """Find what an earlier run left behind. Looks only -- changes nothing.

        A process is stale if its command line names a path under this
        backend's `stash_glob`. That is a better handle than a pid file for
        two reasons: a run killed between launching a process and the shell
        recording its PID leaves the process running and no pid file at all,
        and a stale pid file can name a PID the kernel has since recycled onto
        something unrelated -- which a pid-file sweep would then signal.

        Matching happens HERE, not in the remote shell. A `ps | grep <glob>`
        on the far side matches its own pipeline, whose argv contains the
        pattern, so it would read its own process group id and signal the
        SSH session running it. Bringing the listing back sidesteps that
        entirely: the `ps` command carries no marker.

        `-ww` is load-bearing: without it `ps` truncates long command lines to
        terminal width, which is exactly where the path being matched sits.

        This backend's own directory is excluded, so this is safe to call at
        any point rather than only before one exists.

        What it cannot see: a process that neither names a scratch path nor
        shares a process group with one that does. `socat`/`ptyd`-style relay
        children started with their own session are the real case -- they hold
        only whatever ports they were given, so they are pollution rather than
        breakage, but they will not appear here.
        """
        own = _stash_directory
        directories = [
            line
            for line in self.execute(f"ls -d {self.stash_glob} 2>/dev/null", check=False)
            .stdout.split()
            if line != own
        ]

        groups: dict[str, list[str]] = {}
        marker = self.stash_glob.removesuffix("*")
        for line in self.execute("ps -ww -eo pgid=,args=", check=False).stdout.splitlines():
            pgid, _, command = line.strip().partition(" ")
            if marker not in command or (own is not None and own in command):
                continue
            groups.setdefault(pgid, []).append(command.strip())

        stale = StaleScratch(directories=directories, process_groups=groups)
        if stale:
            logger.debug("stale_scratch found:\n%s", stale.describe())
        return stale

    def sweep_stale(self) -> StaleScratch:
        """Kill what `stale_scratch` found and remove its directories.

        The destructive half, deliberately separate: signalling a process
        group as root, inferred from debris, is not something a caller should
        be able to do by accident. Returns what it acted on.

        One signal per process group rather than per process -- the group is
        what reaches a `sudo`/wrapper/interpreter chain whose members do not
        forward signals to each other, and everything the leader forked.
        """
        stale = self.stale_scratch()
        for pgid in sorted(stale.process_groups):
            logger.debug("sweep_stale: killing process group %s", pgid)
            self.execute(f'sudo kill -TERM -"{pgid}" 2>/dev/null || true', check=False)
        if stale.directories:
            logger.debug("sweep_stale: removing %s", " ".join(stale.directories))
            self.execute(f"sudo rm -rf {self.stash_glob}", check=False)
        return stale

    def process(self, pid_file: str, *, group: bool = True, sudo: bool = False) -> RemoteProcess:
        """Adopt a process this session did not start, from its pid file alone.

        The second way to get a `RemoteProcess`, and what makes a startup
        sweep expressible: a run killed hard leaves its daemon running and its
        pid file behind, and this is the only handle on it that remains.
        Nothing is verified here -- a pid file that is missing, stale, or names
        a process that has already gone is the normal case, and every method
        on the handle tolerates it.
        """
        return RemoteProcess(backend=self, pid_file=pid_file, group=group, sudo=sudo)

    # ------------------------------------------------------------- evidence

    def port_listening(self, port: int) -> bool:
        """Whether anything on the host is in LISTEN on `port`.

        Reads `ss` rather than trying to connect, so it says nothing about
        reachability from anywhere else -- which is the point when the thing
        being checked binds loopback only.
        """
        return self.execute(
            f"ss -ltn | awk '$4 ~ /:{port}$/ {{ found = 1 }} END {{ exit !found }}'", check=False
        ).ok

    def tail(self, path: str, lines: int = TAIL_LINES, *, sudo: bool = False) -> str:
        """The last `lines` of a file on the host, or "" if it cannot be read."""
        prefix = "sudo " if sudo else ""
        return self.execute(
            f"{prefix}tail -n {lines} {shlex.quote(path)}", check=False
        ).stdout

    def file_contains(self, path: str, text: str, *, sudo: bool = False) -> bool:
        """Whether `path` holds `text` as a literal substring.

        `grep -F`, never a pattern: callers are checking for log lines and
        messages, where a regex metacharacter in the text would silently
        change what is being asked.
        """
        prefix = "sudo " if sudo else ""
        return self.execute(
            f"{prefix}grep -F -q {shlex.quote(text)} {shlex.quote(path)}", check=False
        ).ok

    def http_status(self, host: str, port: int, path: str = "/", *, timeout: int = 10) -> str:
        """The HTTP status code a request from the host itself gets back.

        Probed ON THE HOST deliberately. Checking readiness through a local
        forward instead proves nothing about the service: the tunnel accepts
        as soon as its socket is bound, and a stale process left over from an
        earlier run answers just as happily as the one just deployed -- which
        is exactly how a botched deployment can go unnoticed.
        """
        return self.execute(
            f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {timeout} "
            f"http://{host}:{port}{path}",
            check=False,
        ).stdout.strip()

    def remote_hostname(self) -> str:
        """What the host calls itself, asked on the host.

        NOT `self.hostname`, which testinfra's `BaseBackend` sets to the
        hostspec this backend was constructed with -- an `~/.ssh/config` alias
        like `waypoint-incus`. The two are unrelated strings, and a caller
        comparing against something a process on the host reported about
        itself needs this one.
        """
        return self.execute("hostname").stdout.strip()

    def ipv4_addresses(self) -> list[str]:
        """Every IPv4 address the host itself owns."""
        return self.execute(
            "ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1"
        ).stdout.split()

    @contextmanager
    def forward(
        self,
        local_port: int,
        remote_port: int,
        *,
        remote_host: str = "127.0.0.1",
        local_host: str = "127.0.0.1",
        timeout: int = FORWARD_TIMEOUT,
    ) -> Iterator[None]:
        """Give this process a route to a port on the host, and yield only once
        it is usable.

        `Connection.forward_local` is the in-process equivalent of `ssh -O
        forward -L`: it runs the accept loop on a thread of its own and carries
        each accepted connection as a `direct-tcpip` channel on the connection
        already open, so nothing here needs an `ssh` binary or a second
        handshake.

        The readiness wait is not optional, and it is the whole reason this
        wraps `forward_local` rather than callers using it directly: that
        thread binds the listening socket AFTER the context manager has
        yielded (see fabric/tunnels.py), so on return the port genuinely is
        not up yet. Without the wait the symptom is not an error but a client
        that times out connecting to a tunnel it was told existed.
        """
        with self.connection.forward_local(
            local_port=local_port,
            remote_port=remote_port,
            remote_host=remote_host,
            local_host=local_host,
        ):
            eventually(
                lambda: local_port_open(local_port),
                timeout,
                f"the forward tunnel on :{local_port} never came up",
            )
            yield

    def put_tar(self, source: Path, remote_dir: str, *, excludes: set[str] = frozenset()) -> None:
        """tar `source` up in memory (skipping `excludes`), `put()` the single
        archive over this connection, then extract it remotely into
        `remote_dir`.

        This is Fabric's answer to shipping a directory over `rsync`: both are
        small in the deployments that use this, so a one-shot tar+put+extract
        costs nothing extra and needs no local `rsync` binary. A much larger,
        unchanging prebuilt tree (e.g. a Node.js install) is better served by
        `put_rsync` -- re-uploading tens of MB through this path on every
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
        self.execute(f"mkdir -p {remote_dir} && tar -xzf {remote_tar} -C {remote_dir} && rm -f {remote_tar}")

    @functools.cached_property
    def stash_dir(self) -> str:
        """This run's scratch directory on the host, created on first use.

        Also publishes itself as `FabricBackend.STASH_DIRECTORY`, so a caller
        can name a path under it without holding a backend (see that
        property, and `_stash_directory`'s comment for why it is per-run).

        Refuses rather than overwrites when another live backend already owns
        one: a second value would leave the first backend's files reachable
        only through the object that made them, and silently un-cleaned when
        the wrong backend closed.
        """
        global _stash_directory
        assert _stash_directory is None, (
            f"another live backend already owns {_stash_directory}; close it before "
            "opening a second, or give this one no scratch directory"
        )
        path = self.mktemp_dir(self.stash_template)
        _stash_directory = path
        return path

    def stash(self, local: Path, remote_name: str) -> str:
        """Put `local` into this run's scratch directory as `remote_name`, but
        only if what's already there doesn't hash the same.

        The counterpart to `put_tar` for a single file rather than a tree. The
        hash compare rarely saves a transfer now that the directory is fresh
        every run -- it short-circuits on the first call -- but it keeps a
        repeated stash of the same name free, and comparing content rather
        than mere presence is what would keep re-use correct if the directory
        were ever made to persist: a presence-only check serves the old file
        forever across a version bump.

        For something fetched from a URL, use `utility.download_binary`
        instead: it lands in the same scratch directory without the bytes
        travelling through this connection at all.
        """
        # Hashed before anything remote happens, so a missing or unreadable
        # local file fails as itself rather than after opening a connection
        # and creating a scratch directory for a copy that cannot happen.
        local_hash = self._sha256(local)
        remote_path = f"{self.stash_dir}/{remote_name}"
        existing = self.execute(f"sha256sum {remote_path}", check=False)
        remote_hash = existing.stdout.split()[0] if existing.ok and existing.stdout.strip() else None
        if remote_hash == local_hash:
            logger.debug("stash: %s already up to date at %s, skipping copy", remote_name, remote_path)
            return remote_path
        logger.debug("stash: copying %s -> %s", local, remote_path)
        self.connection.put(str(local), remote=remote_path)
        self.execute(f"chmod +x {remote_path}")
        return remote_path

    def put_rsync(self, source: str, destination: str, *, timeout: float = DOWNLOAD_TIMEOUT) -> None:
        """Copy a large tree with the real `rsync`, NOT over this connection.

        The complement `put_tar`'s own docstring points at: a big unchanging
        prebuilt tree (a Node.js install, say) re-tarred and re-uploaded on
        every deploy is pure waste next to rsync's incremental behaviour.

        Unlike everything else here this shells out locally, so it needs an
        `rsync` binary on this machine and pays its own connection handshake
        rather than riding the open one. `destination` is resolved against
        `self.host.name`, which means `~/.ssh/config`'s `ProxyJump` still
        applies -- ssh resolves it the same way Fabric does.
        """
        target = f"{self.host.name}:{destination}"
        logger.debug("put_rsync: %s -> %s", source, target)
        try:
            result = subprocess.run(
                ["rsync", "-az", "-e", "ssh -o BatchMode=yes", source, target],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise AssertionError("put_rsync needs an rsync binary on this machine") from exc
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"rsync {source} -> {target} timed out after {timeout}s") from exc
        assert result.returncode == 0, f"rsync {source} -> {target} failed:\n{result.stderr}"

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def mktemp_dir(self, template: str) -> str:
        path = self.execute(f"mktemp -d {template}").stdout.strip()
        logger.debug("mktemp_dir: %s -> %s", template, path)
        return path

    def kill_pid_file(self, pid_file: str, *, check: bool = False) -> None:
        """Kill the PID recorded in `pid_file`, if any -- a missing or stale
        file is not an error, since teardown always wants to proceed."""
        logger.debug("kill_pid_file: %s", pid_file)
        self.execute(f'kill "$(cat {pid_file} 2>/dev/null)" 2>/dev/null; true', check=check)

    def kill_stale_pid_files(self, dir_glob: str, pid_filename: str) -> None:
        """For every stale run directory a hard-killed previous run left
        behind, kill the PID its `pid_filename` records, if the file is still
        there, before the directory itself is removed.

        Signals a bare, unprivileged PID. For a root-owned process, or one
        launched under `setsid` whose whole group needs the signal, adopt each
        one with `process()` instead and call `kill()` on the handle.
        """
        logger.debug("kill_stale_pid_files: %s/%s", dir_glob, pid_filename)
        self.execute(
            f'for d in {dir_glob}; do [ -f "$d/{pid_filename}" ] && '
            f'kill "$(cat "$d/{pid_filename}")" 2>/dev/null; done; true',
            check=False,
        )

    def close(self) -> None:
        """Remove this run's scratch directory and close the connection.

        Idempotent, and safe to call on a backend that never opened either --
        a deployment closes its backend on the way out of a failure as well as
        a success, and the failure may have happened before anything was
        created.

        The scratch directory goes first, because removing it is a command and
        the connection is what carries one. Only the backend that created the
        directory removes it, so closing a second backend never takes another
        one's files with it.

        `sudo_cleanup` (set at construction) is what makes the removal work
        when something running as root wrote into the directory: an
        unprivileged `rm -rf` cannot remove root-owned files, and would leave
        the directory behind for the next run to trip over.
        """
        global _stash_directory
        owned = self.__dict__.pop("stash_dir", None)
        if owned is not None:
            logger.debug("close: removing scratch directory %s", owned)
            prefix = "sudo " if self.sudo_cleanup else ""
            self.execute(f"{prefix}rm -rf {shlex.quote(owned)}", check=False)
            if _stash_directory == owned:
                _stash_directory = None
        if "connection" in self.__dict__:
            with contextlib.suppress(OSError, SSHException):
                self.connection.close()
