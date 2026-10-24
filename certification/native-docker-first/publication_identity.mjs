// The observer may attest only a proposal made by its authenticated human.
export function isPendingHumanProposal(proposal, proposalId, principalId) {
  return typeof principalId === 'string' && principalId.trim().length > 0 &&
    proposal?.proposalId === proposalId &&
    proposal.status?.toLowerCase() === 'awaitingapproval' &&
    proposal.requestedBy === principalId &&
    proposal.requestedByAgent === false && !proposal.resolvedBy;
}

// An agent's proposal also needs an approver distinct from the human directing it.
export function isDistinctHumanApproval(proposal, agentId, originatingPrincipalId) {
  const present = value => typeof value === 'string' && value.trim().length > 0;
  return present(agentId) && present(originatingPrincipalId) &&
    agentId !== originatingPrincipalId && proposal?.requestedBy === agentId &&
    present(proposal.resolvedBy) && proposal.resolvedBy !== agentId &&
    proposal.resolvedBy !== originatingPrincipalId;
}
