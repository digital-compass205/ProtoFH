"""Fixture: 3.7+ __future__ import and 3.8+ typing names."""
from __future__ import annotations  # BAD: future_annotations

from typing import Literal  # BAD: typing_literal
from typing import Protocol  # BAD: typing_protocol
from typing import Final  # BAD: typing_final
