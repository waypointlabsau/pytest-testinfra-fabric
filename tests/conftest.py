"""Shared fixtures and the recording stand-in connection.

Two ways of driving a backend without a host appear across these tests, and
they are good at different things:

`MockRemote` (from `fabric[pytest]`) patches `paramiko.SSHClient` underneath a
real `fabric.Connection`, so it exercises the actual transfer and channel
plumbing -- worth it for `put_tar`/`stash`, which really do call `put()`.

`Recorder` replaces the connection outright and just remembers what it was
handed. That is what most tests here want: the behaviour under test IS the
command string, and asserting on the whole string in one piece gives a
readable diff when it is wrong.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fabric.testing.base import MockRemote
from invoke.runners import Result

from pytest_testinfra_fabric import backend as backend_module
from pytest_testinfra_fabric.backend import FabricBackend

# The template a default-prefix backend builds, spelled out rather than read
# off an instance, so a test asserting on the emitted `mktemp -d` is pinning
# the real string.
STASH_TEMPLATE = "/tmp/fabric.XXXXXXXX"

# What that `mktemp -d` is made to answer with. A literal rather than the
# template, because the whole point of mktemp is that the real path is not
# predictable.
STASH_DIR = "/tmp/fabric.a1b2c3d4"


def exited(code: int) -> Result:
    """A real invoke Result, not a stand-in: `.ok` is a property over `exited`,
    and the methods under test read it."""
    return Result(exited=code)


def out(stdout: str) -> Result:
    return Result(exited=0, stdout=stdout)


class Recorder:
    """A stand-in connection that records commands and replays canned results.

    Canned results are consumed in order; once they run out every command
    succeeds silently, so a test only has to spell out the answers it
    actually depends on -- but a test asserting on a LATER command's effect
    has to supply results for everything before it.
    """

    def __init__(self, results: list[Result] | None = None) -> None:
        self.commands: list[str] = []
        self.timeouts: list[object] = []
        self.closed = False
        self._results = list(results or [])

    def run(self, command: str, **kwargs: object) -> Result:
        self.commands.append(command)
        self.timeouts.append(kwargs.get("timeout"))
        if self._results:
            return self._results.pop(0)
        return exited(0)

    def close(self) -> None:
        self.closed = True

    @property
    def last(self) -> str:
        return self.commands[-1]


def recording(backend: FabricBackend, results: list[Result] | None = None) -> Recorder:
    """Give `backend` a connection that records instead of connecting.

    Assigns into the instance dict, which is where `functools.cached_property`
    keeps `connection` -- so this is indistinguishable from the connection
    having already been opened.
    """
    recorder = Recorder(results)
    backend.__dict__["connection"] = recorder
    return recorder


@pytest.fixture(autouse=True)
def no_leaked_stash_directory() -> Iterator[None]:
    """Keep the process-wide scratch directory from leaking between tests.

    `_stash_directory` is module state by design -- that is what makes
    `FabricBackend.STASH_DIRECTORY` readable without holding a backend -- so a
    test that creates one would make the next test's `stash_dir` assert
    instead of run.
    """
    backend_module._stash_directory = None
    yield
    backend_module._stash_directory = None


@pytest.fixture
def fabric_backend() -> FabricBackend:
    # Scheme-stripped, the same form testinfra's own backend.get_host()
    # passes to a backend's constructor -- "fabric://" is only ever present
    # in the --hosts= string a user types, never in what reaches __init__.
    return FabricBackend("user@host")


@pytest.fixture
def backend(fabric_backend: FabricBackend) -> FabricBackend:
    """Alias, for tests that read better without the package name in them."""
    return fabric_backend


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
def remote() -> Iterator[MockRemote]:
    mock_remote = MockRemote()
    yield mock_remote
    _check_commands_executed(mock_remote)
    mock_remote.stop()


@pytest.fixture
def sftp_remote() -> Iterator[MockRemote]:
    mock_remote = MockRemote(enable_sftp=True)
    yield mock_remote
    _check_commands_executed(mock_remote)
    mock_remote.stop()
