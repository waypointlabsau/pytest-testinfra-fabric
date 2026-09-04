"""Unit tests for FabricBackend against a mocked Connection/Runner, via
fabric[pytest]'s own testing helpers (`fabric.testing.base`) -- no reachable
host needed.

Each test drives a real `FabricBackend` instance; `MockRemote` patches
`paramiko.SSHClient` underneath it so `FabricBackend.connection` (a real
`fabric.Connection`) runs against mocked channels instead of a socket. See
conftest.py for that fixture and for the lighter-weight `Recorder`
alternative most other modules here use.
"""

from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY

import pytest
from fabric.testing.base import Command, Session
from invoke.exceptions import CommandTimedOut
from invoke.runners import Result
from paramiko.ssh_exception import SSHException

from conftest import STASH_DIR, STASH_TEMPLATE
from pytest_testinfra_fabric.backend import FabricBackend
from pytest_testinfra_fabric.local import free_port, local_port_open


def test_execute_returns_stdout(remote, fabric_backend):
    remote.expect(cmd="echo hi", out=b"hi\n")
    result = fabric_backend.execute("echo hi")
    assert result.stdout == "hi\n"


def test_execute_raises_on_nonzero_exit(remote, fabric_backend):
    remote.expect(cmd="false", exit=1)
    with pytest.raises(AssertionError, match="command failed"):
        fabric_backend.execute("false")


def test_execute_with_check_false_does_not_raise(remote, fabric_backend):
    remote.expect(cmd="false", exit=1)
    result = fabric_backend.execute("false", check=False)
    assert not result.ok


def test_run_returns_command_result_without_raising(remote, fabric_backend):
    remote.expect(cmd="false", exit=1)
    result = fabric_backend.run("false")
    assert result.rc == 1


def _breaks_with(fabric_backend: FabricBackend, error: BaseException) -> None:
    """Make the next command fail the way a dead transport does.

    Assigns into the instance dict rather than patching the class, because
    that is exactly where `functools.cached_property` stores `connection` --
    so this is the same thing as the connection having already been opened,
    and no MockRemote is involved at all.
    """
    def run(*args: object, **kwargs: object) -> None:
        raise error

    fabric_backend.__dict__["connection"] = SimpleNamespace(run=run)


# `paramiko.packet.Packetizer.read_all` raises an empty EOFError()
# when a socket read returns zero bytes
DEAD_TRANSPORTS = [
    SSHException("socket is closed"),
    OSError("Socket is closed"),
    EOFError(),
]
DEAD_TRANSPORT_IDS = ["sshexception", "oserror", "eoferror"]


@pytest.mark.parametrize("error", DEAD_TRANSPORTS, ids=DEAD_TRANSPORT_IDS)
def test_execute_reports_a_dead_connection_as_exit_255(fabric_backend, error):
    """With check=False a broken transport is an ordinary failed Result, not an
    exception -- that is what lets a predicate polled through eventually()
    retry instead of aborting the whole poll, and it is why a consumer never
    has to import paramiko to catch this itself."""
    _breaks_with(fabric_backend, error)

    result = fabric_backend.execute("true", check=False)

    assert result.exited == 255
    assert not result.ok
    # The type name rather than the message: a bare EOFError has no message,
    # so naming the class is the only thing that tells a reader which of these
    # happened -- which is why the reason is rendered with `!r`.
    assert type(error).__name__ in result.stderr


@pytest.mark.parametrize("error", DEAD_TRANSPORTS, ids=DEAD_TRANSPORT_IDS)
def test_execute_raises_on_a_dead_connection_when_checked(fabric_backend, error):
    _breaks_with(fabric_backend, error)

    with pytest.raises(AssertionError, match="connection lost running: true"):
        fabric_backend.execute("true")


@pytest.mark.parametrize("error", DEAD_TRANSPORTS, ids=DEAD_TRANSPORT_IDS)
def test_run_reports_a_dead_connection_as_exit_255(fabric_backend, error):
    """`run()` is testinfra's contract method, and testinfra callers read an
    exit status -- so a dead transport has to become a CommandResult here too,
    not an exception out of the middle of paramiko."""
    _breaks_with(fabric_backend, error)

    result = fabric_backend.run("true")

    assert result.rc == 255
    assert type(error).__name__ in result.stderr


def test_execute_reports_a_timeout_as_exit_255_when_unchecked(fabric_backend):
    """A timeout is governed by `check` for the same reason a dead connection
    is: `port_listening`-style predicates poll on a short bound and must read
    a slow command as "not yet", not as a raise."""
    _breaks_with(fabric_backend, CommandTimedOut(Result(exited=None), timeout=5))

    result = fabric_backend.execute("sleep 60", check=False, timeout=5)

    assert result.exited == 255
    assert "timed out after 5s" in result.stderr


def test_execute_raises_on_a_timeout_when_checked(fabric_backend):
    _breaks_with(fabric_backend, CommandTimedOut(Result(exited=None), timeout=5))

    with pytest.raises(AssertionError, match="timed out after 5s"):
        fabric_backend.execute("sleep 60", timeout=5)


@contextmanager
def _listening_on(port: int):
    """Stand in for what fabric's tunnel thread does: bind and listen.

    The backlog is deliberately generous. Nothing here ever calls `accept()`,
    so every readiness probe leaves a completed connection sitting in the
    queue -- with `listen(1)` the queue is full after the first one and the
    next probe is refused, which looks like the tunnel going down again.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.listen(16)
    try:
        yield
    finally:
        sock.close()


@contextmanager
def _never_binds(port: int):
    """A forward_local that returns without its thread ever getting there."""
    yield


def _forwarding_with(fabric_backend: FabricBackend, fake) -> list[dict]:
    """Swap in a connection whose forward_local defers to `fake`, and record
    the kwargs it was called with."""
    calls: list[dict] = []

    def forward_local(**kwargs):
        calls.append(kwargs)
        return fake(kwargs["local_port"])

    fabric_backend.__dict__["connection"] = SimpleNamespace(forward_local=forward_local)
    return calls


def test_forward_yields_once_the_local_port_accepts(fabric_backend):
    calls = _forwarding_with(fabric_backend, _listening_on)
    port = free_port()

    with fabric_backend.forward(port, 4222):
        assert local_port_open(port)

    assert calls == [
        {"local_port": port, "remote_port": 4222, "remote_host": "127.0.0.1", "local_host": "127.0.0.1"}
    ]


def test_forward_fails_when_the_tunnel_never_comes_up(fabric_backend):
    """The reason this wraps forward_local at all: fabric binds the listening
    socket on a thread AFTER the context manager yields, so without the wait
    the caller gets a client-side connect timeout instead of a clear failure
    naming the tunnel."""
    _forwarding_with(fabric_backend, _never_binds)
    port = free_port()

    with pytest.raises(AssertionError, match=f"the forward tunnel on :{port} never came up"):
        with fabric_backend.forward(port, 4222, timeout=1):
            pytest.fail("forward yielded despite nothing listening")


def test_no_method_is_shadowed_by_a_base_backend_attribute(fabric_backend):
    """testinfra's `BaseBackend.__init__` assigns instance attributes, which
    silently win over any method of the same name on this subclass.

    `hostname` was exactly that: `BaseBackend` sets it to the hostspec, so a
    `hostname()` method here became an un-callable string and only failed
    against a live host. Nothing callable on this class may be shadowed.
    """
    shadowed = [
        name
        for name in vars(type(fabric_backend))
        if not name.startswith("__")
        and callable(vars(type(fabric_backend))[name])
        and not callable(getattr(fabric_backend, name))
    ]

    assert not shadowed


def test_remote_hostname_asks_the_host_not_the_hostspec(remote, fabric_backend):
    """`self.hostname` is the ssh alias this backend was built from; the host's
    own idea of its name is a different string and has to be asked for."""
    remote.expect(cmd="hostname", out=b"waypoint-incus-vm\n")

    assert fabric_backend.remote_hostname() == "waypoint-incus-vm"
    assert fabric_backend.hostname == "host"


def test_mktemp_dir_returns_stripped_path(remote, fabric_backend):
    remote.expect(cmd="mktemp -d /tmp/foo.XXXXXX", out=b"/tmp/foo.abc123\n")
    path = fabric_backend.mktemp_dir("/tmp/foo.XXXXXX")
    assert path == "/tmp/foo.abc123"


def test_kill_pid_file_never_raises_on_missing_file(remote, fabric_backend):
    remote.expect(cmd='kill "$(cat /run/foo.pid 2>/dev/null)" 2>/dev/null; true')
    fabric_backend.kill_pid_file("/run/foo.pid")


def test_put_tar_extracts_uploaded_archive(sftp_remote, fabric_backend, tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "file.txt").write_text("hi")
    remote_dir = "/opt/deploy"

    sftp_remote.expect_sessions(
        Session(
            enable_sftp=True,
            commands=[Command(cmd=f"mkdir -p {remote_dir} && tar -xzf {remote_dir}.tar.gz -C {remote_dir} && rm -f {remote_dir}.tar.gz")],
            transfers=[dict(method="putfo", fl=ANY, remotepath=f"{remote_dir}.tar.gz")],
        )
    )

    fabric_backend.put_tar(source, remote_dir)


def test_stash_skips_copy_when_hash_matches(remote, fabric_backend, tmp_path: Path):
    local = tmp_path / "binary"
    local.write_bytes(b"payload")
    local_hash = fabric_backend._sha256(local)

    remote.expect_sessions(
        Session(
            commands=[
                Command(cmd=f"mktemp -d {STASH_TEMPLATE}", out=f"{STASH_DIR}\n".encode()),
                Command(cmd=f"sha256sum {STASH_DIR}/binary", out=f"{local_hash}  {STASH_DIR}/binary\n".encode()),
            ]
        )
    )

    remote_path = fabric_backend.stash(local, "binary")

    assert remote_path == f"{STASH_DIR}/binary"


def test_stash_raises_when_local_file_is_missing(fabric_backend, tmp_path: Path):
    """The hash is computed before any remote call is made, so a missing local
    file must fail before it ever opens a connection or creates a scratch
    directory -- no MockRemote is set up here, and none is needed."""
    missing = tmp_path / "does-not-exist"

    with pytest.raises(FileNotFoundError):
        fabric_backend.stash(missing, "binary")


def test_stash_copies_when_the_remote_file_is_absent(sftp_remote, fabric_backend, tmp_path: Path):
    """sha256sum failing (nonzero exit, no stdout) means nothing is there yet,
    not that hashing itself failed -- stash must still copy rather than
    raise. This is the ordinary case now that the directory is new each run."""
    local = tmp_path / "binary"
    local.write_bytes(b"payload")

    sftp_remote.expect_sessions(
        Session(
            enable_sftp=True,
            commands=[
                Command(cmd=f"mktemp -d {STASH_TEMPLATE}", out=f"{STASH_DIR}\n".encode()),
                Command(cmd=f"sha256sum {STASH_DIR}/binary", exit=1),
                Command(cmd=f"chmod +x {STASH_DIR}/binary"),
            ],
            transfers=[
                dict(method="put", localpath=f"/local/{os.path.normpath(str(local))}",
                     remotepath=f"{STASH_DIR}/binary")
            ],
        )
    )

    remote_path = fabric_backend.stash(local, "binary")

    assert remote_path == f"{STASH_DIR}/binary"


def test_stash_copies_when_hash_differs(sftp_remote, fabric_backend, tmp_path: Path):
    local = tmp_path / "binary"
    local.write_bytes(b"payload")

    sftp_remote.expect_sessions(
        Session(
            enable_sftp=True,
            commands=[
                Command(cmd=f"mktemp -d {STASH_TEMPLATE}", out=f"{STASH_DIR}\n".encode()),
                Command(cmd=f"sha256sum {STASH_DIR}/binary", out=b"deadbeef  /x/binary\n"),
                Command(cmd=f"chmod +x {STASH_DIR}/binary"),
            ],
            # MockRemote's SFTP support patches fabric.transfer.os.path.abspath
            # to prefix "/local/" (see fabric.testing.base.Session._start_sftp)
            # rather than leave it untouched, so the expected localpath must
            # go through that same fake abspath, not the raw local path.
            transfers=[
                dict(method="put", localpath=f"/local/{os.path.normpath(str(local))}",
                     remotepath=f"{STASH_DIR}/binary")
            ],
        )
    )

    remote_path = fabric_backend.stash(local, "binary")

    assert remote_path == f"{STASH_DIR}/binary"
