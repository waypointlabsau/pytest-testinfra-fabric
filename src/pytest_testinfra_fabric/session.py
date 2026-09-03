"""Opening the one connection everything else rides, and closing it again.

Held for a whole test session rather than per command: a target behind a
`ProxyJump` pays a full multi-hop handshake per connection, and a suite that
polls once a second would pay it hundreds of times. It also has to outlive
every individual use, because a forward tunnel is a channel on it.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from paramiko.ssh_exception import SSHException

from .backend import FabricBackend

logger = logging.getLogger(__name__)

# Paramiko's equivalent of ssh's own `ServerAliveInterval`. Without it an idle
# NAT can drop the connection between two polls, and the next command fails on
# a socket that still looks open from this side.
KEEPALIVE_SECONDS = 30

# Opening the connection is the one thing that cannot ride an already-open
# connection, so it gets its own, shorter bound: an unreachable host should
# fail a run in seconds, not after a minute of TCP patience.
CONNECT_TIMEOUT = 15


@contextmanager
def connected(
    target: str,
    *,
    timeout: int,
    connect_timeout: int = CONNECT_TIMEOUT,
    log_path: Path | str | None = None,
    keepalive: int = KEEPALIVE_SECONDS,
    **kwargs: Any,
) -> Generator[FabricBackend]:
    """Yield an open `FabricBackend` for `target`, and tear it down afterwards.

    Three things happen here that are easy to leave out and painful to debug
    without.

    The connection is opened eagerly rather than on first use, so an
    unreachable host, a refused key or a jump host that is down fails AS
    ITSELF, at the top of a deployment, instead of surfacing as whichever
    command happened to run first.

    `set_keepalive` is what stops an idle NAT dropping the connection between
    two polls -- paramiko's answer to `ServerAliveInterval=30`.

    `log_path` attaches a file handler for the run. Fabric and paramiko log
    through the standard `logging` module rather than to a terminal, so
    without this a run that dies on the connection leaves nothing to read
    afterwards. The backend logs every command and its output at DEBUG;
    paramiko is held at INFO, because at DEBUG it narrates every packet, which
    for a session-long run is tens of megabytes around the one line that
    matters.

    Teardown removes the scratch directory before closing the connection (see
    `FabricBackend.close`), and detaches the handler either way. Pass
    `stash_prefix` and `sudo_cleanup` through `kwargs` to name that directory
    and to remove it as root.
    """
    handler = None
    loggers: list[tuple[logging.Logger, int]] = []
    if log_path is not None:
        handler = logging.FileHandler(log_path, mode="w")
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        loggers = [
            (logging.getLogger(__package__), logging.DEBUG),
            (logging.getLogger("paramiko"), logging.INFO),
        ]
        for log, level in loggers:
            log.addHandler(handler)
            log.setLevel(level)

    backend = FabricBackend(target, timeout=timeout, connect_timeout=connect_timeout, **kwargs)
    try:
        try:
            connection = backend.connection
        except (OSError, SSHException) as error:
            where = f"; see {log_path}" if log_path is not None else ""
            raise AssertionError(f"could not connect to {target}: {error}{where}") from error
        connection.transport.set_keepalive(keepalive)
        yield backend
    finally:
        backend.close()
        for log, _ in loggers:
            log.removeHandler(handler)
        if handler is not None:
            handler.close()
