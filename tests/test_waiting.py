"""Unit tests for the two polling shapes. No host and no mock: these are pure
time-and-predicate functions, so the only thing worth driving them with is a
counter and a monkeypatched `sleep`.
"""

from __future__ import annotations

import pytest

from pytest_testinfra_fabric import waiting


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance a fake clock instead of really sleeping.

    Patching both `sleep` and `monotonic` (rather than passing a tiny
    interval) is what keeps these tests honest about the deadline: a real
    sub-millisecond interval would make "how many times was it polled"
    depend on how fast the machine is.
    """
    now = [0.0]
    monkeypatch.setattr(waiting.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(waiting.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_eventually_returns_the_predicates_own_value() -> None:
    """Not a bool -- the point is that a caller who polled for a thing gets
    the thing, and never has to read it a second time."""
    assert waiting.eventually(lambda: "ready", timeout=5, message="never") == "ready"


def test_eventually_polls_until_the_predicate_holds() -> None:
    calls = []

    def third_time() -> bool:
        calls.append(None)
        return len(calls) == 3

    assert waiting.eventually(third_time, timeout=10, message="never")
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("result", "shown"),
    # Only a falsey value can ever reach the message -- a truthy one is
    # returned instead. These are the three a real predicate produces: an
    # absent column, a status that isn't there yet, and an explicit no.
    [("", "''"), (None, "None"), (False, "False")],
    ids=["empty-string", "none", "false"],
)
def test_eventually_names_the_last_value_it_saw(result: object, shown: str) -> None:
    """"did not become RUNNING within 10s" alone says nothing about what WAS
    happening, so the last value the predicate produced is part of the
    failure."""
    with pytest.raises(AssertionError, match=rf"did not become RUNNING \(last result: {shown}\)"):
        waiting.eventually(lambda: result, timeout=10, message="did not become RUNNING")


def test_steadily_is_true_when_the_condition_never_breaks() -> None:
    assert waiting.steadily(lambda: True, seconds=5)


def test_steadily_is_false_at_the_first_break() -> None:
    """A refused request produces no event to wait for, so holding a window
    open is the only way to show nothing happened -- and one failure inside
    it is enough to say something did."""
    calls = []

    def breaks_on_the_third() -> bool:
        calls.append(None)
        return len(calls) < 3

    assert not waiting.steadily(breaks_on_the_third, seconds=30)
    assert len(calls) == 3
