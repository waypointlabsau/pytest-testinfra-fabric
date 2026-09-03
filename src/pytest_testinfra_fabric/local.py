"""Sockets on the machine running the tests, not on the host.
"""

from __future__ import annotations

import socket

CONNECT_TIMEOUT = 1.0


def free_port() -> int:
    """A locally unused port, for the near end of a forward tunnel.

    Bind-to-zero and read back what the kernel chose, rather than probing a
    fixed guess: nothing else can be holding it at the moment it is reported,
    which is the closest thing to a reservation available without keeping the
    socket open.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def local_port_open(port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    """Whether something is accepting connections on loopback at `port`.

    Deliberately says nothing about what is on the far side of a tunnel: a
    local forward accepts the instant its listening socket is bound, whether
    or not anything answers at the other end. Use this to know the tunnel
    exists, and probe the remote side itself to know the service does.
    """
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()
