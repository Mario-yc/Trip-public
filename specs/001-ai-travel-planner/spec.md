# Feature Specification: AI Travel Planner Agent Demo

**Feature Branch**: `001-ai-travel-planner`
**Created**: 2026-05-31
**Status**: Draft
**Input**: User description from `AI旅行规划Agent需求说明.md` v0.8

## Clarifications

### Session 2026-05-31

- Q: 第一版页面结构如何组织？ -> A: 集中在一个主页面。
- Q: Demo 邮件提醒是否真实发送？ -> A: 先模拟发送状态。
- Q: 无登录情况下提醒邮箱如何处理？ -> A: 创建提醒时临时填写，只绑定当次灵感或行程。
- Q: Demo 数据保存策略是什么？ -> A: 第一版需要真实持久化；具体数据库技术在 plan.md 定义。
- Q: 外部服务兜底行为是什么？ -> A: 第一版需要默认服务路径和模拟兜底路径；具体 provider 接口在 plan.md 定义。
- Q: 第一版固定方案模板有哪些？ -> A: 低预算、拍照优先、轻松不赶路。

### Session 2026-06-03

- Q: 地图相关 POI、候选点和照片是否允许 mock/fallback？ -> A: 彻底取消地图 mock/fallback；高德或地图服务不可用时直接展示错误原因。
- Q: 初次进入地图、切换城市或无 itinerary 时是否展示热门标点？ -> A: 完全无标点；只有搜索、点击地图地名或附近搜索后才展示真实地图服务返回的候选 POI。
- Q: 地图上方快捷分类保留哪些？ -> A: 只保留景点、美食、交通；体验和购物不再作为快捷按钮，但仍可通过关键词搜索。

### Session 2026-06-04

- Q: 右侧时间栏当前应优先优化哪些内容？ -> A: 优先实现“行程总览”页签，把右侧时间栏升级为按 Day 分组的可折叠、可编辑、可统计 itinerary 工作区；“行程对比”和“费用明细”可保留入口但需要真实切换状态和未接入提示。
- Q: 右侧时间栏哪些字段允许用户直接编辑？ -> A: 当前阶段允许编辑旅行标题、每日标题和每个景点/活动的时间点；景点名称、注意事项、预计耗时、预计花费优先由 Agent 或 provider 生成。
- Q: 右侧时间栏是否需要作为 Agent 上下文？ -> A: 需要预留结构化上下文导出能力，后续用户与 Agent 对话时将当前时间栏安排作为上下文输入。

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Convert Inspiration To Itinerary (Priority: P1)

自由行用户上传小红书/抖音截图、攻略文本、地图截图、社交链接、单张风景照或多张图片集合后，系统识别地点、城市、风格、预算和路线线索，并生成可编辑的结构化行程。

**Why this priority**: 这是产品从内容灵感到可执行行程的核心闭环；没有该能力，地图、票务和方案比较都没有稳定输入。

**Independent Test**: 使用一组包含图片和文本的旅行灵感输入，验证系统能生成包含 POI、每日时间轴、路线、费用和预约提示的行程草案。

**Acceptance Scenarios**:

1. **Given** 用户上传攻略截图和文本，**When** 用户请求生成行程，**Then** 系统展示识别出的地点、城市、风格、预算线索和至少一个可编辑 itinerary。
2. **Given** 用户上传单张风景照片，**When** 系统无法唯一确认地点，**Then** 系统推荐相同景点候选、相似景点或同风格景点，并标注置信度。
3. **Given** 用户上传多张图片，**When** 系统完成识别，**Then** 系统将图片内容聚合为一个旅行灵感集合，而不是生成互相孤立的结果。
4. **Given** 社交链接解析受限，**When** 系统无法读取链接内容，**Then** 系统提示用户粘贴正文或上传截图作为兜底。

---

### User Story 2 - Plan On Map And Timeline (Priority: P2)

用户在同一主页面中通过左侧 Agent 对话、中间 3D 视角地图和右侧每日时间轴编辑行程，系统同步展示 POI、路线距离、交通方式、天气、拥挤风险和票务状态。

**Why this priority**: 用户需要理解空间移动、时间安排和执行风险，不能只依赖文本攻略。

**Independent Test**: 选择北京、上海、广州或深圳中的一个城市，验证用户在主页面调整 POI 或交通方式后，地图和时间轴同步更新。

**Acceptance Scenarios**:

1. **Given** 用户已有行程草案，**When** 用户选择某一天，**Then** 地图展示该日关键 POI、立体路线和 POI 卡片，时间轴展示对应活动。
2. **Given** 用户修改交通方式或出发时间，**When** 系统重新规划，**Then** 系统更新路线耗时、拥挤风险、景点顺序和出发时间建议。
3. **Given** 出行当天存在影响旅行目的的坏天气，**When** 用户打开行程或查看提醒，**Then** 系统展示逐小时天气、每日摘要、风险提示和模拟邮件提醒状态。
4. **Given** 用户进入地图或切换城市且尚未搜索，**When** 地图完成加载，**Then** 地图保持当前城市视角且不展示任何候选标点。
5. **Given** 用户在地图上方输入关键词或点击景点、美食、交通快捷分类，**When** 系统查询地图服务，**Then** 仅展示真实地图服务返回的候选 POI，并展示名称、类型、地址、来源说明和可用照片。
6. **Given** 用户点击地图地名或候选 POI，**When** 地点被选中，**Then** 标点上方展示缩略景点图和附近搜索框；用户可在当前页面放大查看照片、来源网址、介绍并左右滑动其他照片。
7. **Given** 用户拖动地图，**When** 地图位置发生变化，**Then** 当前地点缩略图、照片放大层和标点附近搜索框关闭。
8. **Given** 用户需要调整工作区，**When** 用户收缩左侧 Agent 或右侧时间轴，或拖动地图与时间轴之间的分隔条，**Then** 主页面布局按用户操作调整且不影响当前规划上下文。
9. **Given** 用户已有行程草案，**When** 用户查看右侧行程总览，**Then** 系统按 Day 框体展示每日标题、折叠状态、当日步行距离、预计耗时、预计花费和按时间排序的景点/活动。
10. **Given** 用户点击旅行标题或每日标题，**When** 用户输入新标题并确认，**Then** 系统保存有效标题；如果标题为空或过长，系统显示校验错误并保留原状态。
11. **Given** 用户修改某个景点/活动的时间点，**When** 时间格式或同日顺序不合法，**Then** 系统阻止保存并展示可理解的冲突或格式提示。
12. **Given** 用户添加景点/活动或增加新的 Day，**When** 日程结构变化，**Then** 右侧底部总计区域同步更新总费用、总时长和步行距离。
13. **Given** 用户继续与 Agent 对话，**When** 前端发起需要当前行程上下文的 Agent 请求，**Then** 系统能够提供包含 tripTitle、days、segments、time、poiName、agentNotes、duration、estimatedCost、dayTotals 和 tripTotals 的结构化时间栏上下文。

---

### User Story 3 - Compare Three Plan Templates (Priority: P3)

用户可以比较低预算、拍照优先、轻松不赶路三类方案，查看每个方案的路线、预算、交通、票务、天气、拥挤风险和取舍依据。

**Why this priority**: 用户需要决策依据，而不只是一个 AI 答案。

**Independent Test**: 对同一旅行需求生成三套方案，验证每套方案都有不同目标、费用范围、路线强度和决策说明。

**Acceptance Scenarios**:

1. **Given** 用户填写预算和偏好，**When** 系统生成方案，**Then** 系统输出三套固定模板方案并解释差异。
2. **Given** 某方案超出用户预算，**When** 系统展示该方案，**Then** 系统说明超出金额和原因，而不是强制重排。
3. **Given** 票务或预约信息来自多个来源，**When** 用户查看方案详情，**Then** 来源按官方平台、聚合平台、搜索结果的可信度顺序展示。

---

### User Story 4 - Confirm Reusable Preferences (Priority: P4)

用户通过对话表达长期偏好，系统提取偏好摘要卡片并允许用户确认、修改、新增或删除偏好，包括人数和同行人类型。

**Why this priority**: 个性化规划依赖稳定偏好，但第一版需要避免复杂字段管理。

**Independent Test**: 通过对话输入预算、出行人数、同行人类型和旅行节奏，验证左侧对话区出现可编辑偏好摘要卡片，并在下一次规划中复用。

**Acceptance Scenarios**:

1. **Given** 用户说“两个大人一个老人，预算 3000 左右，不想太赶”，**When** Agent 提取偏好，**Then** 偏好摘要卡片包含人数、同行人类型、预算和节奏。
2. **Given** 用户删除或修改偏好卡片内容，**When** 用户保存偏好，**Then** 后续行程生成使用更新后的偏好。

### Edge Cases

- 上传内容中存在多个城市时，系统需要要求用户确认或通过显式城市入口切换。
- 图片或 OCR 识别低置信度时，系统需要展示候选项并请求确认。
- 外部 provider 不可用时，系统需要使用 mock 或兜底结果并清楚标注不可用原因。
- 地图 provider 不可用时例外：地图 POI、候选标点、照片和路线不得使用 mock/fallback 数据，系统必须直接展示失败原因。
- 地图搜索关键词为空时，系统不得移动地图中心，不得新增候选标点，不得触发无意义 POI 推荐。
- 用户拖动地图时，系统需要关闭当前地点缩略图和照片查看层，避免浮层停留在错误位置。
- 票务结果每次打开行程重新查询；查询失败时保留行程并提示用户稍后重试或前往来源平台。
- 默认不长期保存用户上传原图；只有用户选择“保存灵感原图”才长期保存。
- 天气理想时降低提醒频次；天气影响旅行目的时提高提醒频次。

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST accept screenshots, guide text, map screenshots, social links, single scenery photos, and multi-image sets as inspiration inputs.
- **FR-002**: System MUST extract city, POIs, travel style, budget clues, route clues, and uncertainty indicators from supported inputs.
- **FR-003**: System MUST aggregate multi-image inputs into one inspiration set.
- **FR-004**: System MUST provide text or screenshot fallback when social links cannot be parsed.
- **FR-005**: System MUST generate editable itineraries with days, POIs, activity durations, transport segments, cost items, and reservation/ticket notes.
- **FR-005A**: For every resolved non-rest travel day, the server MUST preserve all user-required goal occurrences and compile a non-shrinkable daily-completion target. When a standard/intensive day is otherwise sparse and bounded POI/route capacity permits, the server MUST add at most one route-local activity slot before final candidate and route assignment. That slot MUST use a real map-provider POI and final adjacent-route evidence; if no candidate passes, the plan MUST remain explicitly partial and MUST NOT lower the target or claim completion.
- **FR-006**: System MUST present the first release in a single main page with left Agent conversation, center 3D-view map, and right timeline.
- **FR-007**: System MUST provide city selection and city auto-detection.
- **FR-008**: System MUST support Beijing, Shanghai, Guangzhou, and Shenzhen with equivalent POI, ticket, weather, and traffic-crowding coverage.
- **FR-009**: System MUST show POI cards, route distance, and timeline linkage for itinerary segments.
- **FR-009A**: Map POI candidates, labels, photos, and map search results MUST use real map provider data only; the map experience MUST NOT display mock POIs, mock labels, or mock photos.
- **FR-009B**: When users enter the map, switch city, or have no itinerary and no search query, the map MUST show no candidate markers.
- **FR-009C**: Map quick filters MUST only include scenic spots, food, and transport. Experience and shopping MUST NOT appear as quick filter buttons, though keyword search may still match those places.
- **FR-009D**: Empty map searches MUST keep the current map view unchanged and MUST NOT create candidate markers.
- **FR-009E**: Selecting a map label or candidate POI MUST show a marker-attached thumbnail, an inline nearby-search box centered on that selected point, and an in-page photo viewer with source URL, introduction, and horizontal photo browsing.
- **FR-009F**: Dragging or moving the map MUST close marker-attached thumbnails, nearby-search boxes, and photo viewers.
- **FR-009G**: The main workspace MUST support collapsing/releasing the left Agent panel, resizing the map/timeline split, and collapsing/releasing the right timeline panel.
- **FR-009H**: The Agent conversation, map, and timeline MUST share planning context including selected city, selected map POI, active itinerary day, active segment, candidate POIs, and current plan state.
- **FR-009I**: The right timeline MUST provide real tab switching for itinerary overview, plan comparison, and cost details; the first implementation MUST complete itinerary overview and show clear empty or pending states for unimplemented tabs.
- **FR-009J**: The itinerary overview MUST show an editable trip title without a separate "editable" label and MUST remove template subtitle text under the title.
- **FR-009K**: Trip title and day title editing MUST support confirm, cancel, non-empty validation, and maximum-length validation.
- **FR-009L**: Route optimization and auto-scheduling controls MAY remain visible as future Agent entry points, but they MUST NOT imply completed behavior before the Agent integration exists.
- **FR-009M**: The itinerary overview MUST NOT show global walking distance, estimated duration, estimated cost, or transport-mode selector above Day groups.
- **FR-009N**: The itinerary overview MUST render each day as a collapsible Day card with Day number, editable day title, daily walking distance, daily estimated duration, and daily estimated cost in the card header.
- **FR-009O**: Each itinerary segment inside a Day card MUST show editable time, POI/activity name, Agent-generated note or risk placeholder, estimated duration, and estimated cost.
- **FR-009P**: Segment time editing MUST validate time format and same-day ordering; invalid edits MUST display a user-visible error and MUST NOT silently save.
- **FR-009Q**: Users MUST be able to add an activity skeleton to an existing Day and add a new Day card from the right timeline.
- **FR-009R**: The right timeline MUST show trip totals at the bottom and dynamically calculate total estimated cost, total duration, and total walking distance from all current Day cards.
- **FR-009S**: The right timeline state MUST be serializable as Agent context with tripTitle, days, segments, time, poiName, agentNotes, duration, estimatedCost, dayTotals, and tripTotals.
- **FR-010**: System MUST generate three comparable templates: low budget, photo-first, and relaxed pace.
- **FR-011**: System MUST treat budget as a soft constraint and explain deltas when plans are above the requested budget.
- **FR-012**: System MUST perform real online ticket lookup for high-speed/train, flight, and attraction ticket or reservation status where available.
- **FR-013**: System MUST prefer formal ticket APIs and fall back to third-party search or aggregation when formal APIs are unavailable.
- **FR-014**: System MUST re-query ticket status whenever a saved itinerary is opened.
- **FR-015**: System MUST display "查询结果仅供参考，请以购票平台为准", query time, and source for ticket results.
- **FR-016**: System MUST sort ticket sources by credibility: official, aggregation, then search.
- **FR-017**: System MUST show public search result source links and collapse multiple links behind an expandable control.
- **FR-018**: System MUST include weather in itinerary decisions with hourly forecast, daily summary, and risk hints.
- **FR-019**: System MUST classify bad weather based on whether it affects the user's stated travel purpose.
- **FR-020**: System MUST simulate email reminder creation and send status for same-day weather risk.
- **FR-021**: System MUST collect reminder email temporarily when a reminder is created and bind it only to the current inspiration or itinerary.
- **FR-022**: System MUST include traffic crowding in route, attraction order, and departure-time recommendations.
- **FR-023**: System MUST use both real traffic data and estimated crowding risk when real data is incomplete.
- **FR-024**: System MUST extract reusable preferences into a preference summary card in the left Agent conversation area.
- **FR-025**: System MUST allow users to add, remove, and edit preference summary card content.
- **FR-026**: System MUST save party size and traveler types as long-term editable preferences.
- **FR-027**: System MUST store data under a single default local user for the demo.
- **FR-028**: System MUST store structured extraction results and thumbnails by default.
- **FR-029**: System MUST only long-term store original images when users explicitly choose "保存灵感原图".
- **FR-030**: System MUST support manual cleanup of short-term original-image cache.
- **FR-031**: System MUST support both default and simulated fallback behavior for LLM, ticket, weather, traffic, search, vision, and email-dependent capabilities.
- **FR-031A**: Map-dependent capabilities are excluded from simulated fallback behavior; map provider failures MUST be shown as explicit user-visible errors.
- **FR-032**: System MUST not include checkout, payment, ticket issuance, login, cross-device sync, mobile browser optimization, production collaboration, itinerary export, or real email sending in the first release.

### Key Entities

- **DefaultUser**: Single local user for demo persistence.
- **InspirationSet**: Uploaded or pasted source material grouped for one planning request.
- **SourceMaterial**: A screenshot, text, map image, social link, scenery photo, or image in an inspiration set.
- **ExtractionResult**: Structured places, city, budget clues, style, route clues, confidence, thumbnail, and optional original-image reference.
- **PreferenceProfile**: Reusable user preferences, including party size, traveler types, budget range, pace, transport style, photo preference, and accessibility notes.
- **PreferenceSummaryCard**: User-confirmable view of extracted preferences.
- **ItineraryPlan**: A generated plan with template type, daily schedule, cost estimate, risks, and explanation.
- **ItineraryDay**: A day within a plan with ordered segments and map references.
- **ItinerarySegment**: Activity or transport leg with POIs, time, mode, cost, ticket, weather, and crowding signals.
- **POI**: Attraction, station, airport, restaurant, district, or candidate place.
- **RouteOption**: Route candidate with mode, distance, duration, crowding risk, and constraints.
- **TicketLookupResult**: Ticket or reservation status with source, query time, caveat, and credibility rank.
- **WeatherSignal**: Hourly forecast, daily summary, and purpose-impact risk.
- **TrafficCrowdingSignal**: Real or estimated crowding and congestion hint.
- **ReminderDraft**: Same-day weather-risk email content and simulated send status.
- **ProviderResult**: Response from default or mock provider with source, timestamp, confidence, and failure state.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a demo session, a user can convert one supported inspiration input into a structured itinerary in under 5 minutes.
- **SC-002**: For Beijing, Shanghai, Guangzhou, and Shenzhen, generated plans include at least 90% of required itinerary elements: POIs, daily timeline, route distance, transport mode, estimated costs, ticket notes, weather, and crowding hints.
- **SC-003**: At least 3 comparable plans are generated for the same request, each with a distinct template goal and decision rationale.
- **SC-004**: 100% of ticket lookup displays include source, query time, credibility order, and the required caveat.
- **SC-005**: 100% of uploaded image flows store structured results and thumbnails by default, with original-image long-term storage only after explicit user choice.
- **SC-006**: Preference extraction creates a user-confirmable summary card for at least party size, traveler types, budget, and pace when those are mentioned.
- **SC-007**: Provider failure in ticket, weather, search, map, vision, or email flows does not block itinerary viewing; the user receives a visible fallback or failure explanation.

## Assumptions

- The first release is a personal AI Demo, not a production MVP.
- The first release runs for a single local user and does not require registration.
- First release display target is desktop Web; mobile browser optimization is out of scope.
- External providers may have incomplete access, so simulated fallback paths are part of expected demo behavior except for map-dependent POI, marker, photo, and route display.
- The default map provider will be chosen for domestic POI, 3D-view map, route, traffic, weather, cost, and free/low-cost developer access.

## Map Development Prompt

Use this prompt for the next map-workbench implementation slice:

```text
Continue development for the AI Travel Planner Web Demo. Focus only on the map workbench and its connection to the left Agent panel and right timeline. Do not expand into unrelated user stories.

Hard requirements:
- Do not use mock data for map-related POIs, markers, labels, photos, nearby search, or route display. Use real map provider data only. If the provider key is missing or the provider request fails, show the exact user-visible failure reason.
- On initial map load, city switch, and no-itinerary/no-search state, show the map only. Do not show any candidate markers or default popular POIs.
- Optimize map entry and map movement performance. Reuse the loaded map instance, avoid reloading the JS SDK, avoid empty-search provider calls, debounce map-move dependent work, and only query POIs when the user explicitly searches, clicks a map label, or runs nearby search.
- Keep the top map quick filters to scenic spots, food, and transport only. Remove experience and shopping as quick filter buttons. Keyword search may still search arbitrary terms.
- Empty keyword search must not move the map, must not create markers, and must not trigger candidate POI recommendations.
- When the user searches a keyword or clicks a quick filter, query the current city through the real map provider and show candidate POIs only after results return. Each result must include name, type, address/district, source note, and available photos.
- When the user clicks a map label or candidate POI, center/highlight that point and show a marker-attached thumbnail above the marker. Also show a small nearby-search input above the thumbnail, centered on the selected coordinate.
- Clicking the thumbnail opens an in-page photo viewer, not a page navigation. The viewer must show source URL, introduction/description if available, and horizontal left/right browsing for other photos of the same place.
- Remove the current bottom/right photo drawer behavior. Photos should be anchored to the selected marker and in-page viewer only.
- When the user drags or moves the map, close the marker thumbnail, nearby-search input, and photo viewer.
- Add layout controls: left Agent panel can collapse to the left and release; right timeline can collapse to the right and release; the map/timeline split can be manually resized by dragging.
- Maintain shared planning context across Agent, map, and timeline: selectedCity, selectedMapPoi, candidatePois, activeDayId/dayNumber, activeSegmentId, selectedRouteOptionId, and current plan. Agent messages must be able to include this context so phrases such as “这个地方”, “这一段”, and “放到下午” can be resolved.

Testing expectations:
- Backend tests cover map provider key missing, map provider failure, successful POI search with photos, and no mock map data.
- Frontend tests cover initial map with no markers, category search, keyword search, empty search no-op, marker thumbnail opening, photo viewer carousel, map drag closing overlays, zoom/2D/3D/reset controls, panel collapse/release, split resizing, and context passed from map/timeline into Agent requests.
- Run backend tests with .\trip\Scripts\python.exe -m pytest backend/tests.
- Run npm test and npm run build.
```

## Right Timeline Development Prompt

Use this prompt for the next right-timeline implementation slice:

```text
Continue development for the AI Travel Planner Web Demo. Focus only on the right timeline itinerary overview. Do not expand into unrelated user stories, map provider work, ticket checkout, collaboration, or full Agent conversation redesign.

Goal:
- Upgrade the right timeline from a simple segment list into an editable, collapsible, calculated itinerary workspace.
- Keep the current single-page desktop Web Demo.
- Reuse AppShell, plannerStore, apiClient, DailyTimeline, RiskSignals, PlanComparison, and existing itinerary types where possible.

Hard requirements:
- The top right tabs must actually switch between itinerary overview, plan comparison, and cost details.
- Implement itinerary overview first. Plan comparison and cost details may show clear empty/pending states if they are not connected in this slice.
- The itinerary overview title must show the trip title, for example "北京 2 日行程". Remove the separate "editable" label and remove subtitle/template text such as "轻松不赶路 / 历史路线 / 老北京风情".
- Clicking the trip title starts inline editing. Editing must support confirm, cancel, non-empty validation, and max-length validation.
- Keep "优化路线" and "自动排期" as future Agent entry points only. They may be disabled or show a "待接入 Agent" hint; do not implement real optimization or scheduling in this slice.
- Keep a place for weather warning and POI risk hints. Future behavior: after the Agent identifies concrete travel dates, the system searches weather and public scenic-area information to generate risk warnings. For now, show existing provider data or a "待 Agent 查询" placeholder.
- Remove the global walking distance, estimated duration, estimated cost, and transport-mode selector from above Day groups.
- Render each day as a Day card. The card header must include Day number, editable day title, collapse/expand control, daily walking distance, daily estimated duration, and daily estimated cost.
- Day cards must collapse and expand. Collapsed cards still show Day number, day title, and daily totals.
- Inside each Day card, render a timeline of POI/activity cards sorted by time.
- Each POI/activity card must include editable time, POI/activity name, Agent-generated note or risk placeholder, estimated duration, and estimated cost.
- Time editing must validate HH:mm format, same-day ordering, and obvious conflicts. Invalid edits must show a clear user-facing error and must not silently save.
- Each Day card footer must include "添加景点/活动" to append an activity skeleton to that day.
- The bottom of the right timeline must include "增加日程安排" to add the next Day card.
- The bottom of the right timeline must show trip totals: total estimated cost, total duration, and total walking distance.
- Trip totals must be recalculated from all current Day cards whenever the user edits time, adds/removes activities, or adds/removes days.
- In this slice, directly editable fields are limited to trip title, day title, and segment time. POI name, Agent note, estimated duration, and estimated cost remain Agent/provider-generated fields.
- Design a serializable Agent context shape for the right timeline:
  - tripTitle
  - days
  - segments
  - time
  - poiName
  - agentNotes
  - duration
  - estimatedCost
  - dayTotals
  - tripTotals
- Do not break existing inspiration upload, map/timeline selection, plan comparison, ticket source display, weather/crowding hints, or provider fallback behavior.

Testing expectations:
- Add or update frontend tests for tab switching, trip title edit validation, day title editing, Day collapse/expand, time edit validation, add activity, add Day, trip total recalculation, and Agent context serialization.
- Existing tests for inspiration flow, map timeline flow, plan comparison, preference card, loading/error states must still pass.
- Run npm test.
- Run npm run build.
```
