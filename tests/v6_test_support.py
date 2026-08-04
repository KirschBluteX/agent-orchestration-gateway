from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-cost-orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from routing_catalog import resolve_route_plan  # noqa: E402


def fixed_route_plan(
    *,
    purpose: str = "implementation",
    judgment: str = "routine",
    model: str = "gpt-5.6-luna",
    effort: str = "max",
) -> dict[str, object]:
    request: dict[str, object] = {
        "fixed_effort": effort,
        "fixed_model": model,
        "judgment": judgment,
        "placement_benefits": [],
        "purpose": purpose,
    }
    if purpose != "acceptance":
        request["placement_benefits"] = [
            {"evidence": ["contract:test"], "kind": "closed_execution"}
        ]
    native_catalog = {
        "models": [
            {
                "slug": model,
                "supported_reasoning_levels": [{"effort": effort}],
            }
        ]
    }
    return resolve_route_plan([request], {}, native_catalog)
