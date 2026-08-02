#!/usr/bin/env python3
"""Validate repository-specific plugin metadata and packaging policy in CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
from urllib.parse import urlparse

import yaml


SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PLUGIN_FIELDS = {
    "id",
    "name",
    "version",
    "description",
    "skills",
    "apps",
    "mcpServers",
    "interface",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
}
INTERFACE_FIELDS = {
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
    "capabilities",
    "websiteURL",
    "privacyPolicyURL",
    "termsOfServiceURL",
    "brandColor",
    "composerIcon",
    "logo",
    "logoDark",
    "screenshots",
    "defaultPrompt",
    "default_prompt",
}


def non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_https_url(value: Any) -> bool:
    if not non_empty_string(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def contains_todo(value: Any) -> bool:
    if isinstance(value, str):
        return "[TODO:" in value
    if isinstance(value, list):
        return any(contains_todo(item) for item in value)
    if isinstance(value, dict):
        return any(contains_todo(item) for item in value.values())
    return False


def normalized_contract_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix().rstrip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or None


def read_json_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing {path}")
        return None
    except (OSError, json.JSONDecodeError):
        errors.append(f"{path} must contain valid JSON")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path} must contain a JSON object")
        return None
    return value


def validate_openai_yaml(path: Path, label: str, errors: list[str]) -> None:
    if not path.is_file():
        return
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        errors.append(f"{label} agents/openai.yaml must contain valid YAML")
        return
    if not isinstance(value, dict) or not isinstance(value.get("interface"), dict):
        errors.append(f"{label} agents/openai.yaml requires an interface object")
        return
    interface = value["interface"]
    for field in ("display_name", "short_description"):
        if not non_empty_string(interface.get(field)):
            errors.append(f"{label} agents/openai.yaml interface.{field} is required")
    default_prompt = interface.get("default_prompt")
    if default_prompt is not None and not non_empty_string(default_prompt):
        errors.append(f"{label} agents/openai.yaml interface.default_prompt must be text")


def validate_skills(plugin_root: Path, errors: list[str]) -> None:
    skills_root = plugin_root / "skills"
    if not skills_root.is_dir():
        errors.append("plugin declares skills but the skills directory is missing")
        return
    skill_dirs = [path for path in skills_root.iterdir() if path.is_dir() and not path.name.startswith(".")]
    if not skill_dirs:
        errors.append("plugin skills directory contains no skills")
    for skill_root in sorted(skill_dirs):
        label = f"skill {skill_root.name!r}"
        skill_md = skill_root / "SKILL.md"
        try:
            contents = skill_md.read_text(encoding="utf-8")
        except OSError:
            errors.append(f"{label} is missing SKILL.md")
            continue
        match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", contents, re.DOTALL)
        if match is None:
            errors.append(f"{label} requires closed YAML frontmatter")
            continue
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            errors.append(f"{label} frontmatter must contain valid YAML")
            continue
        if not isinstance(frontmatter, dict):
            errors.append(f"{label} frontmatter must be an object")
            continue
        if frontmatter.get("name") != skill_root.name:
            errors.append(f"{label} frontmatter name must match its directory")
        if not non_empty_string(frontmatter.get("description")):
            errors.append(f"{label} frontmatter description is required")
        validate_openai_yaml(skill_root / "agents" / "openai.yaml", label, errors)


def validate_plugin(plugin_root: Path) -> list[str]:
    errors: list[str] = []
    manifest = read_json_object(plugin_root / ".codex-plugin" / "plugin.json", errors)
    if manifest is None:
        return errors

    unknown = sorted(set(manifest) - PLUGIN_FIELDS)
    if unknown:
        errors.append(f"unsupported plugin fields: {', '.join(unknown)}")
    if contains_todo(manifest):
        errors.append("plugin manifest contains a [TODO: ...] placeholder")

    for field in ("name", "version", "description"):
        if not non_empty_string(manifest.get(field)):
            errors.append(f"plugin field {field!r} is required")
    name = manifest.get("name")
    if non_empty_string(name) and name != plugin_root.name:
        errors.append("plugin name must match the plugin directory")
    version = manifest.get("version")
    if non_empty_string(version) and SEMVER.fullmatch(version) is None:
        errors.append("plugin version must be strict semver")
    if normalized_contract_path(manifest.get("skills")) != "skills":
        errors.append("plugin skills path must resolve to 'skills'")

    author = manifest.get("author")
    if not isinstance(author, dict) or not non_empty_string(author.get("name")):
        errors.append("plugin author.name is required")
    elif author.get("url") is not None and not is_https_url(author.get("url")):
        errors.append("plugin author.url must be an absolute HTTPS URL")

    for field in ("homepage", "repository"):
        if manifest.get(field) is not None and not is_https_url(manifest.get(field)):
            errors.append(f"plugin {field} must be an absolute HTTPS URL")

    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        errors.append("plugin interface must be an object")
    else:
        unknown_interface = sorted(set(interface) - INTERFACE_FIELDS)
        if unknown_interface:
            errors.append(f"unsupported interface fields: {', '.join(unknown_interface)}")
        for field in (
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
        ):
            if not non_empty_string(interface.get(field)):
                errors.append(f"plugin interface.{field} is required")
        capabilities = interface.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or not all(
            non_empty_string(item) for item in capabilities
        ):
            errors.append("plugin interface.capabilities must be a non-empty string array")
        if "defaultPrompt" not in interface and "default_prompt" not in interface:
            errors.append("plugin interface requires defaultPrompt or default_prompt")
        website = interface.get("websiteURL")
        if website is not None and not is_https_url(website):
            errors.append("plugin interface.websiteURL must be an absolute HTTPS URL")

    validate_skills(plugin_root, errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_path", type=Path)
    args = parser.parse_args()
    plugin_root = args.plugin_path.expanduser().resolve()
    errors = validate_plugin(plugin_root)
    if errors:
        print("Plugin validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Plugin validation passed: {plugin_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
