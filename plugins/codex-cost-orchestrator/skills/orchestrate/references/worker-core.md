# CCO v4 worker core

Read this file before the first orchestrated worker spawn. It is the short successful
path for one clean routine node and primary acceptance. Load `runtime-gates.md` only
for a profile/runtime mismatch, a permission or isolation concern, or recovery detail.
Load `contracts-v4.md` only for concurrent Multi, retry/live follow-up, or independent
review.

## Runtime preflight and successful inspection

Resolve commands from the plugin root. Before the short primary path, check its
routine profile and record it in the task's cached checked set:

```text
python scripts/install_agents.py --workspace <repo> --check --profile routine
```

Check `complex` only when that lane is used. Check `reviewer` before an initially
independent graph; it need not be in the initial checked set for a clean primary graph.
Worker profiles are model-neutral. Native spawn must expose the exact custom role,
`fork_turns`, and every user-selected or route-default model/effort override. Never
substitute an ordinary agent.

After a usable spawn, inspect the returned child UUID or canonical task path on the
normal success path:

```text
python scripts/inspect_agent_runtime.py \
  --expect-role cost_orchestrator_routine_worker \
  --expect-model <selected-model> --expect-effort <selected-effort> \
  <child-uuid-or-canonical-task-path>
```

For a `native` model or effort dimension, omit only its corresponding expectation
flag. Success requires exit status 0, the exact role, and a nonempty, stable emitted
model and effort; every user or route-default value must match its expectation. On a
mismatch, missing metadata, unavailable profile, or permission/isolation concern, stop
and load `runtime-gates.md`; do not continue the successful-path procedure by guess.

Capture the lease baseline outside the repository:

```text
python scripts/workspace_state.py capture --repo <repo> --output <baseline.json>
```

## Close graph authority before dispatch

Every contract uses `cco.v4`, explicit `exact`/`prefix` scope objects, sorted stable
acceptance and verification IDs, and sorted `risk_flags`. Valid risk flags are:

```text
authentication_authorization build_release concurrency dependency_boundary
destructive_data external_side_effect migration nondeterministic_verification
public_interface schema security
```

Sol constructs every canonical JSON preimage. `protocol_hash.py` only validates the
complete submitted preimage and hashes it; it does not construct preimages. Hash each
full contract, then build one immutable graph manifest containing all full contract
records and acceptance owners. It rejects duplicate A/V IDs, an owner other than the
declaring node, overlapping or portable case-alias scopes, and more than 128 graph-wide
scopes:

```text
python scripts/protocol_hash.py hash --domain contract
python scripts/protocol_hash.py hash --domain graph_manifest
```

Create acceptance decision revision 1. A single routine graph with no risk flags may
use `primary` with no reasons. Otherwise use `independent` with the exact structural
reasons (`complex_lane`, `multiple_contracts`, `sol_owned_change`, `declared_risk`) or
`explicit_independent_review`. Put the decision and manifest in a one-record canonical
acceptance chain and hash both:

```text
python scripts/protocol_hash.py hash --domain acceptance_decision
python scripts/protocol_hash.py hash --domain acceptance_chain
```

The canonical chain is self-contained: it embeds the graph manifest, its hash, and
one or two hash-linked decisions. Revision 2 may only append a primary-to-independent
upgrade. Never rewrite revision 1.

## Initial worker packet

Construct the contract and input JSON before formatting this envelope, then use the
helper to validate and hash them. `GRAPH_MANIFEST_SHA256`, canonical
`ACCEPTANCE_CHAIN_JSON`, and its hash are mandatory; the spawn hook checks that this
worker's complete contract is uniquely present in the full graph.

```text
CCO_WORK cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: sha256:<contract>
INPUT_CLOSURE_SHA256: sha256:<input>
GRAPH_MANIFEST_SHA256: sha256:<manifest>
ACCEPTANCE_CHAIN_SHA256: sha256:<chain>
ACCEPTANCE_CHAIN_JSON: <canonical compact JSON>
LANE: routine | complex
ROLE: cost_orchestrator_routine_worker | cost_orchestrator_complex_worker
RUN: run_n01_auth_r01
ATTEMPT: 1/3
FOLLOWUP: 0/2
FORK_TURNS: none | <small positive integer>
BASELINE: sha256:<workspace state>
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
MODEL_POLICY: user | route_default | native
REQUESTED_MODEL: <exact value or none>
EFFORT_POLICY: user | route_default | native
REQUESTED_EFFORT: <exact value or none>
ACCEPTANCE_IDS: [A01]
WRITE:
- exact:<path>
OBJECTIVE: <observable outcome>
INTERFACES:
- <fixed interface, or none>
DISCRETION:
- <bounded choice, or none>
CONSTRAINTS:
- <binding rule, or none>
EXCLUSIONS:
- <non-goal, or none>
RISK_FLAGS:
- <sorted flag, or none>
DEPENDENCIES:
- <id>#sha256:<state>, or none
INPUTS:
- <id>#sha256:<content>, or none
ACCEPTANCE:
- A01: <criterion>
VERIFY:
- V01 [A01]: <operation> => <expected evidence>
```

The worker-initial hash binds the manifest and chain hashes as well as routing,
`fork_turns`, run/counters, baseline, lease/generations, dependencies, inputs, and IDs.
Use `fork_turns: none` unless a bounded parent decision is indispensable.

### Exact `worker_initial` input preimage

Use every key below and no others. `sha256:<…>` means `sha256:` followed by exactly 64
lowercase hexadecimal characters; it is notation, not a literal hash. Empty anchors
and dependencies are valid. Nonempty records are exactly
`{"content_sha256":"sha256:<…>","id":"<anchor-id>"}` and
`{"id":"<dependency-id>","state_sha256":"sha256:<…>"}` respectively.

```json
{
  "acceptance_chain_sha256": "sha256:<chain>",
  "acceptance_ids": ["A01"],
  "attempt": {"current": 1, "limit": 3},
  "baseline": "sha256:<workspace-state>",
  "content_anchors": [],
  "contract_rev": 1,
  "contract_sha256": "sha256:<contract>",
  "dependencies": [],
  "effort_policy": "native",
  "fork_turns": "none",
  "followup": {"current": 0, "limit": 2},
  "graph_manifest_sha256": "sha256:<manifest>",
  "kind": "worker_initial",
  "lease": "wl_n01_auth_r01",
  "lease_generation": 1,
  "model_policy": "native",
  "node": "n01_auth",
  "protocol": "cco.v4",
  "requested_effort": null,
  "requested_model": null,
  "role": "cost_orchestrator_routine_worker",
  "run": "run_n01_auth_r01",
  "stop_generation": 0
}
```

`attempt.current` is 1–3 and its `rNN` suffix must equal the current value;
`followup.current` is exactly 0 and its limit is 1–2. `fork_turns` is `none` or a
positive integer string. `model_policy` and `effort_policy` are `user`,
`route_default`, or `native`; only `native` requires its paired requested value to be
`null`. Hash this object with `--domain input_closure` before formatting the envelope.

## Primary verification and finish

Treat the result as a claim. Match its complete identity, graph/chain hashes, latest
input closure, route, lease, and generations; compare the actual baseline-relative
delta and rerun every contract verification. For serialized disjoint nodes, a passing
verify can emit the next baseline without another full capture:

```text
python scripts/workspace_state.py verify --repo <repo> --baseline <baseline.json> \
  --allow exact:<path> --next-baseline <next-baseline.json>
```

Evidence contains the canonical acceptance chain, its hash, one record for every
graph verification, and one exact `CURRENT_STATE`. All outcomes must pass. Primary
acceptance is valid only while the chain still has one eligible `primary` decision.
Any retry, live follow-up, deviation, scope surprise, route mismatch, verification
failure, partial/blocked result, or material judgment appends revision 2 and requires
the independent-review path in `contracts-v4.md`.

### Exact primary-path evidence preimage

`EVIDENCE_JSON` is the exact object below. Its `acceptance_chain` is the complete
object also serialized as `ACCEPTANCE_CHAIN_JSON`, not a hash or a shortened copy. Its nested contract is the
same exact contract whose digest appears everywhere else. The evidence record must use
the contract's exact verification operation, acceptance IDs, and implementation owner.

```json
{
  "acceptance_ids": ["A01"],
  "acceptance_chain": {
    "decisions": [{
      "decision": {
        "graph_manifest_sha256": "sha256:<manifest>",
        "mode": "primary",
        "previous_decision_sha256": null,
        "protocol": "cco.v4",
        "reasons": [],
        "revision": 1
      },
      "decision_sha256": "sha256:<decision>"
    }],
    "graph_manifest": {
      "acceptance_owners": [{
        "acceptance_id": "A01",
        "implementation_owner": "n01_auth"
      }],
      "contracts": [{
        "contract": {
          "acceptance": [{"criterion": "<criterion>", "id": "A01"}],
          "constraints": ["<constraint>"],
          "contract_rev": 1,
          "discretion": ["<bounded choice>"],
          "exclusions": ["<non-goal>"],
          "interfaces": ["<fixed interface>"],
          "lane": "routine",
          "node": "n01_auth",
          "objective": "<observable outcome>",
          "protocol": "cco.v4",
          "risk_flags": [],
          "verification": [{
            "acceptance_ids": ["A01"],
            "expected": "<expected evidence>",
            "id": "V01",
            "operation": "<exact command>"
          }],
          "write": [{"kind": "exact", "path": "src/auth.py"}]
        },
        "contract_sha256": "sha256:<contract>"
      }],
      "protocol": "cco.v4"
    },
    "graph_manifest_sha256": "sha256:<manifest>",
    "protocol": "cco.v4"
  },
  "acceptance_chain_sha256": "sha256:<chain>",
  "current_state": "sha256:<current-workspace-state>",
  "protocol": "cco.v4",
  "records": [{
    "acceptance_ids": ["A01"],
    "artifact_sha256s": [],
    "exit_status": 0,
    "implementation_owner": "n01_auth",
    "observed_outcome": "<actual observation>",
    "operation": "<exact command>",
    "outcome": "passed",
    "verification_id": "V01"
  }]
}
```

Hash this exact object with `--domain evidence`. It is eligible for primary acceptance
only when every record is `passed`, every graph verification occurs exactly once, and
the primary decision remains eligible. Any later anomaly appends revision 2 and requires
the independent path.

When that primary-to-independent upgrade occurs, inspect the cached checked set. If it
does not include the reviewer, run
`python scripts/install_agents.py --workspace <repo> --check --profile reviewer` and
record success before any fix or review. A failed reviewer profile check stops
corrective worker work and review until resolved; it is not safe to defer the check to
the fresh reviewer spawn.
