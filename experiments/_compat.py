"""Keeps the pinned submodules importable with the current clabe.

The submodules were written for the clabe they were released with. This module aliases the clabe modules that were
moved, so they import unchanged. It runs when ``experiments`` is first imported, before the submodules:

- ``clabe.data_mapper.aind_data_schema`` (every data mapper package) is now ``clabe.aind.data_schema``.

Remove it once those repositories import the new locations themselves.
"""

import sys

import clabe.aind.data_schema
import clabe.data_mapper

sys.modules.setdefault("clabe.data_mapper.aind_data_schema", clabe.aind.data_schema)
clabe.data_mapper.aind_data_schema = clabe.aind.data_schema  # type: ignore[attr-defined]
