"""Deterministic UTF-8 transport decoding shared by CCO hook adapters."""

from __future__ import annotations

import json
from typing import Any, BinaryIO


def load_utf8_json(stream: BinaryIO) -> Any:
    return json.loads(stream.read().decode("utf-8"))
