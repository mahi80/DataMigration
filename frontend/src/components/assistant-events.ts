export type AssistantEvent = "navigation" | "discovery" | "plan_created" | "approval_requested" | "retry_requested" | "request_failed" | "validation_opened" | "mapping_edited";

export interface AssistantInteraction {
  event: AssistantEvent;
  outcome?: "success" | "failure" | "pending";
}

export const ASSISTANT_EVENT = "datamigration:assistant-interaction";

// Deliberately accepts event names only: no form values, query text, credentials,
// raw rows, error bodies, or connection strings enter the context buffer.
export function recordInteraction(event: AssistantEvent, outcome?: AssistantInteraction["outcome"]) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent<AssistantInteraction>(ASSISTANT_EVENT, { detail: { event, ...(outcome ? { outcome } : {}) } }));
  }
}
