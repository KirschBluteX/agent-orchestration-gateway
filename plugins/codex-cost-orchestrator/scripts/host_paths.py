#!/usr/bin/env python3
"""Canonical host-path helpers for Codex rollout and state files."""

from __future__ import annotations

import os
from pathlib import Path
import re


_EXTENDED_DRIVE = re.compile(r"^\\\\\?\\(?P<drive>[A-Za-z]:\\.*)$")
_EXTENDED_UNC = re.compile(r"^\\\\\?\\UNC\\(?P<unc>.+)$", re.IGNORECASE)


class HostPathError(ValueError):
    """A host path cannot be mapped to one ordinary filesystem identity."""


def strip_windows_extended_prefix(value: str) -> str:
    """Map safe Windows extended drive/UNC spellings to ordinary paths."""

    unc = _EXTENDED_UNC.fullmatch(value)
    if unc is not None:
        return "\\\\" + unc.group("unc")
    drive = _EXTENDED_DRIVE.fullmatch(value)
    if drive is not None:
        return drive.group("drive")
    if value.startswith("\\\\?\\"):
        raise HostPathError("unsupported Windows device path")
    return value


def host_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute ordinary path without accepting device namespaces."""

    text = strip_windows_extended_prefix(os.fspath(value))
    if text.startswith("\\\\.\\"):
        raise HostPathError("Windows device paths are not supported")
    return Path(os.path.abspath(Path(text).expanduser()))


def is_within(root: Path, candidate: Path) -> bool:
    """Return whether two resolved paths share the expected trusted root."""

    normalized_root = os.path.normcase(str(root))
    normalized_candidate = os.path.normcase(str(candidate))
    try:
        return os.path.commonpath((normalized_root, normalized_candidate)) == normalized_root
    except ValueError:
        return False
