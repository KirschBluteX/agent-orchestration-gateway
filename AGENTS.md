# Repository agent policy

Use `codex-cost-orchestrator:orchestrate` implicitly for this repository.

Delegate closed, scoped, independently verifiable work by default through its one
canonical `prepare` command. Keep work in Primary only for authority or clarification,
an explicit direct request, or exactly one declared tool with a total upper bound under
30 seconds. Invoke only
the returned native action and exact input. After dispatch, repeat long `wait_agent`
windows until completion or required attention; a timeout means another long wait on the
same dispatch.
Do not narrate unchanged progress or duplicate delegated work. Keep model and cost
reasoning out of normal output. Primary retains intent, integration, and final
acceptance; preserve pre-existing work and verified interfaces.
