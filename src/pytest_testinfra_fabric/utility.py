"""Getting executables onto the host, into a directory a launch can put on PATH.

Free functions rather than methods on `FabricBackend`, and rather than a class
of their own: they hold no state, and they are consumers of the backend's
scratch-directory primitive rather than part of what it means to drive an SSH
connection. Nothing here needs teardown -- `FabricBackend.close()` removes the
scratch directory and this subtree with it.

The download runs ON THE HOST. That is the whole point: a suite that fetched
release binaries locally and uploaded them would push tens of megabytes over
a connection that may be several hops long, every run, before a single test
executed -- where the host fetches the same bytes directly at its own
bandwidth. It does mean the host needs outbound HTTPS and `curl`, which is
worth checking for up front rather than discovering as a confusing failure
partway through a deployment.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from .backend import DOWNLOAD_TIMEOUT, FabricBackend

logger = logging.getLogger(__name__)

# tar's decompression flag per archive suffix. Longest suffixes are matched
# first (".tar.gz" before ".gz" would matter if a bare ".gz" were ever added).
TAR_FLAGS = {
    ".tar.gz": "z",
    ".tgz": "z",
    ".tar.xz": "J",
    ".tar.bz2": "j",
}

def download_binary(
    backend: FabricBackend,
    url: str,
    name: str,
    *,
    member: str | None = None,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> Path:
    """Fetch `url` on the host, leaving an executable at
    `FabricBackend.STASH_DIRECTORY/name`, and return that path.

    The scratch directory doubles as the bin directory, so passing it as
    `launch`'s `extra_paths` is what lets a launched process resolve anything
    fetched here by name.

    `name` doubles as the answer to "what, inside the archive?", so the common
    case needs no `member`: a release tarball wrapping its binary in a
    versioned directory (`uv-x86_64-unknown-linux-gnu/uv`,
    `nats-server-v2.14.5-linux-amd64/nats-server`) is matched by `*/{name}`,
    and the trailing `/uv` in that glob correctly ignores the `uvx` sitting
    beside it. A URL that is not an archive at all -- a release asset that is
    just the binary, under a name nobody wants on their PATH, like
    `websocat.x86_64-unknown-linux-musl` -- is fetched straight to `name`.
    Pass `member` only when what is inside the archive is named differently
    from what you want to call it.

    No skip-if-present check: the scratch directory is new every run, so there
    is never anything to skip.

    Two details are about failing loudly. `set -o pipefail` is what makes a
    404 fail here rather than downstream: without it the pipeline reports
    tar's status, not curl's, and `curl -f` writing nothing looks like
    success. And the size check catches the other half of the same problem --
    a `member` glob that matches nothing has tar write a zero-byte file
    through the pipe rather than erroring, which would otherwise surface much
    later as an unhelpful "cannot execute binary file".

    Extracting by wildcard needs GNU tar, which is what a Linux host has;
    BSD tar has no `--wildcards`.
    """
    destination = Path(backend.stash_dir) / name
    quoted = shlex.quote(str(destination))
    backend.execute(f"mkdir -p {shlex.quote(str(destination.parent))}")

    flag = next((f for suffix, f in TAR_FLAGS.items() if url.endswith(suffix)), None)
    if flag is None:
        logger.debug("download_binary: %s -> %s", url, destination)
        fetch = f"curl -fsSL {shlex.quote(url)} -o {quoted}"
    else:
        pattern = member or f"*/{name}"
        logger.debug("download_binary: %s -> %s (member %s)", url, destination, pattern)
        fetch = (
            f"set -o pipefail; curl -fsSL {shlex.quote(url)} "
            f"| tar -x{flag}O --wildcards {shlex.quote(pattern)} > {quoted}"
        )

    backend.execute(fetch, timeout=timeout)
    backend.execute(f"chmod +x {quoted}")
    assert backend.execute(f"test -s {quoted}", check=False).ok, (
        f"{url} produced an empty {destination} -- "
        f"nothing in the archive matched {member or f'*/{name}'!r}"
    )
    return destination
