import { PreferenceSummaryCard } from "../services/apiClient";
import { effectivePreferenceMemoryText } from "../components/preferences/PreferenceSummaryCard";
import type { PlannerState } from "./plannerStore";

export type PlannerSnapshot = PlannerState;

export type AgentViewContextInput = {
  activeView: "comparison" | "overview";
};

export function buildAgentContext(
  snapshot: PlannerSnapshot,
  preferenceCard: PreferenceSummaryCard,
  currentUserMessage = "",
  view: AgentViewContextInput = { activeView: "overview" }
) {
  const timelineContext = buildPlanningContext(snapshot);
  const memoryText =
    effectivePreferenceMemoryText(snapshot.preferenceMemory?.memoryText ?? "") ||
    effectivePreferenceMemoryText(preferenceCard.summaryText);
  const effectivePreferenceCard = preferenceContextCard(preferenceCard, memoryText);
  const activeVersionId = String(snapshot.activeVersionId ?? "").trim();
  const adoptedPlan = snapshot.comparisonPreview.plans.find(
    (plan) =>
      plan.proposalId === snapshot.comparisonPreview.adoptedProposalId &&
      plan.workflowMode === "simple_direction_v1" &&
      plan.isAdopted
  );
  const editingPlan = activeVersionId ? adoptedPlan : undefined;
  const focusedPlan = snapshot.comparisonPreview.plans.find(
    (plan) =>
      plan.proposalId === snapshot.comparisonPreview.focusedProposalId &&
      plan.workflowMode === "simple_direction_v1" &&
      plan.materialFingerprint &&
      plan.repairChoiceId
  );
  return {
    currentUserMessage,
    currentPreferenceSummary: memoryText,
    memoryText,
    activeVersionId: snapshot.activeVersionId,
    selectedDayNumber: snapshot.selectedDayNumber,
    selectedSegmentId: snapshot.selectedSegmentId,
    candidatePoiIds: snapshot.candidateMapPois.map((poi) => poi.id),
    preferenceCardId: effectivePreferenceCard ? preferenceCard.id : null,
    candidateMapPois: snapshot.candidateMapPois,
    pendingPoiCandidates: snapshot.pendingPoiCandidates,
    preferenceCard: effectivePreferenceCard,
    preferenceMemory: memoryText && snapshot.preferenceMemory ? { ...snapshot.preferenceMemory, memoryText } : null,
    preferenceSummary: memoryText,
    itineraryPlan: snapshot.itineraryPlan,
    timelineContext,
    viewContext: {
      schemaVersion: "agent-view-context-v1",
      activeView: view.activeView,
      editingProposal: editingPlan
        ? {
            planningSelectionRootTurnId: editingPlan.planningSelectionRootTurnId,
            rootPortfolioId: editingPlan.rootPortfolioId,
            proposalId: editingPlan.proposalId,
            sourceAssistantTurnId: editingPlan.sourceAssistantTurnId,
            activeVersionId
          }
        : null,
      focusedProposal: focusedPlan
        ? {
            planningSelectionRootTurnId: focusedPlan.planningSelectionRootTurnId,
            rootPortfolioId: focusedPlan.rootPortfolioId,
            proposalId: focusedPlan.proposalId,
            sourceAssistantTurnId: focusedPlan.sourceAssistantTurnId,
            materialFingerprint: focusedPlan.materialFingerprint!,
            repairChoiceId: focusedPlan.repairChoiceId!
          }
        : null
    }
  };
}

export function buildPlanningContext(snapshot: PlannerSnapshot) {
  const segments = snapshot.itineraryPlan?.days.flatMap((day) => day.segments) ?? [];
  const selectedSegment = segments.find((segment) => segment.id === snapshot.selectedSegmentId) ?? null;
  const memoryText =
    effectivePreferenceMemoryText(snapshot.preferenceMemory?.memoryText ?? "") ||
    effectivePreferenceMemoryText(snapshot.preferenceCard?.summaryText ?? "");
  const effectivePreferenceCard = preferenceContextCard(snapshot.preferenceCard, memoryText);
  return {
    city: snapshot.selectedCity,
    currentPreferenceSummary: memoryText,
    memoryText,
    itineraryPlanId: snapshot.itineraryPlan?.id ?? null,
    activeVersionId: snapshot.activeVersionId,
    itineraryPlan: snapshot.itineraryPlan,
    itineraryAgentContext: snapshot.itineraryAgentContext,
    preferenceCard: effectivePreferenceCard,
    preferenceMemory: memoryText && snapshot.preferenceMemory ? { ...snapshot.preferenceMemory, memoryText } : null,
    pendingPoiCandidates: snapshot.pendingPoiCandidates,
    candidateMapPois: snapshot.candidateMapPois.map((poi) => ({
      id: poi.id,
      name: poi.name,
      type: poi.type,
      address: poi.address,
      longitude: poi.longitude,
      latitude: poi.latitude
    })),
    selectedDayNumber: snapshot.selectedDayNumber,
    selectedSegment,
    selectedRouteOptionId: snapshot.selectedRouteOptionId
  };
}

function preferenceContextCard(preferenceCard: PreferenceSummaryCard | null | undefined, memoryText: string) {
  if (!preferenceCard || !memoryText) {
    return null;
  }
  return {
    id: preferenceCard.id,
    profileId: preferenceCard.profileId,
    summaryText: memoryText,
    status: preferenceCard.status
  };
}

export function sameMapPoi(
  left: { id: string; amapId?: string; name: string; longitude: number; latitude: number },
  right: { id: string; amapId?: string; name: string; longitude: number; latitude: number }
) {
  return Boolean(
    (left.amapId && right.amapId && left.amapId === right.amapId) ||
      left.id === right.id ||
      (left.name === right.name && left.longitude === right.longitude && left.latitude === right.latitude)
  );
}
