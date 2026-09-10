import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  isCurrentSpatialBoundaryPreviewTurn,
  SpatialBoundaryPreviewCard
} from "../../src/components/agent/SpatialBoundaryPreviewCard";
import { isValidClosedBoundaryPath } from "../../src/components/map/PlannerMap";


describe("spatial boundary preview", () => {
  it("hides a preview after its confirmation-scoped choice is consumed", () => {
    const preview = {
      schemaVersion: "spatial-boundary-preview-v1" as const,
      status: "confirmation_pending" as const,
      canonicalName: "动态测试边界",
      boundaryEvidenceFingerprint: "a".repeat(64),
      sourceUrl: "https://www.openstreetmap.org/copyright",
      sourceEntityId: "relation/fixture",
      retrievedAt: "2026-08-23T00:00:00Z",
      contentHash: "b".repeat(64),
      license: "ODbL-1.0",
      attribution: "© OpenStreetMap contributors",
      originalVertexCount: 5,
      simplifiedVertexCount: 5,
      simplificationMaxDeviationMeters: 0,
      polygonGcj02: [
        [116.3, 39.8],
        [116.5, 39.8],
        [116.5, 40.0],
        [116.3, 39.8]
      ] as Array<[number, number]>
    };
    const baseTurn = {
      role: "assistant" as const,
      status: "active" as const,
      spatialBoundaryPreview: preview,
      choiceOptions: [
        {
          id: "confirm-boundary",
          action: "confirm_spatial_boundary" as const,
          lifecycle: "offered" as const
        }
      ]
    };

    expect(isCurrentSpatialBoundaryPreviewTurn(baseTurn)).toBe(true);
    expect(
      isCurrentSpatialBoundaryPreviewTurn({
        ...baseTurn,
        choiceOptions: [{ ...baseTurn.choiceOptions[0], lifecycle: "consumed" }]
      })
    ).toBe(false);
    expect(
      isCurrentSpatialBoundaryPreviewTurn({
        ...baseTurn,
        choiceOptions: [{ ...baseTurn.choiceOptions[0], lifecycle: "stale" }]
      })
    ).toBe(false);
  });

  it("rejects open, self-intersecting, and oversized preview geometry", () => {
    expect(
      isValidClosedBoundaryPath([
        [0, 0],
        [2, 2],
        [0, 2],
        [2, 0],
        [0, 0]
      ])
    ).toBe(false);
    expect(
      isValidClosedBoundaryPath([
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1]
      ])
    ).toBe(false);
    expect(
      isValidClosedBoundaryPath(
        Array.from({ length: 41 }, (_, index) => [index / 100, index / 100] as [number, number])
      )
    ).toBe(false);
  });

  it("shows source identity, precision, attribution, and a keyboard reachable source link", () => {
    render(
      <SpatialBoundaryPreviewCard
        preview={{
          schemaVersion: "spatial-boundary-preview-v1",
          status: "confirmation_pending",
          canonicalName: "动态测试边界",
          boundaryEvidenceFingerprint: "a".repeat(64),
          sourceUrl: "https://www.openstreetmap.org/copyright",
          sourceEntityId: "relation/fixture",
          sourceVersion: "2",
          retrievedAt: "2026-08-23T00:00:00Z",
          contentHash: "b".repeat(64),
          license: "ODbL-1.0",
          attribution: "© OpenStreetMap contributors",
          originalVertexCount: 392,
          simplifiedVertexCount: 40,
          simplificationMaxDeviationMeters: 16.3,
          polygonGcj02: [
            [116.3, 39.8],
            [116.5, 39.8],
            [116.5, 40.0],
            [116.3, 39.8]
          ]
        }}
      />
    );

    expect(screen.getByRole("region", { name: "活动区域边界预览" })).toBeTruthy();
    expect(screen.getByText("392 → 40 个点，最大偏差约 16.3 米")).toBeTruthy();
    expect(screen.getByText(/OpenStreetMap contributors/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "查看来源" }).getAttribute("href")).toBe(
      "https://www.openstreetmap.org/copyright"
    );
  });
});
