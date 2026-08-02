# CCO v3 contracts

## Contents

- [Vocabulary](#vocabulary)
- [Names and fork policy](#names-and-fork-policy)
- [Work packet](#work-packet)
- [Worker follow-up](#worker-follow-up)
- [Worker result](#worker-result)
- [Fresh review](#fresh-review)
- [Delta review](#delta-review)
- [Reviewer result](#reviewer-result)
- [State transitions](#state-transitions)

## Vocabulary

| Term | Meaning |
| --- | --- |
| `cco.v3` | Packet/schema version echoed by every result. |
| `NODE` | Stable logical work unit, such as `n01_auth`. |
| `CONTRACT_REV` | Revision of material node requirements. |
| `RUN` | One role-pinned agent-thread incarnation. |
| `LANE` | `routine`, `complex`, or `review`. |
| `LEASE` | Orchestrator-issued behavioral exclusive write boundary. |
| `EPOCH` | Period with fixed goal, architecture, interfaces, ownership, and acceptance. |
| `MODE` | Reviewer mode: `fresh` or `delta`. |
| `STATE` | Identifier binding baseline/current files, diff, and evidence. |

The lease is not a filesystem lock. The orchestrator must detect overlaps, serialize
shared paths, and verify the actual baseline-relative delta.

## Names and fork policy

Use deterministic names with lowercase letters, digits, and underscores:

```text
work_<node>_<lane>_rNN
review_eNN
consult_<node>_rNN
```

Increment `rNN` only when a new thread is required. Store the canonical path returned
by `spawn_agent`; use that exact path for `send_message` and `followup_task`.

Use `fork_turns: none` by default. Use the smallest positive integer only when an
indispensable recent parent turn should be inherited. Never use `all` with a custom
`agent_type`. A fresh review always uses `none`; follow-ups continue the existing
thread and have no fork setting.

## Work packet

```text
CCO_WORK cco.v3
NODE: n01_auth
CONTRACT_REV: 1
LANE: routine
RUN: run_n01_auth_r01
BASELINE: <state-id>
LEASE: wl_n01_auth_r01
WRITE:
- <exact path>
OBJECTIVE: <observable outcome>
INTERFACES:
- <signature/schema/behavior that remains compatible>
DISCRETION:
- <implementation choices the worker may make>
CONSTRAINTS:
- <settled rules and source-of-truth order>
EXCLUSIONS:
- <explicit non-goal>
DEPENDENCIES:
- <required state, or none>
DONE_WHEN:
- <acceptance criterion>
VERIFY:
- <command> => <expected evidence>
```

The current packet supersedes inherited conversational decisions. If the packet omits
a material requirement, the worker must stop instead of inferring new authority.

## Worker follow-up

```text
CCO_WORK_FOLLOWUP cco.v3
NODE: n01_auth
CONTRACT_REV: 1
RUN: run_n01_auth_r01
LEASE: wl_n01_auth_r01
FOLLOWUP: f01
TYPE: correction | verification | completion
DELTA:
- <bounded request that does not change the contract>
VERIFY:
- <command> => <expected evidence>
```

Use a follow-up only while node, contract revision, run, role, and lease remain
unchanged. Reject a result whose `RUN` does not match the single-flight ledger.

## Worker result

```text
CCO_WORK_RESULT cco.v3
NODE: <node>
CONTRACT_REV: <revision>
RUN: <run>
LEASE: <lease>
STATUS: complete | partial | blocked
CHANGED:
- <path and summary, or none>
VERIFIED:
- <command and decisive result, or none>
JUDGMENT:
- <material choice, or none>
DEVIATIONS:
- <deviation, or none>
BLOCKERS:
- <blocker, or none>
```

## Fresh review

```text
CCO_REVIEW cco.v3
EPOCH: e01
MODE: fresh
CONTRACTS:
- n01_auth@1
GOAL: <user-visible outcome>
ACCEPTANCE:
- <fixed acceptance criterion>
INTERFACES:
- <fixed public contract>
BASELINE: <before-state-id>
CURRENT_STATE: <current-state-id>
ALLOWED_PATHS:
- <complete accumulated owned change set>
ACCUMULATED_DELTA:
- <actual diff/revision/artifact reference>
EVIDENCE:
- <command> => <actual primary-session result>
OPEN_RISKS:
- <risk, or none>
```

Spawn with `task_name: review_e01`, `agent_type: cost_orchestrator_reviewer`, and
`fork_turns: none`.

## Delta review

```text
CCO_REVIEW_DELTA cco.v3
EPOCH: e01
MODE: delta
PRIOR_REVIEWED_STATE: <state-id>
CURRENT_STATE: <state-id>
CONTRACT_STATUS: preserved
RESOLVES:
- F01 => <implemented fix>
DELTA:
- <actual reference since prior reviewed state>
UPDATED_EVIDENCE:
- <command> => <actual result>
```

Use `followup_task` on the same reviewer. A delta review is independent of the
implementer but is not a second fresh-context review.

## Reviewer result

```text
CCO_REVIEW_RESULT cco.v3
EPOCH: <epoch>
MODE: fresh | delta
REVIEWED_STATE: <state-id>
VERDICT: ship | fix-first | rethink
REASON: <decisive evidence-based reason>
FINDINGS:
- F01: <path/evidence/required fix, or none>
RESIDUAL_RISK:
- <risk, or none>
```

`ship` applies only to `REVIEWED_STATE`. The reviewer never implements findings.

## State transitions

1. Acquire a non-overlapping lease before spawn.
2. Hold it across contract-preserving follow-ups.
3. Verify baseline-relative paths and evidence before integration.
4. Revoke it only after integration or explicit abandonment.
5. Transfer it only after the previous owner has stopped and partial state is judged.
6. Increment `CONTRACT_REV` and start a new worker run for material contract changes.
7. Start the first review of every epoch fresh.
8. Use the same reviewer for contract-preserving delta fixes.
9. Increment the epoch for changes to goal, architecture, public interfaces or
   schemas, safety constraints, write ownership, exclusions, or acceptance.
10. Treat `rethink` as a mandatory new epoch.
11. Invalidate `ship` after any later mutation.
