"""Unit tests for `launch`/`process` and the `RemoteProcess` handle.

Assertions here are on the EXACT command string emitted, because that is
where the behaviour lives. Whether `exec` precedes the command, whether all
three descriptors are redirected away from the channel, and whether a `-`
precedes the PGID are each invisible to any coarser check -- they show up only
against a real host, as a hung channel or a signal that silently reached
nothing.

`MockRemote` is deliberately not used here: it verifies that expected
commands ran, but conftest's `Recorder` keeps what it was handed, which lets a
test assert the whole string in one piece and read the diff when it is
wrong.
"""

from __future__ import annotations

import pytest
from conftest import exited, recording
from pytest_testinfra_fabric.backend import RemoteProcess

DAEMON_PID = "/tmp/run/daemon.pid"
DAEMON_LOG = "/tmp/run/daemon.log"


# ---------------------------------------------------------------- launching


def test_launch_emits_the_whole_detachment_incantation(backend):
    recorder = recording(backend)

    backend.launch("/opt/bin/daemon --flag", pid_file=DAEMON_PID, log=DAEMON_LOG)

    assert recorder.last == (
        "setsid sh -c 'exec /opt/bin/daemon --flag' "
        "< /dev/null >> /tmp/run/daemon.log 2>&1 & "
        "echo $! > /tmp/run/daemon.pid; disown; true"
    )


def test_launch_puts_cwd_inside_the_sh_c_before_exec(backend):
    """`cd` has to live inside the backgrounded simple command, in this order:
    backgrounding a compound `cd … && …` list directly makes the shell fork a
    subshell whose stdio is not redirected away until partway through, and the
    channel then hangs for the full timeout even though the process started."""
    recorder = recording(backend)

    backend.launch(
        "env PORT=1 node server.js",
        pid_file="/tmp/run/server.pid",
        log="/tmp/run/server.log",
        cwd="/tmp/run/backend-server",
    )

    assert "sh -c 'cd /tmp/run/backend-server && exec env PORT=1 node server.js'" in recorder.last


def test_launch_prepends_extra_paths_in_order(backend):
    recorder = recording(backend)

    backend.launch(
        "websocat --version",
        pid_file=DAEMON_PID,
        extra_paths=("/tmp/fabric.x/bin", "/opt/waypoint/bin"),
    )

    assert recorder.last.startswith('export PATH="/tmp/fabric.x/bin:/opt/waypoint/bin:$PATH"; setsid ')


def test_launch_without_extra_paths_exports_nothing(backend):
    """The default is empty, not the bin directory: callers say what they want
    on the PATH rather than inheriting a guess."""
    recorder = recording(backend)

    backend.launch("/opt/bin/daemon", pid_file=DAEMON_PID)

    assert "export PATH" not in recorder.last


def test_launch_under_setsid_returns_a_group_handle(backend):
    recording(backend)

    process = backend.launch("/opt/bin/daemon", pid_file=DAEMON_PID)

    assert process.group is True


def test_launch_under_nohup_returns_a_bare_pid_handle(backend):
    """nohup forks no new process group, so the recorded PID is only ever a
    PID -- signalling it as a PGID would hit the wrong thing entirely."""
    recorder = recording(backend)

    process = backend.launch("nats-server -js", pid_file="/tmp/run/nats.pid", detach="nohup")

    assert recorder.last.startswith("nohup sh -c 'exec nats-server -js'")
    assert process.group is False


def test_launch_rejects_an_unknown_detach_mode(backend):
    recording(backend)

    with pytest.raises(AssertionError, match="unknown detach mode 'daemonize'"):
        backend.launch("/opt/bin/daemon", pid_file=DAEMON_PID, detach="daemonize")


def test_launch_bounds_itself_with_a_short_timeout(backend):
    """A correct launch returns the moment the shell forks, so the timeout is
    what turns a failure to detach into a failure in seconds rather than a
    hung session."""
    recorder = recording(backend)

    backend.launch("/opt/bin/daemon", pid_file=DAEMON_PID)

    assert recorder.timeouts == [25]


# ----------------------------------------------------------------- killing


def test_kill_signals_the_group_with_sudo(backend):
    """The shape the orchestrator's root-owned, setsid-launched daemon needs:
    a bare `kill` would fail with EPERM, and a bare PID would leave the
    sudo/wrapper/interpreter chain's other members running as orphans."""
    recorder = recording(backend)
    process = RemoteProcess(backend=backend, pid_file=DAEMON_PID, group=True, sudo=True)

    process.kill()

    assert recorder.last == 'sudo kill -TERM -"$(cat /tmp/run/daemon.pid)" 2>/dev/null || true'


def test_kill_signals_a_bare_pid_without_sudo(backend):
    recorder = recording(backend)
    process = RemoteProcess(backend=backend, pid_file="/tmp/run/nats.pid", group=False)

    process.kill()

    assert recorder.last == 'kill -TERM "$(cat /tmp/run/nats.pid)" 2>/dev/null || true'


def test_kill_passes_the_signal_through(backend):
    recorder = recording(backend)

    backend.process(DAEMON_PID).kill("KILL")

    assert recorder.last.startswith("kill -KILL ")


def test_kill_tolerates_a_missing_pid_file(backend):
    """A run killed before it ever wrote the file is the ordinary case, not an
    error -- teardown always wants to proceed."""
    recording(backend, [exited(1)])

    backend.process(DAEMON_PID).kill()  # must not raise


# ------------------------------------------------------------------ liveness


def test_alive_probes_the_group_with_kill_zero(backend):
    recorder = recording(backend, [exited(0)])
    process = RemoteProcess(backend=backend, pid_file=DAEMON_PID, group=True, sudo=True)

    assert process.alive()
    assert recorder.last == 'sudo kill -0 -"$(cat /tmp/run/daemon.pid)" 2>/dev/null'


def test_alive_is_false_when_kill_zero_fails(backend):
    recording(backend, [exited(1)])

    assert not backend.process(DAEMON_PID).alive()


def test_wait_returns_true_once_the_process_goes(backend):
    # alive, alive, then gone.
    recording(backend, [exited(0), exited(0), exited(1)])

    assert backend.process(DAEMON_PID).wait(timeout=10, interval=0)


def test_wait_returns_false_rather_than_raising(backend):
    """Teardown must not have a process that refuses to die replace the
    failure a test was already reporting."""
    recording(backend, [exited(0)] * 50)

    assert not backend.process(DAEMON_PID).wait(timeout=1, interval=0)


# --------------------------------------------------------------- adoption


def test_process_adopts_with_group_and_sudo_by_default(backend):
    """The sweep case: a hard-killed run leaves a root-owned, setsid-launched
    daemon behind, so the defaults match what needs adopting most often."""
    process = backend.process(DAEMON_PID)

    assert (process.pid_file, process.group) == (DAEMON_PID, True)


def test_process_verifies_nothing_up_front(backend):
    """A pid file that is missing or stale is the normal case for something an
    earlier run left behind, so adopting one must cost no round trip."""
    recorder = recording(backend)

    backend.process("/tmp/orchtest.gone/daemon.pid")

    assert recorder.commands == []


# ------------------------------------------------------------- process logs


def test_tail_reads_the_processes_own_log_as_root(backend):
    recorder = recording(backend)
    process = RemoteProcess(backend=backend, pid_file=DAEMON_PID, log=DAEMON_LOG, sudo=True)

    process.tail(20)

    assert recorder.last == "sudo tail -n 20 /tmp/run/daemon.log"


def test_log_contains_greps_for_a_literal(backend):
    """-F, never a pattern: a regex metacharacter in a log line being looked
    for would silently change the question."""
    recorder = recording(backend, [exited(0)])
    process = RemoteProcess(backend=backend, pid_file=DAEMON_PID, log=DAEMON_LOG)

    assert process.log_contains("refused namespace pytest.a1b2")

    assert recorder.last == (
        "grep -F -q 'refused namespace pytest.a1b2' /tmp/run/daemon.log"
    )
