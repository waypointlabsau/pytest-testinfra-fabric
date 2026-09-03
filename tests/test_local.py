"""Unit tests for the local-socket helpers. Real sockets on loopback, no host
and no mock -- there is nothing here to fake that would leave anything worth
asserting.
"""

from __future__ import annotations

import socket
from contextlib import closing

from pytest_testinfra_fabric import local


def test_free_port_is_in_the_ephemeral_range_and_bindable() -> None:
    port = local.free_port()

    assert 1024 < port < 65536
    # Reported free means genuinely bindable: the socket that discovered it is
    # closed before it returns, so nothing of ours is still holding it.
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", port))


def test_free_port_does_not_repeat_itself() -> None:
    """Not a guarantee the kernel makes forever, but two calls in a row
    handing back the same port would make it useless for its one job."""
    assert local.free_port() != local.free_port()


def test_local_port_open_is_false_for_a_port_nothing_holds() -> None:
    assert not local.local_port_open(local.free_port())


def test_local_port_open_is_true_for_a_listening_socket() -> None:
    with closing(socket.socket()) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        assert local.local_port_open(port)


def test_local_port_open_is_false_for_a_bound_but_unlistening_socket() -> None:
    """The distinction that makes this usable as a tunnel readiness check:
    binding alone does not accept connections, so a half-set-up forward
    reads as not ready rather than as ready."""
    with closing(socket.socket()) as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]

        assert not local.local_port_open(port)
