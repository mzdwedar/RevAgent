# The authority decision from agentstack.policy, as Rego. Default deny; every refusal
# that applies is collected, and `primary` is the one the audit record names.
package agentstack.authority

default allow := false

allow if count(deny) == 0

deny contains "envelope.expired" if input.now >= input.envelope.expires_at

deny contains "identity.mismatch" if input.envelope.acts_as != input.tool.acts_as

deny contains "scope.missing" if not input.tool.scope in input.envelope.scopes

deny contains "tenant.boundary" if not in_tenant

in_tenant if startswith(input.request.resource, concat("", [input.envelope.tenant, "/"]))

deny contains "approval.stale" if {
	input.tool.tier != "none"
	input.approval.exists
	input.approval.snapshot != input.state_snapshot
}

# ALWAYS: a person, every time. A policy grant never satisfies it.
deny contains "approval.required" if {
	input.tool.tier == "always"
	not by_a_human
}

by_a_human if {
	input.approval.exists
	input.approval.granted_by == "human"
}

# PRE_COMMIT with nothing on record: the rule may grant - reversible, within the tenant.
deny contains "approval.required" if {
	input.tool.tier == "pre_commit"
	not input.approval.exists
	not pre_commit_permits
}

pre_commit_permits if {
	input.tool.reversible
	in_tenant
}

order := ["envelope.expired", "identity.mismatch", "scope.missing", "tenant.boundary",
	"approval.stale", "approval.required"]

default primary := ""

primary := ranked[0] if {
	ranked := [r | some r in order; r in deny]
	count(ranked) > 0
}

verdict := {"allow": allow, "deny": deny, "primary": primary}
