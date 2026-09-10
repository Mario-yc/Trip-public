export const DEFAULT_AGENT_MODEL = "deepseek-v4-flash";

export const AGENT_MODEL_OPTIONS = [
  { id: "deepseek-v4-flash", label: "deepseek-v4-flash" },
  { id: "deepseek-v4-pro", label: "deepseek-v4-pro" }
] as const;

export type AgentModelId = (typeof AGENT_MODEL_OPTIONS)[number]["id"];

export function normalizeAgentModel(value: string | null | undefined): AgentModelId {
  return AGENT_MODEL_OPTIONS.some((option) => option.id === value) ? (value as AgentModelId) : DEFAULT_AGENT_MODEL;
}

export function displayAgentModel(value: string | null | undefined): AgentModelId {
  if (value === "deepseek-chat") {
    return "deepseek-v4-flash";
  }
  if (value === "deepseek-reasoner") {
    return "deepseek-v4-pro";
  }
  return normalizeAgentModel(value);
}
