"""Registers `FabricBackend` under testinfra's `fabric` connection scheme.

testinfra has no plugin/entry-point registry of its own for backends (see
`testinfra.backend.BACKENDS`) -- adding a scheme means mutating that
module-level dict directly. This module is loaded automatically by pytest via
this package's `pytest11` entry point, so the mutation below runs for any test
session that has this package installed, without any project needing to
import it explicitly.
"""

from __future__ import annotations

from testinfra import backend

from .backend import FabricBackend

backend.BACKENDS["fabric"] = f"{FabricBackend.__module__}.FabricBackend"
