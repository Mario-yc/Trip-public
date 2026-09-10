import { describe, expect, it } from "vitest";

import { mapPoiFromSegment } from "../../src/components/map/PlannerMap";
import type { PlannerSegment } from "../../src/services/apiClient";

describe("timeline POI map projection", () => {
  it("preserves structured provider evidence", () => {
    const sourceClaims = [
      {
        claimKey: "local_food",
        stance: "support",
        locality: "北京"
      }
    ];
    const segment: PlannerSegment = {
      id: "seg_meal",
      startTime: "12:00",
      endTime: "13:15",
      kind: "meal",
      poi: {
        id: "poi_meal",
        amapId: "B000MEAL01",
        name: "北京本地餐馆",
        city: "北京",
        category: "food",
        latitude: 39.91,
        longitude: 116.41,
        source: "amap-place-search",
        confidence: 0.95,
        type: "餐饮服务;中餐厅",
        providerTypeCode: "050100",
        tags: ["地方风味", "北京菜"],
        sourceClaims
      },
      transportMode: "walking",
      estimatedCost: 120,
      notes: ""
    };

    const projected = mapPoiFromSegment(
      segment as PlannerSegment & {
        poi: PlannerSegment["poi"] & { longitude: number; latitude: number };
      }
    );

    expect(projected.providerTypeCode).toBe("050100");
    expect(projected.tags).toEqual(["地方风味", "北京菜"]);
    expect(projected.sourceClaims).toEqual(sourceClaims);
  });
});
