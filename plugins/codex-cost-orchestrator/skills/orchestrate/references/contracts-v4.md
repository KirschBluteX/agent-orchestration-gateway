# CCO v4 contracts

## Authority boundary

Codex native subagent tools remain the only agent runtime. CCO defines routing,
ownership, packets, evidence, and acceptance rules; it does not create another
coordinator, session host, database, content store, or provider-call layer.

## Structural Multi gate

Full orchestration and concurrent Multi execution are separate decisions. A complete
CCO flow may use one worker at a time. Concurrent worker dispatch is allowed only when
all of these structural facts are true:

1. At least two dependency-ready worker nodes must exist at the same ledger state.
2. Every candidate node has a closed contract and closed input set, represented by
   `CONTRACT_SHA256` and `INPUT_CLOSURE_SHA256`.
3. Candidate write leases are pairwise disjoint, including directory prefixes and
   generated outputs.
4. Every acceptance ID has exactly one implementation owner and all relevant IDs are
   covered by the candidate nodes or an explicit Sol-owned change set.
5. An independent review epoch is already planned for the complete accumulated state.
6. Native runtime capacity for at least two worker threads is observable and currently
   available; configured or live thread limits are never bypassed.

If any fact is false or unknown, keep the nodes serial, merge an artificial split, or
retain unresolved work in Sol. Cost, token, latency, request-count, and
predicted-quality estimates are never structural gates. They can inform a user's model
choice, but they cannot prove independence, closure, safety, or net benefit.

## Canonical protocol hashes

Resolve the helper relative to `SKILL.md`:

```text
<skill-dir>/../../scripts/protocol_hash.py
```

Hash one UTF-8 JSON object from standard input with either command:

```text
protocol_hash.py hash --domain contract
protocol_hash.py hash --domain input_closure
```

The helper rejects duplicate or non-ASCII object keys, floating-point values, non-JSON
numeric constants, integers outside the interoperable safe range, non-NFC strings, and
a non-object root. It also validates the exact cco.v4 schema selected by the hash
domain and `kind`: missing or unknown keys, wrong nested types, invalid policy/null
pairings, and unsorted or duplicate unordered arrays fail before hashing. Input is
limited to 1 MiB and 64 nesting levels. Unicode paths and content remain valid as
values. It sorts object keys, preserves schema-significant array order, and emits
compact UTF-8 JSON. The digest preimage is the
byte concatenation `cco.protocol-hash.v1`, NUL, the ASCII domain, NUL, and those
canonical JSON bytes. The output form is
`sha256:<64 lowercase hexadecimal characters>`.

This is the project-defined `cco.protocol-hash.v1` canonical form, not a claim of full
RFC 8785/JCS compatibility. Independent implementations must reproduce the documented
bytes and known-answer vector rather than substituting another JSON canonicalizer.

Cross-implementation known-answer vector for the `contract` domain:

```text
{"acceptance":[{"criterion":"CLI returns a domain-separated digest","id":"A01"}],"constraints":["Use UTF-8 JSON"],"contract_rev":1,"discretion":["Use a standard SHA-256 implementation"],"exclusions":["Do not change hooks"],"interfaces":["protocol_hash.py hash --domain contract"],"lane":"routine","node":"n01_protocol_hash","objective":"Validate canonical CCO v4 preimages","protocol":"cco.v4","verification":[{"acceptance_ids":["A01"],"expected":"A sha256 digest","id":"V01","operation":"python -m unittest tests/test_protocol_hash.py"}],"write":["plugins/codex-cost-orchestrator/scripts/protocol_hash.py","tests/test_protocol_hash.py"]}
sha256:1e8288d57193a008be6dfd0a60997ce62bf7e0c8ceb39014aa701062f8f12998
```

The canonical contract object contains the protocol version, node and contract
revision, lane, objective, write boundary, interfaces, discretion, constraints,
exclusions, acceptance IDs with their criteria, and verification IDs. It excludes the
worker run, routing choice, attempt counters, generations, and mutable evidence. Model
and effort are execution inputs and are excluded from `CONTRACT_SHA256`.

Every initial worker input closure includes the contract hash, complete acceptance-ID
set, run, attempt, follow-up zero, `fork_turns`, baseline, dependencies, lease and both
generations, selected role, requested model and effort, finite limits, and all content
anchors. Represent paths and other Unicode data as values because canonical object keys
are ASCII-only. `fork_turns` is part of every initial worker and fresh-review input
preimage.

Every live worker follow-up creates a new `INPUT_CLOSURE_SHA256` over the unchanged run
identity plus `PREVIOUS_INPUT_CLOSURE_SHA256`, the next consecutive follow-up counter,
type, bounded delta, verification request, and still-binding inputs. The worker result
echoes the most recent input-closure hash. A bounded live steer may therefore update
the input closure without changing the run or lease. Any other material input change
retires the old owner and starts a new run, attempt, and lease generation.

A worker follow-up is a live in-turn steer delivered with `send_message`, not a new
turn or standalone authority packet. The initial `CCO_WORK` plus the latest valid
hash-chained live steer is the complete worker authority. Transparent V2 reload does
not replay the original per-spawn model and effort overrides into a model-neutral
worker profile. A completed or idle worker therefore receives no `followup_task`;
fence and retire it, inspect its delta, and spawn a new full `CCO_WORK` with explicit
routing. Routing and immutable binding fields are not legal live-steer deltas.
`BINDING_JSON` carries the exact canonical still-binding worker object, including its
acceptance IDs. The follow-up preimage also binds the exact canonical native task path.
The continuation hook can therefore rebuild the new closure and reject an altered
target or acceptance set; Sol still compares both with its actual ledger because the
hook is stateless.

Hashes bind already-closed content; they never make omitted content complete and never
replace passing the worker the actual bounded inputs it needs.

### Canonical preimage schemas

The canonical contract preimage has exactly these top-level keys; unknown or missing
keys are a contract-construction error:

```json
{
  "acceptance": [{"criterion": "<text>", "id": "A01"}],
  "constraints": ["<text>"],
  "contract_rev": 1,
  "discretion": ["<text>"],
  "exclusions": ["<text>"],
  "interfaces": ["<text>"],
  "lane": "routine",
  "node": "n01_auth",
  "objective": "<text>",
  "protocol": "cco.v4",
  "verification": [{"acceptance_ids": ["A01"], "expected": "<text>", "id": "V01", "operation": "<text>"}],
  "write": ["<repository-relative path>"]
}
```

The worker-initial input preimage has the discriminator
`"kind":"worker_initial"` and exactly these other top-level keys:
`acceptance_ids`, `attempt`, `baseline`, `content_anchors`, `contract_rev`, `contract_sha256`,
`dependencies`, `effort_policy`, `followup`, `fork_turns`, `lease`, `lease_generation`,
`model_policy`, `node`, `protocol`, `requested_effort`, `requested_model`, `role`,
`run`, and `stop_generation`. Counters are objects with integer `current` and `limit`;
dependencies and content anchors carry stable IDs plus exact state or content hashes.

The worker-follow-up input preimage has the discriminator
`"kind":"worker_followup"` and exactly these other top-level keys: `binding`, `delta`,
`followup`, `previous_input_closure_sha256`, `protocol`, `target`, `type`, and `verify`.
`binding` repeats every still-binding worker-initial key except `kind`, `followup`, and
`protocol`; `followup` contains the next counter. This makes a changed delta or verify
request produce a different chained hash without changing the run.

The fresh-review input preimage has the discriminator `"kind":"review_fresh"`, binds
`"epoch":"e01"`, and has exactly these other top-level keys: `acceptance`,
`acceptance_ids`, `accumulated_delta`, `allowed_paths`, `attempt`, `baseline`,
`contracts`, `current_state`, `evidence_sha256`, `followup`, `fork_turns`, `goal`, `interfaces`,
`open_risks`, and `protocol`.

The delta-review input preimage has the discriminator `"kind":"review_delta"` and
exactly these other top-level keys: `acceptance_ids`, `attempt`, `contract_status`,
`contracts`, `current_state`, `delta`, `epoch`, `evidence_sha256`, `followup`, `open_risks`,
`previous_input_closure_sha256`, `prior_reviewed_state`, `protocol`, `resolves`, and `target`.

The failure preimage has exactly `acceptance_or_verification_id`, `contract_sha256`,
`diagnostic_ids`, `exit_status`, `failure_class`, `node`, and `protocol`. The evidence
preimage has exactly `acceptance_ids`, `current_state`, `protocol`, and `records`; each
record contains its acceptance IDs, implementation owner, verification ID, operation,
exit status, observed outcome, and exact artifact hashes.

Nested objects are also exact. Counters contain `current` and `limit`; dependencies
contain `id` and `state_sha256`; content anchors contain `id` and `content_sha256`;
review contract references contain `node`, `contract_rev`, and `contract_sha256`; and
delta resolutions contain `id` and `resolution`. A native routing policy uses JSON
`null` for its requested value; user and route-default policies require a nonempty
string. Each evidence record has exactly `acceptance_ids`, `artifact_sha256s`,
`exit_status`, `implementation_owner`, `observed_outcome`, `operation`, `outcome`, and
`verification_id`. `outcome` is `passed`, `failed`, or `unavailable`; a numeric exit
status must agree with it, while a direct observation may use JSON `null`.

All unordered ID, path, dependency, contract, and evidence arrays must be sorted and
duplicate-free by NFC UTF-8 byte order before hashing. A protocol path uses the exact
Git spelling and NFC forward slashes, is repository-relative, and contains no empty,
`.` or `..` segment, absolute/drive/UNC form, backslash, or trailing slash. On a
case-insensitive host, Sol additionally compares active lease paths by `casefold()` so
two spellings of one filesystem path cannot acquire different owners. Only follow-up `delta`, fresh
review `accumulated_delta`, and delta-review `delta` are ordered sequences. Unicode
paths and content are values, never object keys. A
protocol hash is an identity checksum, not authentication, authorization, a
content-addressed store, or proof that omitted input is complete.

## Per-node worker selection

Routine and complex select contract shape, not a model family or effort. The user may
choose model and effort independently for every worker node. Resolve and record each
dimension separately before dispatch:

```text
MODEL_POLICY: user | route_default | native
REQUESTED_MODEL: <exact model or none>
EFFORT_POLICY: user | route_default | native
REQUESTED_EFFORT: <exact effort or none>
```

A user selection always overrides the route recommendation. Route defaults recommend
an ordered finite preference chain of `gpt-5.6-luna` / `max`, then
`gpt-5.6-terra` / `max`, for routine work and `gpt-5.6-terra` / `max` for complex
work. These are availability-aware dispatch preferences, not custom-agent pins. A
native policy omits only that dimension from the spawn call and lets Codex resolve it
from current agent defaults, the parent, or the selected model's default effort.

Build candidate tuples by overlaying the independently resolved dimensions. A
user-selected or native dimension remains fixed while a route-default dimension walks
only its own lane-specific sequence; remove duplicate tuples before dispatch. For
example, a user-selected `high` effort stays `high` while the routine model preference
may advance from Luna to Terra. This preserves independent choice instead of treating
the default model/effort examples as inseparable pins.

Before dispatch, inspect the native capability catalog when the current Codex surface
exposes it. Validate the requested model, supported effort and any task-required input
or tool capability. Do not scrape private rollout state or invent support from a model
name. When no public catalog is exposed, the finite spawn proposals below are the
capability probe; native spawn validation remains authoritative in either case.

A route-default fallback is legal only when its finite ordered preference chain was
fixed before dispatch. Fallback may change only dimensions whose policy is
`route_default`; a user-selected dimension never falls back, and `native` is not an
implicit last candidate. For each candidate, reserve the proposed run identity and
input closure, then call native spawn. A rejected pre-thread dispatch proposal creates
no usable owner and does not consume `ATTEMPT` or `LEASE_GENERATION`; record the native
error and either try the next already-declared route-default candidate with a new input
closure or stop. An unavailable explicit user selection fails closed without fallback.

Native spawn uses `model` and `reasoning_effort`; worker TOML must omit `model` and
`model_reasoning_effort`. Pass each exact user or route-default value with the native
spawn field. Keep the selected routine or complex `agent_type` so the leaf authority
boundary never changes merely to satisfy a model choice. The reviewer remains pinned
to `gpt-5.6-sol` / `high` and never receives worker routing overrides.

Observed role, model, and effort must be recorded before accepting work. An explicit
or route-default value must exactly equal its observed value. A native value still
must be observable and remain consistent across the run. Spawn returning a canonical
task path activates the reserved owner, attempt and lease generation. An observed
mismatch after a usable worker starts is fenced and rejected. Changing effective role,
model, or effort after a usable worker exists starts a new run, consumes an attempt,
issues a new lease generation, and fences the old owner. Never fall back to a generic
or different leaf role.

Use `fork_turns: none` by default or the smallest positive integer needed for a binding
parent turn. Never use `fork_turns: all` with a custom role. Model and effort overrides
alone remain valid with a full-history fork in the pinned Codex source, but CCO always
supplies a custom leaf role, so its full-history path remains invalid.

## Ownership generations and late-result fencing

The Sol-owned ledger stores a monotonically increasing `LEASE_GENERATION` for each
logical lease and a monotonically increasing `STOP_GENERATION` for each node. A new
owner or new worker run receives a new lease generation. A contract-preserving
same-thread follow-up retains both generations only while the owner and lease remain
active.

Increment `STOP_GENERATION` in the ledger before calling native interrupt. Then revoke
or transfer the lease only after the old owner has stopped or been observed idle and
its partial workspace delta has been inspected. A result is eligible only when its
canonical task path, active owner, `RUN`, `LEASE_GENERATION`, and `STOP_GENERATION` all
exactly match the current ledger. The echoed node, contract revision, contract hash,
input-closure hash, lease, attempt, and follow-up must also match the issued packet.

Reject a stale or duplicate result without integrating its report. Still inspect the
actual workspace against every relevant baseline because an interrupted agent may
already have written files. The stop generation is an acceptance fence, not a
filesystem or process-write barrier. Native interrupt, disjoint ownership, and
baseline-relative inspection remain necessary.

Native `Interrupted` is not proof of a terminal agent. Never steer or follow up the
fenced canonical task path again. Observe it idle or terminal, inspect its partial
delta, and only then transfer the lease to a newer generation. If terminal state is
unobservable, keep the lease closed and report the blocker rather than overlapping
owners.

## Finite runs and follow-ups

Each node contract chooses finite positive limits before its first spawn and encodes
the current counters in every packet and result:

```text
ATTEMPT: <current>/<finite-limit>
FOLLOWUP: <current>/<finite-limit>
```

`ATTEMPT` counts every new worker run for one `NODE@CONTRACT_REV` across input-closure,
role, model, and effort changes. `FOLLOWUP` is zero for the initial turn and increments
for each bounded live in-turn correction, verification, or completion steer. A worker
follow-up is valid only while the agent is observably running and node,
contract revision and hash, run, role, selected model and effort, lease, and both
generations remain unchanged; only the chained input closure and follow-up counter
advance.

The attempt limit is fixed before first dispatch and cannot be reset by changing an
input anchor or bumping `CONTRACT_REV` without a material contract change. Follow-up
limits are fixed per run and counters must be strictly consecutive.

Never resend an unchanged failed request after either finite limit is reached. Stop
the lane and choose an evidence-backed intervention: repair the contract or inputs,
change the implementation route with a new run, retain the work in Sol, or report a
blocker. Limits prevent loops; they are not token, cost, time, or predicted-quality
budgets.

Reviewer `ATTEMPT` counts fresh reviewer threads inside one fixed epoch;
reviewer `FOLLOWUP` counts delta-review turns on its one active thread. A cold reviewer
uses another bounded fresh attempt. `rethink` changes the epoch and its acceptance
basis rather than resetting counters in place.

The SubagentStop hook may request one syntax-only envelope repair when the first final
message is malformed. That native continuation is not an implementation `FOLLOWUP`:
it cannot authorize writes, new verification, or changed identity fields, and
`stop_hook_active` prevents another automatic repair. Sol rejects any substantive
delta performed during envelope repair.

## Failure signatures

For a blocked result or failed verification, primary Sol creates `FAILURE_SIGNATURE`
from a canonical object containing the protocol version, node, `CONTRACT_SHA256`,
acceptance or verification ID, stable failure class, exit status when one exists, and
bounded diagnostic identifiers. Exclude the input closure, run, counters, model,
timestamps, absolute machine paths, and full logs. Compute it with:

```text
protocol_hash.py hash --domain failure
```

Worker-provided signatures are claims; Sol recomputes them from observed evidence.
`INPUT_CLOSURE_SHA256` is adjacent comparison evidence, not part of the
failure-signature preimage. If the same failure signature recurs for the same contract
after a non-material input change, the next action must materially change and be
recorded. Repeating the same prompt, blindly spawning another owner, or spending the
remaining counters on identical work is forbidden. A complete result uses
`FAILURE_SIGNATURE: none`; partial, blocked, or primary-verification failure requires a
Sol-recomputed hash.

The result also carries the claimed preimage as
`FAILURE_ACCEPTANCE_OR_VERIFICATION_ID`, `FAILURE_CLASS`, `FAILURE_EXIT_STATUS`, and
`FAILURE_DIAGNOSTIC_IDS`. The hook recomputes that claim's checksum; Sol still decides
the authoritative class from observed evidence.

Normalize failure classes before deciding a retry. `transport_transient` may receive
one same-contract retry; `context_capacity` requires smaller reclosed inputs;
`auth_policy`, `sandbox`, and `bad_request` do not retry automatically; `active_turn`
waits instead of resending; `interrupted_by_fence` requires a new run; and
`verification_failed`, `contract_defect`, or `scope_conflict` require the matching
material intervention. A recurring signature never consumes another counter on the
same request.

## Acceptance evidence closure

Every acceptance criterion receives a stable `Axx` identifier when the work graph is
created. The epoch-wide `ACCEPTANCE_IDS` value is a sorted duplicate-free array. The
ledger assigns exactly one implementation owner to each ID before any worker is
spawned. One node may own several IDs, but an ID cannot be unowned or have competing
implementation owners. Worker packets and results carry the relevant IDs.

Worker evidence remains a claim until primary Sol reruns or directly observes the
acceptance-critical check. For the exact current state, Sol builds a canonical evidence
object containing:

- `CURRENT_STATE` candidate state identifier;
- the complete `ACCEPTANCE_IDS` array from the fixed contracts;
- for every ID, its owner, verification ID, command or observation, decisive outcome,
  exit status when applicable, and any exact artifact hashes; and
- explicit failures or unavailable evidence rather than inferred success.

Every evidence record must be `passed` before a fresh or delta review packet is
eligible. Failed or unavailable records remain diagnostic evidence and cannot support
`ship`.

Every primary evidence record is bound to the same current state. Hash the complete
object with:

```text
protocol_hash.py hash --domain evidence
```

The review packet carries the exact canonical evidence preimage as `EVIDENCE_JSON`,
not a lossy prose summary. Before judging either a fresh or delta review, the reviewer
recomputes `EVIDENCE_SHA256` with the helper and confirms that the preimage matches its
`ACCEPTANCE_IDS` and `CURRENT_STATE`. A fresh `PreToolUse` preflight performs the same
structural check before native spawn; a delta review remains responsible for checking
its new preimage on the existing reviewer thread.

The resulting `EVIDENCE_SHA256` and canonical evidence preimage enter the fresh or
delta review packet. The review input closure binds every
`NODE@CONTRACT_REV#CONTRACT_SHA256`, the complete acceptance-ID array,
`CURRENT_STATE`, `EVIDENCE_SHA256`, accumulated delta, and open risks. The reviewer
result echoes the review `INPUT_CLOSURE_SHA256`, `ACCEPTANCE_IDS`, `EVIDENCE_SHA256`,
and the resulting `REVIEWED_STATE`.

A `ship` verdict is eligible only when the reviewer echoes the complete acceptance-ID
set and exact evidence hash, every required ID has passing primary evidence, and the
reviewed state equals the unchanged current state. Missing, duplicate, stale, or
worker-only evidence makes the verdict ineligible regardless of its text. Any later
mutation invalidates both the evidence closure and the verdict; refresh primary
evidence and run a valid delta or fresh review for the new exact state.

## Names and dispatch identity

Use deterministic lowercase names and retain the canonical task path returned by
native spawn:

```text
work_<node>_<lane>_rNN
review_eNN_rNN
```

Increment `rNN` for every new run. Address that exact canonical path with native
steer, follow-up, wait, or interrupt operations. Worker steers and reviewer deltas bind
that full path in `TARGET`; a leaf name alone is not sufficient. Use `fork_turns: none`
by default and never treat a partial fork as a guaranteed cache hit.

## Work packet

```text
CCO_WORK cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: sha256:<contract-hash>
INPUT_CLOSURE_SHA256: sha256:<input-hash>
LANE: routine | complex
ROLE: cost_orchestrator_routine_worker | cost_orchestrator_complex_worker
RUN: run_n01_auth_r01
ATTEMPT: 1/2
FOLLOWUP: 0/1
FORK_TURNS: none | <smallest positive turn count>
BASELINE: sha256:<workspace-state>
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
MODEL_POLICY: user | route_default | native
REQUESTED_MODEL: <exact model or none>
EFFORT_POLICY: user | route_default | native
REQUESTED_EFFORT: <exact effort or none>
ACCEPTANCE_IDS: [A01,A02]
WRITE:
- <exact path>
OBJECTIVE: <observable outcome>
INTERFACES:
- <fixed behavior or schema>
DISCRETION:
- <bounded implementation choice>
CONSTRAINTS:
- <settled rule and source-of-truth order>
EXCLUSIONS:
- <explicit non-goal>
DEPENDENCIES:
- <exact state or artifact hash, or none>
INPUTS:
- <exact bounded content anchor>
ACCEPTANCE:
- A01: <criterion>
VERIFY:
- V01 [A01]: <command or observation> => <expected evidence>
```

The packet, not inherited conversation, is the worker's authority. Missing material
content is a blocker. `REQUESTED_MODEL` or `REQUESTED_EFFORT` is `none` only when its
policy is `native`; otherwise it is exact.

## Worker follow-up

```text
CCO_WORK_FOLLOWUP cco.v4
NODE: n01_auth
CONTRACT_REV: 1
CONTRACT_SHA256: sha256:<contract-hash>
PREVIOUS_INPUT_CLOSURE_SHA256: sha256:<prior-input-hash>
INPUT_CLOSURE_SHA256: sha256:<new-input-hash>
BINDING_JSON: <exact compact canonical still-binding worker JSON object>
TARGET: /root/<exact canonical task path>
RUN: run_n01_auth_r01
ATTEMPT: 1/2
FOLLOWUP: 1/1
LEASE: wl_n01_auth_r01
LEASE_GENERATION: 1
STOP_GENERATION: 0
ACCEPTANCE_IDS: [A01,A02]
TYPE: correction | verification | completion
DELTA:
- <one bounded request>
VERIFY:
- V01 [A01]: <command or observation> => <expected evidence>
```

Use this only on the same active canonical task path with its initial `CCO_WORK` still
in context. It advances the input-closure chain and follow-up counter but preserves the
contract, acceptance IDs, run, routing, lease, and both generations. `TARGET` must equal
the complete canonical native target used by `send_message`. Routing fields are
intentionally absent: changing one requires a new full work packet and run.

## Worker result

```text
CCO_WORK_RESULT cco.v4
NODE: <node>
CONTRACT_REV: <revision>
CONTRACT_SHA256: sha256:<contract-hash>
INPUT_CLOSURE_SHA256: sha256:<latest-input-hash>
RUN: <run>
ATTEMPT: <current>/<finite-limit>
FOLLOWUP: <current>/<finite-limit>
LEASE: <lease>
LEASE_GENERATION: <generation>
STOP_GENERATION: <generation>
ACCEPTANCE_IDS: [<sorted IDs>]
STATUS: complete | partial | blocked
FAILURE_ACCEPTANCE_OR_VERIFICATION_ID: <Axx/Vxx or none>
FAILURE_CLASS: <stable failure class or none>
FAILURE_EXIT_STATUS: <integer or none>
FAILURE_DIAGNOSTIC_IDS: [<sorted bounded IDs>]
FAILURE_SIGNATURE: sha256:<failure-hash> | none
CHANGED:
- <path and summary, or none>
VERIFIED:
- V01 [A01]: <operation> => <observed result, or none>
JUDGMENT:
- <material choice, or none>
DEVIATIONS:
- <deviation, or none>
BLOCKERS:
- <blocker, or none>
```

This result is a claim. Sol validates its complete identity tuple, runtime routing,
workspace delta, and evidence before changing ledger state.

## Fresh review

```text
CCO_REVIEW cco.v4
EPOCH: e01
MODE: fresh
ATTEMPT: 1/2
FOLLOWUP: 0/1
FORK_TURNS: none
INPUT_CLOSURE_SHA256: sha256:<review-input-hash>
CONTRACTS:
- n01_auth@1#sha256:<contract-hash>
GOAL: <user-visible outcome>
ACCEPTANCE_IDS: [A01,A02]
ACCEPTANCE:
- A01: <criterion>
INTERFACES:
- <fixed public contract>
BASELINE: sha256:<before-state>
CURRENT_STATE: sha256:<current-state>
ALLOWED_PATHS:
- <complete accumulated owned path set>
ACCUMULATED_DELTA:
- <exact diff, revision, or artifact reference>
EVIDENCE_SHA256: sha256:<evidence-hash>
EVIDENCE_JSON: <exact compact canonical evidence JSON object>
OPEN_RISKS:
- <risk, or none>
```

Spawn a new reviewer with `fork_turns: none`. Its first pass is always fresh and does
not inherit an implementer's conclusions. It must recompute `EVIDENCE_SHA256` from
`EVIDENCE_JSON` before using any record.

## Delta review

```text
CCO_REVIEW_DELTA cco.v4
EPOCH: e01
MODE: delta
ATTEMPT: 1/2
FOLLOWUP: 1/1
PREVIOUS_INPUT_CLOSURE_SHA256: sha256:<prior-review-input-hash>
INPUT_CLOSURE_SHA256: sha256:<new-review-input-hash>
TARGET: /root/<exact canonical reviewer task path>
PRIOR_REVIEWED_STATE: sha256:<prior-state>
CURRENT_STATE: sha256:<current-state>
CONTRACT_STATUS: preserved
CONTRACTS:
- n01_auth@1#sha256:<contract-hash>
ACCEPTANCE_IDS: [A01,A02]
EVIDENCE_SHA256: sha256:<evidence-hash>
RESOLVES:
- F01: <implemented bounded fix>
DELTA:
- <exact reference since prior reviewed state>
EVIDENCE_JSON: <exact compact refreshed canonical evidence JSON object>
OPEN_RISKS:
- <risk, or none>
```

Use native follow-up on the same reviewer only while every material epoch field stays
fixed and `TARGET` equals its full canonical native path. The reviewer recomputes the
refreshed evidence hash and checks its state and ID set before delta judgment. Otherwise
fence or retire it and start a fresh review.

## Reviewer result

```text
CCO_REVIEW_RESULT cco.v4
EPOCH: <epoch>
MODE: fresh | delta
ATTEMPT: <current>/<finite-limit>
FOLLOWUP: <current>/<finite-limit>
INPUT_CLOSURE_SHA256: sha256:<review-input-hash>
ACCEPTANCE_IDS: [<complete sorted IDs>]
EVIDENCE_SHA256: sha256:<evidence-hash>
REVIEWED_STATE: sha256:<state-hash>
VERDICT: ship | fix-first | rethink
REASON: <decisive evidence-based reason>
FINDINGS:
- F01: <path, evidence, and required fix, or none>
RESIDUAL_RISK:
- <risk, or none>
```

`ship` applies only to the echoed review closure and state. The reviewer never
implements findings.

## State transitions

1. Close the contracts, acceptance ownership, inputs, routing policy, finite limits,
   and review plan before dispatch.
2. Apply the structural Multi gate to concurrent dispatch; serialize otherwise.
3. Issue one active owner and monotonically newer lease generation per worker run.
4. Chain every same-run follow-up through a new input closure and consecutive counter.
5. Increment the stop-generation fence before native interrupt or owner retirement.
6. Reject stale identity tuples and inspect the actual workspace even after rejection.
7. Recompute failure signatures and require a material intervention on recurrence.
8. Recheck the baseline-relative delta and create primary evidence for every
   acceptance ID at `CURRENT_STATE`.
9. Start each epoch with a fresh reviewer; use bounded delta review only for
   contract-preserving fixes.
10. Accept `ship` only for the exact complete evidence closure and unchanged reviewed
    state. Any mutation invalidates it.
