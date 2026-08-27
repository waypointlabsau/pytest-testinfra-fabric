"""Unit tests for FabricBackend against a mocked Connection/Runner, via
fabric[pytest]'s own testing helpers (`fabric.testing.base`) -- no reachable
host needed.

Each test drives a real `FabricBackend` instance; `MockRemote` patches
`paramiko.SSHClient` underneath it so `FabricBackend.connection` (a real
`fabric.Connection`) runs against mocked channels instead of a socket.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import ANY

import pytest
from fabric.testing.base import Command, MockRemote, Session

from pytest_testinfra_fabric.backend import FabricBackend


@pytest.fixture
def fabric_backend() -> FabricBackend:
    # Scheme-stripped, the same form testinfra's own backend.get_host()
    # passes to a backend's constructor -- "fabric://" is only ever present
    # in the --hosts= string a user types, never in what reaches __init__.
    return FabricBackend("user@host")


def _check_commands_executed(mock_remote: MockRemote) -> None:
    """The same per-command/per-transfer verification `MockRemote.safety()`
    does, minus its `Connection.connect()` kwargs assertion.

    That assertion hardcodes an exact match against only
    username/hostname/port, but `FabricBackend` always adds its own
    `connect_kwargs` (`look_for_keys`, `allow_agent`) and a connect timeout --
    both of which `fabric.Connection.open()` legitimately forwards to
    `SSHClient.connect()` on top of those three -- so it fails here for a
    reason unrelated to the backend's correctness.
    """
    for session in mock_remote.sessions:
        for channel, command in zip(session.channels, session.commands):
            command.expect_execution(channel=channel)
        for transfer in session.transfers or []:
            method_name = transfer.pop("method")
            getattr(session.sftp, method_name).assert_any_call(**transfer)


@pytest.fixture
def remote():
    mock_remote = MockRemote()
    yield mock_remote
    _check_commands_executed(mock_remote)
    mock_remote.stop()


@pytest.fixture
def sftp_remote():
    mock_remote = MockRemote(enable_sftp=True)
    yield mock_remote
    _check_commands_executed(mock_remote)
    mock_remote.stop()


def test_run_checked_returns_stdout(remote, fabric_backend):
    remote.expect(cmd="echo hi", out=b"hi\n")
    result = fabric_backend.run_checked("echo hi")
    assert result.stdout == "hi\n"


def test_run_checked_raises_on_nonzero_exit(remote, fabric_backend):
    remote.expect(cmd="false", exit=1)
    with pytest.raises(AssertionError, match="command failed"):
        fabric_backend.run_checked("false")


def test_run_checked_with_check_false_does_not_raise(remote, fabric_backend):
    remote.expect(cmd="false", exit=1)
    result = fabric_backend.run_checked("false", check=False)
    assert not result.ok


def test_run_returns_command_result_without_raising(remote, fabric_backend):
    remote.expect(cmd="false", exit=1)
    result = fabric_backend.run("false")
    assert result.rc == 1


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
                Command(cmd="mkdir -p /opt/bin"),
                Command(cmd="sha256sum /opt/bin/binary", out=f"{local_hash}  /opt/bin/binary\n".encode()),
            ]
        )
    )

    remote_path = fabric_backend.stash(local, "/opt/bin", "binary")

    assert remote_path == "/opt/bin/binary"


def test_stash_raises_when_local_file_is_missing(fabric_backend, tmp_path: Path):
    """The hash is computed before any remote call is made, so a missing
    local file must fail before it ever opens a connection -- no MockRemote
    is set up here, and none is needed."""
    missing = tmp_path / "does-not-exist"

    with pytest.raises(FileNotFoundError):
        fabric_backend.stash(missing, "/opt/bin", "binary")


def test_stash_copies_when_the_remote_file_is_absent(sftp_remote, fabric_backend, tmp_path: Path):
    """sha256sum failing (nonzero exit, no stdout) means nothing is there yet,
    not that hashing itself failed -- stash must still copy rather than
    raise."""
    local = tmp_path / "binary"
    local.write_bytes(b"payload")

    sftp_remote.expect_sessions(
        Session(
            enable_sftp=True,
            commands=[
                Command(cmd="mkdir -p /opt/bin"),
                Command(cmd="sha256sum /opt/bin/binary", exit=1),
                Command(cmd="chmod +x /opt/bin/binary"),
            ],
            transfers=[
                dict(method="put", localpath=f"/local/{os.path.normpath(str(local))}", remotepath="/opt/bin/binary")
            ],
        )
    )

    remote_path = fabric_backend.stash(local, "/opt/bin", "binary")

    assert remote_path == "/opt/bin/binary"


def test_stash_copies_when_hash_differs(sftp_remote, fabric_backend, tmp_path: Path):
    local = tmp_path / "binary"
    local.write_bytes(b"payload")

    sftp_remote.expect_sessions(
        Session(
            enable_sftp=True,
            commands=[
                Command(cmd="mkdir -p /opt/bin"),
                Command(cmd="sha256sum /opt/bin/binary", out=b"deadbeef  /opt/bin/binary\n"),
                Command(cmd="chmod +x /opt/bin/binary"),
            ],
            # MockRemote's SFTP support patches fabric.transfer.os.path.abspath
            # to prefix "/local/" (see fabric.testing.base.Session._start_sftp)
            # rather than leave it untouched, so the expected localpath must
            # go through that same fake abspath, not the raw local path.
            transfers=[
                dict(method="put", localpath=f"/local/{os.path.normpath(str(local))}", remotepath="/opt/bin/binary")
            ],
        )
    )

    remote_path = fabric_backend.stash(local, "/opt/bin", "binary")

    assert remote_path == "/opt/bin/binary"
