// The observer may attest only a proposal made by its authenticated human.
export function isPendingHumanProposal(proposal, proposalId, principalId) {
  return typeof principalId === 'string' && principalId.trim().length > 0 &&
    proposal?.proposalId === proposalId &&
    proposal.status?.toLowerCase() === 'awaitingapproval' &&
    proposal.requestedBy === principalId &&
    proposal.requestedByAgent === false && !proposal.resolvedBy;
}
