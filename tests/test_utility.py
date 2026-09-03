"""Unit tests for `download_binary`.

The three URL shapes here are the real ones a deployment fetches, named in
each test, because the branch between them is a suffix match that is easy to
get subtly wrong and whose failure mode is a zero-byte file rather than an
error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import STASH_DIR, STASH_TEMPLATE, exited, out, recording
from pytest_testinfra_fabric import utility
from pytest_testinfra_fabric.backend import FabricBackend

# `download_binary`'s command sequence, in order: create the scratch
# directory, make sure it exists, fetch, make it executable, then check the
# result is not empty.
MKTEMP, MKDIR, FETCH, CHMOD, SIZE = range(5)

NATS_URL = (
    "https://github.com/nats-io/nats-server/releases/download/v2.14.5"
    "/nats-server-v2.14.5-linux-amd64.tar.gz"
)
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.9.2/uv-x86_64-unknown-linux-gnu.tar.gz"
WEBSOCAT_URL = (
    "https://github.com/vi/websocat/releases/download/v1.14.1/websocat.x86_64-unknown-linux-musl"
)


def fetching(backend: FabricBackend, *, empty: bool = False):
    """A recorder with an answer for each of `download_binary`'s commands.

    `empty` flips only the final size check to a failure, which is what a
    `member` glob matching nothing actually looks like: every command
    succeeded and the file is zero bytes.
    """
    answers = [out(f"{STASH_DIR}\n"), exited(0), exited(0), exited(0), exited(0)]
    if empty:
        answers[SIZE] = exited(1)
    return recording(backend, answers)


def test_download_binary_lands_in_the_scratch_directory(backend) -> None:
    """Which doubles as the bin directory: that same path is what gets passed
    as `launch`'s `extra_paths`, so a launched process resolves anything
    fetched here by name."""
    fetching(backend)

    path = utility.download_binary(backend, WEBSOCAT_URL, "websocat")

    assert path == Path(STASH_DIR) / "websocat"
    assert isinstance(path, Path)


def test_download_binary_fetches_a_bare_asset_straight_to_its_name(backend) -> None:
    """websocat's release asset IS the binary, under a name nobody wants on
    their PATH -- so it is fetched directly to `name`, with no tar involved."""
    recorder = fetching(backend)

    utility.download_binary(backend, WEBSOCAT_URL, "websocat")

    assert recorder.commands[FETCH] == f"curl -fsSL {WEBSOCAT_URL} -o {STASH_DIR}/websocat"


@pytest.mark.parametrize(
    ("url", "name"),
    # Both wrap the binary in a versioned directory, so `*/{name}` finds it.
    [(NATS_URL, "nats-server"), (UV_URL, "uv")],
    ids=["nats-server", "uv"],
)
def test_download_binary_extracts_a_tarball_member_named_after_the_binary(backend, url, name) -> None:
    recorder = fetching(backend)

    utility.download_binary(backend, url, name)

    assert recorder.commands[FETCH] == (
        f"set -o pipefail; curl -fsSL {url} "
        f"| tar -xzO --wildcards '*/{name}' > {STASH_DIR}/{name}"
    )


def test_download_binary_uses_pipefail_so_a_404_fails_here(backend) -> None:
    """Without it the pipeline reports tar's status rather than curl's, and
    `curl -f` writing nothing looks like success."""
    recorder = fetching(backend)

    utility.download_binary(backend, NATS_URL, "nats-server")

    assert recorder.commands[FETCH].startswith("set -o pipefail; ")


def test_download_binary_picks_the_decompression_flag_from_the_suffix(backend) -> None:
    recorder = fetching(backend)

    utility.download_binary(backend, "https://example.invalid/node.tar.xz", "node")

    assert "tar -xJO --wildcards '*/node'" in recorder.commands[FETCH]


def test_download_binary_accepts_an_explicit_member(backend) -> None:
    """For an archive whose internal name differs from what you want to call
    the thing on your PATH."""
    recorder = fetching(backend)

    utility.download_binary(backend, NATS_URL, "nats", member="*/nats-server")

    assert f"--wildcards '*/nats-server' > {STASH_DIR}/nats" in recorder.commands[FETCH]


def test_download_binary_makes_the_result_executable(backend) -> None:
    recorder = fetching(backend)

    utility.download_binary(backend, WEBSOCAT_URL, "websocat")

    assert recorder.commands[CHMOD] == f"chmod +x {STASH_DIR}/websocat"


def test_download_binary_rejects_an_empty_result(backend) -> None:
    """A `member` glob matching nothing has tar write a zero-byte file through
    the pipe rather than erroring, which would otherwise surface much later as
    an unhelpful "cannot execute binary file"."""
    fetching(backend, empty=True)

    with pytest.raises(AssertionError, match="produced an empty .*/nats-server"):
        utility.download_binary(backend, NATS_URL, "nats-server")


def test_download_binary_creates_the_scratch_directory_first(backend) -> None:
    recorder = fetching(backend)

    utility.download_binary(backend, WEBSOCAT_URL, "websocat")

    assert recorder.commands[MKTEMP] == f"mktemp -d {STASH_TEMPLATE}"
    assert recorder.commands[MKDIR] == f"mkdir -p {STASH_DIR}"
