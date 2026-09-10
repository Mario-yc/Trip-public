import { plannerStore } from "./plannerStore";

export function isCurrentVersionedWrite(baseVersionId: string | null | undefined) {
  return !baseVersionId || plannerStore.getSnapshot().activeVersionId === baseVersionId;
}
