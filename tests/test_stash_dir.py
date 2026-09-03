"""Unit tests for the per-run scratch directory and its teardown.

The behaviour worth pinning here is mostly about lifetime rather than about
command strings: who creates the directory, who is allowed to, what
`STASH_DIRECTORY` does before one exists, and what `close()` removes.
"""

from __future__ import annotations

import pytest

from conftest import STASH_DIR, STASH_TEMPLATE, out, recording
from pytest_testinfra_fabric.backend import FabricBackend


def created(backend: FabricBackend):
    """A recorder whose first answer is a scratch directory from mktemp."""
    return recording(backend, [out(f"{STASH_DIR}\n")])


def test_stash_directory_refuses_to_answer_before_one_exists() -> None:
    """The whole reason this is a property and not a plain class attribute: a
    `None` here would format into a plausible-looking "None/ws_client.py" and
    fail somewhere else entirely."""
    with pytest.raises(RuntimeError, match="no scratch directory exists yet"):
        _ = FabricBackend.STASH_DIRECTORY


def test_stash_dir_creates_it_with_mktemp(backend) -> None:
    recorder = created(backend)

    assert backend.stash_dir == STASH_DIR
    assert recorder.commands == [f"mktemp -d {STASH_TEMPLATE}"]


def test_stash_dir_publishes_itself_as_the_class_property(backend) -> None:
    """What lets a caller name a path under it without holding a backend."""
    created(backend)

    _ = backend.stash_dir

    assert FabricBackend.STASH_DIRECTORY == STASH_DIR


def test_stash_dir_is_created_once(backend) -> None:
    recorder = created(backend)

    assert backend.stash_dir == backend.stash_dir

    assert len(recorder.commands) == 1


def test_a_second_live_backend_refuses_rather_than_overwrites(backend) -> None:
    """A second value would leave the first backend's files reachable only
    through the object that made them, and un-cleaned when the wrong backend
    closed."""
    created(backend)
    _ = backend.stash_dir
    other = FabricBackend("user@host")
    created(other)

    with pytest.raises(AssertionError, match=f"another live backend already owns {STASH_DIR}"):
        _ = other.stash_dir


def test_close_removes_the_directory_and_clears_the_property(backend) -> None:
    recorder = created(backend)
    _ = backend.stash_dir

    backend.close()

    assert recorder.last == f"rm -rf {STASH_DIR}"
    with pytest.raises(RuntimeError):
        _ = FabricBackend.STASH_DIRECTORY


def test_close_removes_as_root_when_asked(backend) -> None:
    """What makes teardown work when something running as root wrote into the
    directory: an unprivileged `rm -rf` cannot remove root-owned files, and
    would leave the whole directory behind for the next run to trip over."""
    rooted = FabricBackend("user@host", sudo_cleanup=True)
    recorder = created(rooted)
    _ = rooted.stash_dir

    rooted.close()

    assert recorder.last == f"sudo rm -rf {STASH_DIR}"


def test_close_closes_the_connection(backend) -> None:
    recorder = created(backend)
    _ = backend.stash_dir

    backend.close()

    assert recorder.closed


def test_close_frees_the_slot_for_another_backend(backend) -> None:
    created(backend)
    _ = backend.stash_dir
    backend.close()

    other = FabricBackend("user@host")
    created(other)

    assert other.stash_dir == STASH_DIR


def test_close_does_nothing_when_no_directory_was_created(backend) -> None:
    """A deployment closes its backend on the way out of a failure as well as
    a success, and the failure may have happened before anything existed."""
    recorder = recording(backend)

    backend.close()

    assert recorder.commands == []


def test_close_is_idempotent(backend) -> None:
    recorder = created(backend)
    _ = backend.stash_dir

    backend.close()
    backend.close()

    assert recorder.commands.count(f"rm -rf {STASH_DIR}") == 1


def test_close_removes_only_its_own_directory(backend) -> None:
    """Closing a backend that never created one must not take another's files
    with it."""
    created(backend)
    _ = backend.stash_dir
    bystander = FabricBackend("user@host")
    recording(bystander)

    bystander.close()

    assert FabricBackend.STASH_DIRECTORY == STASH_DIR
