"""Unit tests for finding, and then sweeping, what an earlier run left behind.

The selection in `stale_scratch` is the highest-consequence code in this
package: what it returns is what `sweep_stale` signals, as a process GROUP.
So it is driven here against canned `ps` output rather than against a host,
which is the only way to pin the cases that matter -- two processes sharing a
group, a line that must not match, and this run's own directory.

Whether the signal is elevated is pinned here too, in both directions: an
unprivileged sweep is the default and a `sudo` one is opt-in, because the
commands tolerate failure and so an elevation the host refuses is silent.
"""

from __future__ import annotations

import pytest

from conftest import STASH_DIR, out, recording
from pytest_testinfra_fabric import backend as backend_module
from pytest_testinfra_fabric.backend import FabricBackend

STALE_DIR = "/tmp/fabric.deadbee1"
OTHER_DIR = "/tmp/fabric.deadbee2"

# What `ps -ww -eo pgid=,args=` looks like on a host with one hard-killed run
# still on it: a daemon whose venv path names the directory, the nats-server
# whose store path does, and unrelated processes that must be left alone.
PS_LISTING = """\
  1234 sudo env PATH=/x /tmp/fabric.deadbee1/venv/bin/orchestrator --state /tmp/fabric.deadbee1/state.yaml
  1234 /tmp/fabric.deadbee1/venv/bin/python /tmp/fabric.deadbee1/venv/bin/orchestrator --state /tmp/fabric.deadbee1/state.yaml
  5678 /tmp/fabric.deadbee1/nats-server -js -sd /tmp/fabric.deadbee1/jetstream -p 4222
   999 sshd: philip@notty
     1 /sbin/init
  4321 socat TCP-LISTEN:39001,bind=10.0.0.2,fork,reuseaddr EXEC:nsenter -t 8123 -n -- socat - TCP4:127.0.0.1:22
"""


def looking(backend: FabricBackend, *, directories: str = "", listing: str = ""):
    """A recorder answering `stale_scratch`'s two reads, in order."""
    return recording(backend, [out(directories), out(listing)])


# ------------------------------------------------------------------ prefix


def test_prefix_names_the_template_and_the_glob() -> None:
    backend = FabricBackend("user@host", stash_prefix="orchtest")

    assert backend.stash_template == "/tmp/orchtest.XXXXXXXX"
    assert backend.stash_glob == "/tmp/orchtest.*"


def test_prefix_defaults_to_the_package_name() -> None:
    assert FabricBackend("user@host").stash_glob == "/tmp/fabric.*"


@pytest.mark.parametrize(
    "prefix",
    ["", "has/slash", "has space", "has*star", "has;semicolon", "..", "$(whoami)"],
)
def test_prefix_refuses_anything_but_a_path_component(prefix: str) -> None:
    """It is interpolated into both a `mktemp -d` template and an `rm -rf`
    glob, so anything shell-significant has to fail here rather than be
    quoted into something surprising later."""
    with pytest.raises(AssertionError, match="stash_prefix must be"):
        FabricBackend("user@host", stash_prefix=prefix)


# -------------------------------------------------------------- detection


def test_stale_scratch_is_falsey_on_a_clean_host(backend) -> None:
    looking(backend)

    assert not backend.stale_scratch()


def test_stale_scratch_reads_directories_and_processes(backend) -> None:
    recorder = looking(backend, directories=f"{STALE_DIR}\n{OTHER_DIR}\n", listing=PS_LISTING)

    stale = backend.stale_scratch()

    assert stale
    assert stale.directories == [STALE_DIR, OTHER_DIR]
    assert recorder.commands == [
        "ls -d /tmp/fabric.* 2>/dev/null",
        "ps -ww -eo pgid=,args=",
    ]


def test_stale_scratch_groups_by_process_group_not_by_process(backend) -> None:
    """Two processes of one daemon share a PGID, and the group is what gets
    signalled -- reporting them separately would mean signalling twice."""
    looking(backend, listing=PS_LISTING)

    groups = backend.stale_scratch().process_groups

    assert sorted(groups) == ["1234", "5678"]
    assert len(groups["1234"]) == 2
    assert len(groups["5678"]) == 1


def test_stale_scratch_ignores_processes_that_name_no_scratch_path(backend) -> None:
    """`sshd` and `init` are the obvious ones. The `socat` line is the case
    worth pinning: it IS an orphaned relay from that same dead run, but its
    argv names no scratch path, so it cannot be found this way -- see
    `stale_scratch`'s docstring."""
    looking(backend, listing=PS_LISTING)

    matched = [c for commands in backend.stale_scratch().process_groups.values() for c in commands]

    assert not any("sshd" in c or "/sbin/init" in c or c.startswith("socat") for c in matched)


def test_stale_scratch_excludes_this_backends_own_directory(backend) -> None:
    """So it is safe to call at any point, not only before one exists -- and
    so a running suite never reports itself as debris."""
    backend_module._stash_directory = STASH_DIR
    own = f"  4242 {STASH_DIR}/nats-server -js -sd {STASH_DIR}/jetstream -p 4222\n"
    looking(backend, directories=f"{STASH_DIR}\n{STALE_DIR}\n", listing=own + PS_LISTING)

    stale = backend.stale_scratch()

    assert stale.directories == [STALE_DIR]
    assert "4242" not in stale.process_groups


def test_stale_scratch_uses_the_backends_own_glob(backend) -> None:
    other = FabricBackend("user@host", stash_prefix="orchtest")
    recorder = looking(other)

    other.stale_scratch()

    assert recorder.commands[0] == "ls -d /tmp/orchtest.* 2>/dev/null"


def test_describe_lists_groups_and_directories(backend) -> None:
    looking(backend, directories=f"{STALE_DIR}\n", listing=PS_LISTING)

    described = backend.stale_scratch().describe()

    assert "process group 1234:" in described
    assert "process group 5678:" in described
    assert f"directory {STALE_DIR}" in described


def test_describe_says_so_when_there_is_nothing(backend) -> None:
    looking(backend)

    assert backend.stale_scratch().describe() == "  (nothing)"


# ------------------------------------------------------------------ sweeping


def test_sweep_stale_signals_each_group_once_then_removes(backend) -> None:
    looking(backend, directories=f"{STALE_DIR}\n", listing=PS_LISTING)

    swept = backend.sweep_stale()

    recorder = backend.__dict__["connection"]
    assert recorder.commands[2:] == [
        'kill -TERM -"1234" 2>/dev/null || true',
        'kill -TERM -"5678" 2>/dev/null || true',
        "rm -rf /tmp/fabric.*",
    ]
    assert sorted(swept.process_groups) == ["1234", "5678"]


def test_sweep_stale_is_unprivileged_by_default() -> None:
    """The regression this pins: `sudo` used to be hardcoded here, and both
    commands tolerate failure. On a host granting the login user no blanket
    sudo -- which is the shape of a least-privilege CI host -- every kill and
    the rm were refused, nothing was swept, and `sweep_stale` still returned
    the full list of what it had found for the caller to print as a success.
    """
    backend = FabricBackend("user@host")
    recording(backend, [out(f"{STALE_DIR}\n"), out(PS_LISTING)])

    backend.sweep_stale()

    assert not any(
        command.startswith("sudo") for command in backend.__dict__["connection"].commands
    )


def test_sweep_stale_elevates_when_the_debris_is_root_owned() -> None:
    """`sudo_cleanup` is for a consumer whose processes really do run as root;
    it is the same flag `close()` removes this backend's own directory under."""
    backend = FabricBackend("user@host", sudo_cleanup=True)
    recorder = recording(backend, [out(f"{STALE_DIR}\n"), out(PS_LISTING)])

    backend.sweep_stale()

    assert recorder.commands[2:] == [
        'sudo kill -TERM -"1234" 2>/dev/null || true',
        'sudo kill -TERM -"5678" 2>/dev/null || true',
        "sudo rm -rf /tmp/fabric.*",
    ]


def test_sweep_stale_does_nothing_on_a_clean_host(backend) -> None:
    recorder = looking(backend)

    assert not backend.sweep_stale()

    # The two reads, and no kill or rm at all.
    assert len(recorder.commands) == 2


def test_sweep_stale_removes_directories_with_no_live_process(backend) -> None:
    """Inert debris still goes: it is evidence a run died badly, and leaving
    it would mean the next run keeps refusing."""
    recorder = looking(backend, directories=f"{STALE_DIR}\n")

    backend.sweep_stale()

    assert recorder.last == "rm -rf /tmp/fabric.*"
