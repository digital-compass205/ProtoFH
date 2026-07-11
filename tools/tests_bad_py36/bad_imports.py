"""Fixture: imports of modules forbidden before Python 3.6 / by project rules."""
import dataclasses  # BAD: import_dataclasses
import asyncio  # BAD: import_asyncio
import contextvars  # BAD: import_contextvars
import zoneinfo  # BAD: import_zoneinfo
import graphlib  # BAD: import_graphlib
import importlib.metadata  # BAD: import_importlib_metadata
from importlib import metadata  # BAD: import_importlib_metadata
