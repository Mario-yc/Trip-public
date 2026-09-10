import type { ConversationTurn, SpatialBoundaryPreview } from "../../services/apiClient";

type SpatialBoundaryPreviewCardProps = {
  preview: SpatialBoundaryPreview;
};

const CLOSED_SPATIAL_CHOICE_LIFECYCLES = new Set([
  "consumed",
  "stale",
  "expired",
  "cancelled",
  "failed_terminal"
]);

export function isCurrentSpatialBoundaryPreviewTurn(
  turn: Pick<ConversationTurn, "role" | "status" | "choiceOptions" | "spatialBoundaryPreview">
) {
  if (
    turn.role !== "assistant" ||
    turn.status !== "active" ||
    turn.spatialBoundaryPreview?.status !== "confirmation_pending"
  ) {
    return false;
  }
  return !(turn.choiceOptions ?? []).some(
    (option) =>
      (option.action === "confirm_spatial_boundary" || option.action === "change_spatial_boundary") &&
      CLOSED_SPATIAL_CHOICE_LIFECYCLES.has(String(option.lifecycle ?? ""))
  );
}

export function SpatialBoundaryPreviewCard({ preview }: SpatialBoundaryPreviewCardProps) {
  return (
    <section aria-label="活动区域边界预览" className="spatial-boundary-preview-card">
      <header>
        <strong>{preview.canonicalName || "活动区域边界"}</strong>
        <span className="spatial-boundary-preview-status" role="status">
          等待你确认
        </span>
      </header>
      <p>该边界来自可更新的开放地理数据，需要你确认是否符合本次理解。</p>
      <dl>
        <div>
          <dt>来源身份</dt>
          <dd>{preview.sourceEntityId}</dd>
        </div>
        <div>
          <dt>几何简化</dt>
          <dd>
            {preview.originalVertexCount} → {preview.simplifiedVertexCount} 个点，最大偏差约{" "}
            {preview.simplificationMaxDeviationMeters.toFixed(1)} 米
          </dd>
        </div>
        <div>
          <dt>内容校验</dt>
          <dd>{preview.contentHash.slice(0, 12)}</dd>
        </div>
      </dl>
      <p className="spatial-boundary-attribution">
        {preview.attribution} · {preview.license}
        {preview.sourceUrl ? (
          <>
            {" · "}
            <a href={preview.sourceUrl} rel="noreferrer" target="_blank">
              查看来源
            </a>
          </>
        ) : null}
      </p>
    </section>
  );
}
