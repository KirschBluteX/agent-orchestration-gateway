from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-cost-orchestrator"


class V9ContractTests(unittest.TestCase):
    def test_clean_v9_control_plane_surface_exists(self) -> None:
        scripts = PLUGIN / "scripts"
        hooks = PLUGIN / "hooks"
        self.assertTrue((scripts / "control_plane.py").is_file())
        self.assertTrue((scripts / "workspace_guard.py").is_file())
        self.assertTrue((hooks / "cco_hook.py").is_file())
        self.assertTrue((PLUGIN / "skills" / "manage-cco" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
