# CCO v4 extended contracts

Read `worker-core.md` first. Read this file only for concurrent Multi, worker recovery
or live follow-up, or an independent review epoch.

## Authority boundary

Codex native spawn/send/follow-up/wait/interrupt tools remain the only Agent runtime.
CCO adds no coordinator, session host, database, CAS, provider-call layer, or hidden
cost/quality score. Hashes are integrity identities, not encryption or authorization.

## Canonical protocol identities

`protocol_hash.py` accepts these domain-separated cco.v4 objects:

```text
contract
graph_manifest
acceptance_decision
acceptance_chain
input_closure
failure
evidence
```

Input is compact UTF-8 JSON, at most 1 MiB and 64 levels. Object keys are ASCII and
unique; strings are NFC; floats, non-JSON constants, and unsafe integers are rejected.
Unordered arrays are sorted and duplicate-free in NFC UTF-8 byte order. Repository
scopes are objects `{ "kind": "exact|prefix", "path": "..." }`; packet/CLI spelling
is `exact:<path>` or `prefix:<directory>`.

The contract object has exactly:

```json
{
  "acceptance": [{"criterion":"<text>","id":"A01"}],
  "constraints": ["<text>"],
  "contract_rev": 1,
  "discretion": ["<text>"],
  "exclusions": ["<text>"],
  "interfaces": ["<text>"],
  "lane": "routine|complex|sol",
  "node": "n01_auth",
  "objective": "<text>",
  "protocol": "cco.v4",
  "risk_flags": ["<enumerated flag>"],
  "verification": [{"acceptance_ids":["A01"],"expected":"<text>","id":"V01","operation":"<text>"}],
  "write": [{"kind":"exact","path":"src/auth.py"}]
}
```

Known-answer contract vector used by the tests:

```text
{"acceptance":[{"criterion":"CLI returns a domain-separated digest","id":"A01"}],"constraints":["Use UTF-8 JSON"],"contract_rev":1,"discretion":["Use a standard SHA-256 implementation"],"exclusions":["Do not change hooks"],"interfaces":["protocol_hash.py hash --domain contract"],"lane":"routine","node":"n01_protocol_hash","objective":"Validate canonical CCO v4 preimages","protocol":"cco.v4","risk_flags":[],"verification":[{"acceptance_ids":["A01"],"expected":"A sha256 digest","id":"V01","operation":"python -m unittest tests/test_protocol_hash.py"}],"write":[{"kind":"exact","path":"plugins/codex-cost-orchestrator/scripts/protocol_hash.py"},{"kind":"exact","path":"tests/test_protocol_hash.py"}]}
sha256:1e9af0d5512998e82580f308ab44220d382dd025fb5683d4b975d38ac64cea94
```

The graph manifest contains `protocol`, sorted full `{contract,
contract_sha256}` records, and sorted `{acceptance_id, implementation_owner}` records.
It recomputes every contract hash; requires declaring-node owners and globally unique
A/V IDs; rejects any exact/prefix overlap or portable case alias; and caps the entire
graph at 128 distinct scopes.

An acceptance decision contains exactly `protocol`, `revision`,
`graph_manifest_sha256`, `mode`, `reasons`, and `previous_decision_sha256`. Revision 1
uses null previous hash. `primary` has no reasons and is valid only for one routine,
risk-free graph. Initial independent reasons are `complex_lane`, `multiple_contracts`,
`sol_owned_change`, `declared_risk`, or `explicit_independent_review` and must match the
manifest. Revision 2 must link revision 1, use `independent`, and name at least one:

```text
material_judgment routing_mismatch scope_surprise verification_failure
worker_deviation worker_followup worker_partial_or_blocked worker_retry
```

The acceptance chain embeds the complete immutable graph manifest plus one or two
`{decision, decision_sha256}` records. It therefore makes graph closure and one-way
upgrade independently recomputable. This self-contained graph is repeated in worker
packets; it trades some context for a stateless pre-spawn global authority check. Do
not repeat surrounding reference prose or unrelated repository content.

### Extended canonical input preimages

Sol constructs these JSON objects. `protocol_hash.py` validates each complete object
for its domain and then hashes it; it does not construct, fill, or infer a preimage.
The following are exact key sets. `sha256:<…>` denotes `sha256:` plus 64 lowercase
hexadecimal characters and is not itself hashable. Packet-only fields such as
`INPUT_CLOSURE_SHA256` or `EVIDENCE_JSON` are not silently added to a domain preimage;
`target` appears only in the preimages that explicitly show it below.

#### `worker_followup` (`input_closure` domain)

`binding` is the initial-worker object with exactly its immutable binding keys: it
omits `kind`, `protocol`, and `followup` but retains every other key shown.

```json
{
  "acceptance_chain_sha256": "sha256:<chain>",
  "binding": {
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
    "graph_manifest_sha256": "sha256:<manifest>",
    "lease": "wl_n01_auth_r01",
    "lease_generation": 1,
    "model_policy": "native",
    "node": "n01_auth",
    "requested_effort": null,
    "requested_model": null,
    "role": "cost_orchestrator_routine_worker",
    "run": "run_n01_auth_r01",
    "stop_generation": 0
  },
  "delta": ["<bounded request>"],
  "followup": {"current": 1, "limit": 2},
  "kind": "worker_followup",
  "previous_input_closure_sha256": "sha256:<prior-input>",
  "protocol": "cco.v4",
  "target": "/root/work_n01_auth_r01",
  "type": "correction",
  "verify": [{
    "acceptance_ids": ["A01"],
    "expected": "<expected evidence>",
    "id": "V01",
    "operation": "<exact command>"
  }]
}
```

`verify` may be an empty array, but every listed acceptance ID must stay inside the
binding. `followup.current` starts at 1 and is bounded by 2; `target` is the exact
canonical native task path.

#### `review_fresh` (`input_closure` domain)

```json
{
  "acceptance": [{"criterion": "<criterion>", "id": "A01"}],
  "acceptance_ids": ["A01"],
  "accumulated_delta": ["<exact accumulated delta reference>"],
  "allowed_paths": [{"kind": "exact", "path": "src/auth.py"}],
  "attempt": {"current": 1, "limit": 2},
  "baseline": "sha256:<baseline>",
  "acceptance_chain_sha256": "sha256:<chain>",
  "contracts": [{
    "contract_rev": 1,
    "contract_sha256": "sha256:<contract>",
    "node": "n01_auth"
  }],
  "current_state": "sha256:<current-state>",
  "epoch": "e01",
  "evidence_sha256": "sha256:<evidence>",
  "followup": {"current": 0, "limit": 2},
  "fork_turns": "none",
  "goal": "<goal>",
  "graph_manifest_sha256": "sha256:<manifest>",
  "interfaces": ["<interface>"],
  "kind": "review_fresh",
  "open_risks": [],
  "protocol": "cco.v4"
}
```

`acceptance_ids` must equal the sorted IDs in `acceptance`; `attempt.current` is 1–2,
`followup.current` is exactly 0, and `fork_turns` is exactly `none`.

#### `review_delta` (`input_closure` domain)

```json
{
  "acceptance_ids": ["A01"],
  "acceptance_chain_sha256": "sha256:<chain>",
  "attempt": {"current": 1, "limit": 2},
  "contract_status": "preserved",
  "contracts": [{
    "contract_rev": 1,
    "contract_sha256": "sha256:<contract>",
    "node": "n01_auth"
  }],
  "current_state": "sha256:<current-state>",
  "delta": ["<exact contract-preserving delta>"],
  "epoch": "e01",
  "evidence_sha256": "sha256:<evidence>",
  "followup": {"current": 1, "limit": 2},
  "graph_manifest_sha256": "sha256:<manifest>",
  "kind": "review_delta",
  "open_risks": [],
  "previous_input_closure_sha256": "sha256:<prior-review-input>",
  "prior_reviewed_state": "sha256:<prior-state>",
  "protocol": "cco.v4",
  "resolves": [{"id": "F01", "resolution": "<bounded resolution>"}],
  "target": "/root/review_e01_r01"
}
```

`contract_status` is currently only `preserved`; `followup.current` is 1–2, and
`resolves` must contain at least one sorted record. The helper's exact
`review_delta` object has no `EVIDENCE_JSON` member; that canonical evidence object is
carried by the readable review packet and checked separately.

#### `failure` (`failure` domain)

```json
{
  "acceptance_or_verification_id": "V01",
  "contract_sha256": "sha256:<contract>",
  "diagnostic_ids": ["D_VERIFY_FAILED"],
  "exit_status": 1,
  "failure_class": "verification_failed",
  "node": "n01_auth",
  "protocol": "cco.v4"
}
```

`acceptance_or_verification_id` matches `Axx` or `Vxx`; `diagnostic_ids` is sorted and
may be empty; `exit_status` may be an integer or `null`; and `failure_class` must name
an observed failure (it cannot be `none`). Run `protocol_hash.py hash --domain
failure` on this exact object. These shapes intentionally contain no run, model,
timestamp, absolute path, full log, or input-closure fields.

## Structural Multi gate

Concurrent worker dispatch is allowed only when all are true:

1. At least two nodes are dependency-ready at the same ledger state.
2. Every contract and initial input closure is already hashed.
3. Write scopes are pairwise disjoint across the full graph.
4. Every acceptance ID has exactly one declaring-node owner.
5. The acceptance chain already ends in `independent`.
6. Native capacity for at least two worker tasks is observable and available.

Otherwise serialize still-disjoint nodes, merge overlapping/artificial nodes under
one owner, or keep unresolved work in Sol. Price, predicted tokens, latency, request
count, file count, and predicted quality are advisory only.

## Runs, follow-ups, leases, and fences

One `NODE@CONTRACT_REV` may use at most three worker runs. Each run may use at most two
live follow-ups. One review epoch may use at most two fresh reviewer threads; each
thread may use at most two delta turns. Counters prevent loops; they are not budgets.

`RUN` and `LEASE` suffix `rNN` equals the worker attempt. A new run increments the
lease generation. Increment `STOP_GENERATION` before interrupt/retirement, observe the
old task idle or terminal, inspect its partial delta, then transfer the lease. A late
result is eligible only when canonical task path, hashes, run, counters, lease and both
generations equal the Sol ledger.

### Live worker follow-up

Use native `send_message` only while the worker is observably running. The follow-up
binds the original canonical `BINDING_JSON`, exact task target, latest acceptance
chain, previous input hash, consecutive counter, bounded delta, and verification:

```text
CCO_WORK_FOLLOWUP cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: sha256:<contract>
PREVIOUS_INPUT_CLOSURE_SHA256: sha256:<prior-input>
INPUT_CLOSURE_SHA256: sha256:<new-input>
ACCEPTANCE_CHAIN_SHA256: sha256:<current-chain>
ACCEPTANCE_CHAIN_JSON: <canonical compact JSON>
BINDING_JSON: <canonical initial worker binding>
TARGET: /root/work_n01_auth_routine_r01
RUN: run_n01_auth_r01
ATTEMPT: 1/3
FOLLOWUP: 1/2
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
ACCEPTANCE_IDS: [A01]
TYPE: correction | verification | completion
DELTA:
- <bounded request>
VERIFY:
- V01 [A01]: <operation> => <expected>
```

A live follow-up from a primary run must append decision revision 2 with reason
`worker_followup`; the hook verifies the prior chain hash from the binding. Never use
`followup_task` for a completed model-neutral worker. Fence it and start a complete new
run with explicit routing.

## Failure signatures

For partial/blocked work or failed primary verification, Sol hashes the protocol,
node, contract hash, A/V failure ID, normalized failure class, optional exit status,
and bounded diagnostic IDs with `--domain failure`. Exclude run, model, timestamps,
absolute paths, full logs, and input hash. A recurring signature requires a materially
different intervention. Normalize transport, context-capacity, auth/policy, sandbox,
bad-request, active-turn, fence, verification, contract, and scope failures before any
retry.

Worker results echo both graph and latest chain identities:

```text
CCO_WORK_RESULT cco.v4
NODE: <node>
CONTRACT_REV: <revision>
CONTRACT_SHA256: sha256:<contract>
INPUT_CLOSURE_SHA256: sha256:<latest-input>
GRAPH_MANIFEST_SHA256: sha256:<manifest>
ACCEPTANCE_CHAIN_SHA256: sha256:<latest-chain>
RUN: <run>
ATTEMPT: <current>/<limit>
FOLLOWUP: <current>/<limit>
LEASE: <lease>
LEASE_GENERATION: <generation>
STOP_GENERATION: <generation>
ACCEPTANCE_IDS: [<IDs>]
STATUS: complete | partial | blocked
FAILURE_ACCEPTANCE_OR_VERIFICATION_ID: <Axx/Vxx or none>
FAILURE_CLASS: <class or none>
FAILURE_EXIT_STATUS: <integer or none>
FAILURE_DIAGNOSTIC_IDS: [<IDs>]
FAILURE_SIGNATURE: sha256:<failure> | none
CHANGED:
- <path/summary, or none>
VERIFIED:
- V01 [A01]: <operation> => <observation, or none>
JUDGMENT:
- <choice, or none>
DEVIATIONS:
- <deviation, or none>
BLOCKERS:
- <blocker, or none>
```

## Evidence and independent review

Evidence has exactly `protocol`, `current_state`, sorted `acceptance_ids`, canonical
`acceptance_chain`, `acceptance_chain_sha256`, and one sorted record for every graph
verification. Each record exactly matches its contract operation, acceptance IDs, and
declaring-node owner. Review is eligible only when every outcome is `passed`.

Fresh and delta review input closures bind `GRAPH_MANIFEST_SHA256`,
`ACCEPTANCE_CHAIN_SHA256`, contract references, acceptance IDs, current state,
evidence hash, delta, and risks. `EVIDENCE_JSON` carries the full recomputable chain.

```text
CCO_REVIEW cco.v4
EPOCH: e01
MODE: fresh
ATTEMPT: 1/2
FOLLOWUP: 0/2
FORK_TURNS: none
INPUT_CLOSURE_SHA256: sha256:<review-input>
GRAPH_MANIFEST_SHA256: sha256:<manifest>
ACCEPTANCE_CHAIN_SHA256: sha256:<chain>
CONTRACTS:
- n01_auth@1#sha256:<contract>
GOAL: <goal>
ACCEPTANCE_IDS: [A01]
ACCEPTANCE:
- A01: <criterion>
INTERFACES:
- <interface, or none>
BASELINE: sha256:<baseline>
CURRENT_STATE: sha256:<state>
ALLOWED_PATHS:
- exact:<path>
ACCUMULATED_DELTA:
- <exact reference>
EVIDENCE_SHA256: sha256:<evidence>
EVIDENCE_JSON: <canonical compact JSON>
OPEN_RISKS:
- <risk, or none>
```

```text
CCO_REVIEW_DELTA cco.v4
EPOCH: e01
MODE: delta
ATTEMPT: 1/2
FOLLOWUP: 1/2
PREVIOUS_INPUT_CLOSURE_SHA256: sha256:<prior-review-input>
INPUT_CLOSURE_SHA256: sha256:<new-review-input>
TARGET: /root/review_e01_r01
PRIOR_REVIEWED_STATE: sha256:<prior-state>
CURRENT_STATE: sha256:<state>
CONTRACT_STATUS: preserved
GRAPH_MANIFEST_SHA256: sha256:<manifest>
ACCEPTANCE_CHAIN_SHA256: sha256:<chain>
CONTRACTS:
- n01_auth@1#sha256:<contract>
ACCEPTANCE_IDS: [A01]
EVIDENCE_SHA256: sha256:<evidence>
RESOLVES:
- F01: <resolution>
DELTA:
- <exact reference>
EVIDENCE_JSON: <canonical compact JSON>
OPEN_RISKS:
- <risk, or none>
```

Reviewer output echoes the same graph, chain, evidence, and exact reviewed state:

```text
CCO_REVIEW_RESULT cco.v4
EPOCH: <epoch>
MODE: fresh | delta
ATTEMPT: <current>/<limit>
FOLLOWUP: <current>/<limit>
INPUT_CLOSURE_SHA256: sha256:<review-input>
GRAPH_MANIFEST_SHA256: sha256:<manifest>
ACCEPTANCE_CHAIN_SHA256: sha256:<chain>
ACCEPTANCE_IDS: [<IDs>]
EVIDENCE_SHA256: sha256:<evidence>
REVIEWED_STATE: sha256:<state>
VERDICT: ship | fix-first | rethink
REASON: <evidence-based reason>
FINDINGS:
- F01: <finding, or none>
RESIDUAL_RISK:
- <risk, or none>
```

`ship` is eligible only for the exact unchanged current state and complete passing
evidence closure. Contract/ownership/safety/acceptance changes require a fresh epoch;
contract-preserving fixes may use the bounded same-reviewer delta path.
