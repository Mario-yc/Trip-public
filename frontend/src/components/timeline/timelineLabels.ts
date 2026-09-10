export function groundingStatusLabelForValue(status: string) {
  if (status === "draft_only") {
    return "高德 POI 待校验";
  }
  if (status === "routeable_anchor") {
    return "已匹配地图锚点，待核对";
  }
  if (status === "verified_amap") {
    return "已确认高德地点";
  }
  if (status === "provisional") {
    return "地点已匹配，夜间适配待核验";
  }
  if (status === "unresolved") {
    return "地点待补全";
  }
  if (status === "agent_selected_candidate") {
    return "Agent 已选高德候选";
  }
  if (status === "user_confirmed") {
    return "用户已确认高德地点";
  }
  if (status === "area_unresolved") {
    return "区域意图待细化";
  }
  if (status === "provider_rate_limited") {
    return "高德限流，稍后重试";
  }
  if (status === "optional_waiting") {
    return "可选择顺路餐厅";
  }
  if (status === "waiting_for_poi_grounding") {
    return "地点待补全";
  }
  if (status === "area_poi" || status === "composite_poi") {
    return "建议细化具体目标";
  }
  if (status === "functional_poi") {
    return "等待 Agent 按附近搜索补全";
  }
  return "";
}

export function groundingStatusLabelForSegmentFields(
  status: string,
  kind?: string | null,
  intentType?: string | null,
  poiName?: string | null,
  notes?: string | null
) {
  if (status !== "waiting_for_poi_grounding" && status !== "provider_rate_limited" && status !== "area_unresolved") {
    return groundingStatusLabelForValue(status);
  }
  const intentText = `${kind ?? ""} ${intentType ?? ""} ${poiName ?? ""} ${notes ?? ""}`;
  if (status === "area_unresolved" || /area_walk|区域|街区|商圈|漫步/.test(intentText)) {
    return "区域地点待细化";
  }
  if (/meal|午餐|晚餐|早餐|餐饮|美食|餐厅/.test(intentText)) {
    return "餐饮地点待补全";
  }
  if (/campus|高校|大学|学院|校园|校区/.test(intentText)) {
    return "高校地点待补全";
  }
  if (/night|夜景|夜游|观景|灯光/.test(intentText)) {
    return "夜景地点待补全";
  }
  return groundingStatusLabelForValue(status);
}
