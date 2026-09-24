// The authority decision from agentstack.policy, as Cedar. forbid overrides permit,
// which is "checks are conjunctive" without anyone having to remember it.

@id("allow")
permit (principal, action, resource);

@id("envelope.expired")
forbid (principal, action, resource)
when { context.now >= context.envelope.expires_at };

@id("identity.mismatch")
forbid (principal, action, resource)
when { context.envelope.acts_as != context.tool.acts_as };

@id("scope.missing")
forbid (principal, action, resource)
unless { context.envelope.scopes.contains(context.tool.scope) };

// Cedar's `like` takes a literal pattern, so "starts with the envelope's tenant" cannot
// be written here. The caller parses the resource's tenant and passes it in.
@id("tenant.boundary")
forbid (principal, action, resource)
when { context.request.resource_tenant != context.envelope.tenant };

@id("approval.stale")
forbid (principal, action, resource)
when {
  context.tool.tier != "none" &&
  context.approval.exists &&
  context.approval.snapshot != context.state_snapshot
};

// ALWAYS: a person, every time. A policy grant never satisfies it.
@id("approval.required.always")
forbid (principal, action, resource)
when {
  context.tool.tier == "always" &&
  !(context.approval.exists && context.approval.granted_by == "human")
};

// PRE_COMMIT with nothing on record: the rule may grant - reversible, within the tenant.
@id("approval.required.pre_commit")
forbid (principal, action, resource)
when {
  context.tool.tier == "pre_commit" &&
  !context.approval.exists &&
  !(context.tool.reversible &&
    context.request.resource_tenant == context.envelope.tenant)
};
