"""Polling a predicate until it holds, or for as long as it holds.

Neither of these touches a connection -- they are here because everything in
this package that waits is built on them (see `FabricBackend.forward`'s
readiness check and `RemoteProcess.wait`), and because a consumer driving a
real host needs the same two shapes for its own domain predicates. Keeping one
waiting idiom is the point: a backend method that polls on its caller's behalf
would be a second one.
"""

from __future__ import annotations

import time
from collections.abc import Callable

DEFAULT_INTERVAL = 1.0


def eventually(
    predicate: Callable[[], object],
    timeout: int,
    message: str,
    interval: float = DEFAULT_INTERVAL,
) -> object:
    """Poll until `predicate` returns something truthy, and return it.

    Returns the predicate's own value rather than a bool, so a caller that
    polled for a thing does not have to read it a SECOND time to use it --
    which for anything read over a network is a race, and for anything read
    through a short-lived consumer is a read with nothing to fall back on.

    Raises AssertionError naming the last value seen, because "did not happen
    within 60s" on its own says nothing about what WAS happening.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{message} (last result: {last!r})")


def steadily(
    predicate: Callable[[], object],
    seconds: int,
    interval: float = DEFAULT_INTERVAL,
) -> bool:
    """Whether a condition holds for a whole window rather than at one instant.

    A refused request produces no event to wait for, so the only way to show
    that nothing was created is to keep looking for a while.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not predicate():
            return False
        time.sleep(interval)
    return True
