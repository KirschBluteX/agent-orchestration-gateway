#!/usr/bin/env python3
"""Build Git environments bound to the repository supplied by CCO.

Git accepts several process-environment overrides that can replace its work
tree, administrative directory, index, object database, namespace, or config.
CCO always supplies a concrete repository path, so inheriting any of those
overrides would make a checked workspace different from the requested one.
"""

from __future__ import annotations

import os
from typing import Mapping


_ROUTING_VARIABLES = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_ICASE_PATHSPECS",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_LITERAL_PATHSPECS",
        "GIT_NAMESPACE",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_OPTIONAL_LOCKS",
        "GIT_PREFIX",
        "GIT_SUPER_PREFIX",
        "GIT_WORK_TREE",
    }
)
_CONFIG_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _is_routing_override(name: str) -> bool:
    """Treat environment names case-insensitively for Windows Git hosts."""

    normalized = name.upper()
    return normalized in _ROUTING_VARIABLES or normalized.startswith(_CONFIG_PREFIXES)


def clean_git_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy ``source`` without Git settings that can redirect repository state."""

    environment = dict(os.environ if source is None else source)
    for name in tuple(environment):
        if _is_routing_override(name):
            environment.pop(name, None)
    return environment
