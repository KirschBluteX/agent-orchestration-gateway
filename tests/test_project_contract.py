import json
from pathlib import Path
import re
import subprocess
import tomllib
import unittest


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "codex-cost-orchestrator"
SKILL = PLUGIN / "skills" / "orchestrate" / "SKILL.md"
CORE = SKILL.parent / "references" / "worker-core.md"
CONTRACTS = SKILL.parent / "references" / "contracts-v4.md"
RUNTIME = SKILL.parent / "references" / "runtime-gates.md"
HOOK = PLUGIN / "hooks" / "subagent_stop.py"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def result_fields(source: str, constant: str) -> set[str]:
    match = re.search(
        rf"{constant}\s*=\s*\((.*?)\)\n",
        source,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing {constant}")
    return set(re.findall(r'"([A-Z_]+)"', match.group(1)))


class ProjectIdentityTests(unittest.TestCase):
    def test_public_identity_and_bilingual_readmes(self) -> None:
        english = text(REPO / "README.md")
        chinese = text(REPO / "README.zh-CN.md")
        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))

        self.assertIn("[简体中文](README.zh-CN.md)", english)
        self.assertIn("[English](README.md)", chinese)
        self.assertEqual(manifest["name"], "codex-cost-orchestrator")
        self.assertEqual(manifest["version"], "0.4.0")
        self.assertEqual(manifest["author"]["name"], "KirschQAQ")
        self.assertEqual(manifest["license"], "MIT")
        combined = (english + chinese).lower()
        self.assertNotIn("opensquilla", combined)
        self.assertNotIn("sol-advisor", combined)

    def test_skill_uses_conditional_reference_loading(self) -> None:
        skill = text(SKILL)
        normalized = " ".join(skill.split())
        self.assertIn("references/worker-core.md", skill)
        self.assertIn("only for runtime/profile mismatch", normalized)
        self.assertIn("before concurrent Multi", normalized)
        self.assertTrue(CORE.is_file())
        self.assertTrue(CONTRACTS.is_file())
        self.assertTrue(RUNTIME.is_file())
        combined_size = len(text(CORE).encode())
        self.assertLess(combined_size, 20_000)

    def test_protocol_documents_only_authoritative_hash_domains(self) -> None:
        docs = "\n".join((text(CORE), text(CONTRACTS), text(SKILL)))
        for domain in (
            "contract",
            "graph_manifest",
            "acceptance_decision",
            "acceptance_chain",
            "input_closure",
            "failure",
            "evidence",
        ):
            self.assertIn(domain, docs)
        self.assertNotIn("contract_bundle", docs)
        self.assertNotIn("CONTRACT_BUNDLE_SHA256", docs)

    def test_worker_packet_closes_graph_chain_scope_risks_and_routing(self) -> None:
        core = text(CORE)
        for field in (
            "GRAPH_MANIFEST_SHA256",
            "ACCEPTANCE_CHAIN_SHA256",
            "ACCEPTANCE_CHAIN_JSON",
            "RISK_FLAGS",
            "MODEL_POLICY",
            "REQUESTED_MODEL",
            "EFFORT_POLICY",
            "REQUESTED_EFFORT",
            "FORK_TURNS",
            "LEASE_GENERATION",
            "STOP_GENERATION",
        ):
            self.assertIn(field, core)
        self.assertIn("exact:<path>", core)
        self.assertIn("prefix", core)

    def test_acceptance_chain_is_one_way_and_structural(self) -> None:
        contracts = " ".join(text(CONTRACTS).split())
        for phrase in (
            "one-way",
            "previous_decision_sha256",
            "worker_followup",
            "globally unique A/V IDs",
            "128 distinct scopes",
            "The acceptance chain already ends in `independent`",
        ):
            self.assertIn(phrase, contracts)
        self.assertIn("advisory only", contracts)
        self.assertIn("not encryption", contracts)

    def test_attempt_and_followup_limits_have_unambiguous_scope(self) -> None:
        contracts = " ".join(text(CONTRACTS).split())
        self.assertIn("at most three worker runs", contracts)
        self.assertIn("Each run may use at most two live follow-ups", contracts)
        self.assertIn("at most two fresh reviewer threads", contracts)
        self.assertIn("each thread may use at most two delta turns", contracts)

    def test_leaf_profiles_remain_non_delegating_and_model_neutral(self) -> None:
        for lane in ("routine", "complex"):
            profile = tomllib.loads(
                text(PLUGIN / "agents" / f"codex-cost-orchestrator-{lane}-worker.toml")
            )
            self.assertNotIn("model", profile)
            self.assertNotIn("model_reasoning_effort", profile)
            self.assertFalse(profile["features"]["multi_agent"])
            self.assertFalse(profile["features"]["multi_agent_v2"])
            instructions = profile["developer_instructions"]
            self.assertIn("GRAPH_MANIFEST_SHA256", instructions)
            self.assertIn("ACCEPTANCE_CHAIN_SHA256", instructions)

        reviewer = tomllib.loads(
            text(PLUGIN / "agents" / "codex-cost-orchestrator-reviewer.toml")
        )
        self.assertEqual(reviewer["model"], "gpt-5.6-sol")
        self.assertEqual(reviewer["model_reasoning_effort"], "high")
        self.assertEqual(reviewer["sandbox_mode"], "read-only")
        self.assertIn("ACCEPTANCE_CHAIN_SHA256", reviewer["developer_instructions"])

    def test_role_result_templates_match_stop_hook_required_fields(self) -> None:
        hook_source = text(HOOK)
        worker_required = result_fields(hook_source, "WORK_RESULT_FIELDS")
        review_required = result_fields(hook_source, "REVIEW_RESULT_FIELDS")
        for lane in ("routine", "complex"):
            instructions = tomllib.loads(
                text(PLUGIN / "agents" / f"codex-cost-orchestrator-{lane}-worker.toml")
            )["developer_instructions"]
            for field in worker_required:
                self.assertIn(f"{field}:", instructions)
        reviewer = tomllib.loads(
            text(PLUGIN / "agents" / "codex-cost-orchestrator-reviewer.toml")
        )["developer_instructions"]
        for field in review_required:
            self.assertIn(f"{field}:", reviewer)

    def test_workspace_docs_distinguish_capture_and_verify_schemas(self) -> None:
        readmes = text(REPO / "README.md") + text(REPO / "README.zh-CN.md")
        runtime = text(RUNTIME)
        for required in (
            "cco.workspace-state.v2",
            "cco.workspace-verification.v2",
            "allowed_scopes",
            "--next-baseline",
        ):
            self.assertIn(required, readmes + runtime)

    def test_installer_docs_bound_concurrency_and_metadata_claims(self) -> None:
        docs = text(REPO / "README.md") + text(RUNTIME)
        self.assertIn("identity or bytes changed", docs)
        self.assertIn("POSIX ctime", docs)
        self.assertIn("final check/replace race", docs)

    def test_native_codex_remains_the_only_agent_runtime(self) -> None:
        docs = text(CONTRACTS) + text(SKILL)
        self.assertIn("only Agent runtime", docs)
        for forbidden in (
            "SQLite coordinator",
            "ProviderCallPlan",
            "Pi SDK session",
        ):
            self.assertNotIn(forbidden, docs)

    def test_release_ci_and_marketplace_contracts_remain_pinned(self) -> None:
        workflow = text(REPO / ".github" / "workflows" / "ci.yml")
        first_party_uses = re.findall(
            r"uses:\s+(actions/(?:checkout|setup-python))@([^\s#]+)",
            workflow,
        )
        self.assertEqual(len(first_party_uses), 3)
        for action, revision in first_party_uses:
            with self.subTest(action=action):
                self.assertRegex(revision, r"^[0-9a-f]{40}$")

        manifest = json.loads(text(PLUGIN / ".codex-plugin" / "plugin.json"))
        marketplace = json.loads(text(REPO / ".agents" / "plugins" / "marketplace.json"))
        self.assertEqual(marketplace["name"], manifest["name"])
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["source"]["source"], "local")
        self.assertEqual(entry["source"]["path"], f"./plugins/{manifest['name']}")
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")

    def test_shipping_tree_keeps_independent_clean_root_identity(self) -> None:
        text_suffixes = {".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}
        excluded = ("sol-advisor", "DannyMac180", "OpenSquilla")
        for path in PLUGIN.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            contents = text(path)
            for identity in excluded:
                with self.subTest(path=path.relative_to(REPO), identity=identity):
                    self.assertNotIn(identity, contents)

        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=REPO,
            text=True,
        ).splitlines()
        self.assertEqual(len(roots), 1)
        root_paths = set(
            subprocess.check_output(
                ["git", "ls-tree", "-r", "--name-only", roots[0]],
                cwd=REPO,
                text=True,
            ).splitlines()
        )
        self.assertTrue(
            {
                "AGENTS.md",
                "README.md",
                "README.zh-CN.md",
                "plugins/codex-cost-orchestrator/skills/orchestrate/SKILL.md",
            }.issubset(root_paths)
        )
        self.assertFalse(any(path.startswith("plugins/sol-advisor/") for path in root_paths))

    def test_shipping_tree_exposes_only_cco_v4(self) -> None:
        text_suffixes = {".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}
        for path in PLUGIN.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in text_suffixes:
                continue
            contents = text(path)
            with self.subTest(path=path.relative_to(REPO)):
                self.assertNotIn("cco.v3", contents)
                self.assertNotIn("contracts-v3", contents)

    def test_hook_trust_and_fail_open_limits_are_documented(self) -> None:
        readme = text(REPO / "README.md")
        runtime = text(RUNTIME)
        docs = " ".join((readme + runtime).split())
        for required in (
            "`/hooks`",
            "untrusted",
            "current hash",
            "ambient OS permissions",
            "Hook failure is fail-open",
            "never replace primary acceptance",
        ):
            with self.subTest(required=required):
                self.assertIn(required, docs)

    def test_skill_allows_implicit_invocation(self) -> None:
        skill_root = SKILL.parent
        openai_yaml = text(skill_root / "agents" / "openai.yaml")
        self.assertRegex(
            openai_yaml,
            re.compile(
                r"^policy:\s*\n\s+allow_implicit_invocation:\s+true\s*$",
                re.MULTILINE,
            ),
        )

    def test_direct_route_requires_deterministic_verification_and_no_risk_flags(self) -> None:
        english_docs = (
            text(REPO / "README.md"),
            text(REPO / "AGENTS.md"),
            text(SKILL),
        )
        for document in english_docs:
            self.assertIn("deterministic verification", document)
            self.assertIn("no enumerated `RISK_FLAGS`", document)
            self.assertIn("external_side_effect", document)
            self.assertIn("nondeterministic_verification", document)

        chinese = text(REPO / "README.zh-CN.md")
        self.assertIn("确定性验证", chinese)
        self.assertIn("枚举的 `RISK_FLAGS` 均为空", chinese)
        self.assertIn("external_side_effect", chinese)
        self.assertIn("nondeterministic_verification", chinese)

    def test_short_primary_docs_show_exact_worker_and_evidence_preimages(self) -> None:
        core = text(CORE)
        for key in (
            "acceptance_chain_sha256",
            "attempt",
            "acceptance_ids",
            "baseline",
            "content_anchors",
            "contract_rev",
            "contract_sha256",
            "dependencies",
            "effort_policy",
            "fork_turns",
            "followup",
            "graph_manifest_sha256",
            "kind",
            "lease",
            "lease_generation",
            "model_policy",
            "node",
            "protocol",
            "requested_effort",
            "requested_model",
            "role",
            "run",
            "stop_generation",
        ):
            self.assertIn(f'"{key}"', core)
        for key in (
            "acceptance_ids",
            "acceptance_chain",
            "acceptance_chain_sha256",
            "current_state",
            "protocol",
            "records",
            "observed_outcome",
            "artifact_sha256s",
            "implementation_owner",
            "verification_id",
        ):
            self.assertIn(f'"{key}"', core)
        self.assertRegex(core, r'"kind"\s*:\s*"worker_initial"')
        self.assertIn("EVIDENCE_JSON", core)

    def test_extended_contract_docs_show_followup_review_and_failure_shapes(self) -> None:
        contracts = text(CONTRACTS)
        for kind, keys in {
            "worker_followup": (
                "acceptance_chain_sha256",
                "binding",
                "delta",
                "followup",
                "kind",
                "previous_input_closure_sha256",
                "protocol",
                "target",
                "type",
                "verify",
            ),
            "review_fresh": (
                "acceptance",
                "acceptance_ids",
                "accumulated_delta",
                "allowed_paths",
                "attempt",
                "baseline",
                "acceptance_chain_sha256",
                "contracts",
                "current_state",
                "epoch",
                "evidence_sha256",
                "followup",
                "fork_turns",
                "goal",
                "graph_manifest_sha256",
                "interfaces",
                "kind",
                "open_risks",
                "protocol",
            ),
            "review_delta": (
                "acceptance_ids",
                "acceptance_chain_sha256",
                "attempt",
                "contract_status",
                "contracts",
                "current_state",
                "delta",
                "epoch",
                "evidence_sha256",
                "followup",
                "graph_manifest_sha256",
                "kind",
                "open_risks",
                "previous_input_closure_sha256",
                "prior_reviewed_state",
                "protocol",
                "resolves",
                "target",
            ),
            "failure": (
                "acceptance_or_verification_id",
                "contract_sha256",
                "diagnostic_ids",
                "exit_status",
                "failure_class",
                "node",
                "protocol",
            ),
        }.items():
            with self.subTest(kind=kind):
                if kind != "failure":
                    self.assertRegex(contracts, rf'"kind"\s*:\s*"{kind}"')
                for key in keys:
                    self.assertIn(f'"{key}"', contracts)

    def test_primary_upgrade_rechecks_uncached_reviewer_profile_before_fix_or_review(self) -> None:
        docs = text(SKILL) + text(RUNTIME) + text(REPO / "AGENTS.md")
        for required in (
            "primary-to-independent upgrade",
            "reviewer profile",
            "cached checked set",
            "before any fix or review",
            "--profile reviewer",
        ):
            with self.subTest(required=required):
                self.assertIn(required, docs)

    def test_worker_core_contains_short_successful_runtime_inspector_path(self) -> None:
        core = text(CORE)
        self.assertIn("scripts/inspect_agent_runtime.py", core)
        self.assertIn("--expect-role cost_orchestrator_routine_worker", core)
        self.assertIn("--expect-model <selected-model>", core)
        self.assertIn("--expect-effort <selected-effort>", core)
        self.assertIn("exact role", core)
        self.assertIn("stable", core)
        self.assertIn("mismatch", core)

    def test_bilingual_docs_name_all_applicable_hashes_and_helper_role(self) -> None:
        english = text(REPO / "README.md")
        chinese = text(REPO / "README.zh-CN.md")
        self.assertNotIn("recomputes both hashes", english)
        self.assertNotIn("重算两个 hash", chinese)
        self.assertIn("all applicable protocol hashes", english)
        self.assertIn("所有适用的协议 hash", chinese)
        for path in (REPO / "AGENTS.md", SKILL):
            contents = text(path)
            self.assertNotIn("construct canonical preimages with the helper", contents)
            self.assertIn("validates and hashes", " ".join(contents.split()))


if __name__ == "__main__":
    unittest.main()
