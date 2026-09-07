/**
 * NetGravity — Insight & Reasoning Mapper
 * =======================================
 * Formats Orchestrator reasoning outputs, action proposals, and evidence packages.
 */

export function mapInsightResponse(raw) {
  if (!raw) return null;
  const reasoning = raw.reasoning || {};
  return {
    stateId: raw.state_id,
    snapshotId: raw.snapshot_id,
    scope: raw.scope,
    entityId: raw.entity_id,
    narrative: reasoning.explanation || reasoning.narrative || '',
    recommendation: reasoning.recommendation || '',
    rejectedAlternatives: reasoning.rejected_alternatives || [],
    confidence: reasoning.confidence || 'HIGH',
    evidenceGrounded: reasoning.is_grounded !== false,
  };
}
