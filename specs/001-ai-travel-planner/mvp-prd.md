# AI Travel Planner MVP PRD

**Version**: v1
**Date**: 2026-06-10
**Status**: Draft for MVP alignment
**Scope**: Single-user desktop Web demo

## 1. Executive Summary

### Problem Statement

Free-travel users often start from vague inspiration, screenshots, social notes, and personal constraints, but struggle to turn them into a concrete, editable, map-aware itinerary.

The current product needs an MVP that proves one complete AI planning loop: user talks to an Agent, the Agent creates or revises a structured itinerary, and the user can manually correct the itinerary while all state is persisted.

### Proposed Solution

Build a single-page AI travel planning workspace where DeepSeek-powered Agent conversation generates and modifies the right-side itinerary timeline. The itinerary uses real AMap POIs in MVP, supports manual timeline edits, persists every accepted itinerary state to SQLite, and supports reverting to a previous conversation state when the user edits an earlier message.

### Success Criteria

- A user can complete a demo flow from conversation input to saved itinerary in under 5 minutes.
- 100% of itinerary POIs shown in the MVP are backed by real AMap POI data or explicitly marked as unresolved and not added to the final itinerary.
- Agent-generated itinerary updates are visible in the right timeline within one request-response cycle and are saved to the database.
- Manual edits to trip title, day title, segment time, added activities, and added days persist after reload.
- The user can edit a previous conversation turn and restore the itinerary to the state associated with that turn before generating a revised branch.

## 2. MVP Positioning

### MVP Goal

Prove that an AI Agent can collaboratively build and revise a real, editable travel itinerary with the user.

### Primary Demo Loop

1. User describes a trip in the left Agent conversation.
2. Agent extracts preferences, city, dates or duration, budget, pace, and requested places.
3. Agent searches or resolves POIs through AMap.
4. Agent generates a structured itinerary in the right timeline.
5. User manually edits the timeline or asks Agent to modify it.
6. System persists the itinerary state.
7. User can edit a prior conversation message; the itinerary returns to that message's saved state and can be regenerated from there.

### MVP Priority

The MVP optimizes for:

- Complete demonstrable planning loop.
- Agent intelligence and explainable itinerary modification.
- Reliable state persistence.
- Real POI grounding through AMap.

It does not optimize for production scalability, mobile experience, account systems, payment, or full provider coverage.

## 3. User Personas

### Primary User: Domestic Free-Travel Planner

A user planning a domestic city trip who has rough ideas, screenshots, or preferences and wants an executable itinerary rather than a text-only guide.

### Secondary User: Demo Evaluator

A person evaluating whether the Agent can understand travel intent, resolve real POIs, and revise itinerary structure through conversation.

## 4. User Stories & Acceptance Criteria

### Story 1: Generate Itinerary From Agent Conversation

As a free-travel user, I want to describe my travel idea in conversation so that the Agent can generate a structured itinerary in the right timeline.

Acceptance Criteria:

- User can enter natural language trip intent in the left Agent panel.
- Agent identifies at minimum city, trip duration or requested days, travel style, budget clues, and candidate POIs when present.
- Agent resolves itinerary POIs through AMap before adding them to the right timeline.
- If a POI cannot be resolved confidently, the Agent asks for confirmation or presents candidates instead of silently adding a fake location.
- Right timeline displays generated trip title, Day groups, ordered segments, time, POI name, Agent notes, estimated duration, estimated cost, and day/trip totals.
- Generated itinerary is saved to SQLite and can be reloaded.

### Story 2: Modify Itinerary Through Conversation

As a user, I want to ask the Agent to change the itinerary so that I can adjust the plan without manually editing every field.

Acceptance Criteria:

- User can issue commands such as "把故宫放到下午", "第二天轻松一点", "删掉这个景点", or "加一个附近的餐厅".
- Agent receives current itinerary context, selected map POI, selected day, selected segment, preference card, and candidate POIs.
- Agent returns a structured itinerary patch, not only prose.
- System validates the patch before applying it.
- Applied patch updates the right timeline and persists to SQLite.
- If the Agent patch conflicts with time ordering, missing POI resolution, or unsupported operation, the UI shows a clear error and does not corrupt the saved itinerary.

### Story 3: Manually Edit Right Timeline

As a user, I want to directly edit the right timeline so that I can make precise corrections when the Agent result is not exactly right.

Acceptance Criteria:

- User can edit trip title.
- User can edit Day title.
- User can edit segment start time.
- User can add an activity skeleton to an existing Day.
- User can add a new Day.
- Time edits validate HH:mm format and same-day ordering.
- Day and trip totals recalculate after edits.
- All manual edits persist to SQLite.
- Manual edits become part of the next Agent request context.

### Story 4: Edit Previous Conversation And Restore Itinerary State

As a user, I want to revise an earlier Agent conversation message so that I can branch the planning process when the result goes in the wrong direction.

Acceptance Criteria:

- Each user message and Agent response is stored as a conversation turn.
- Each conversation turn stores or references an itinerary snapshot after that turn.
- User can edit a previous user message.
- When editing a previous user message, the current itinerary returns to the snapshot associated with that previous turn.
- Later conversation turns after the edited message are marked stale, hidden, or superseded.
- User can regenerate a new Agent response from the edited message and restored itinerary context.
- The new generated itinerary is saved as a new state version.

### Story 5: Use Real AMap POIs In MVP

As a user, I want itinerary places to correspond to real map locations so that the plan can be trusted spatially.

Acceptance Criteria:

- MVP itinerary POIs are resolved through AMap WebService or selected from AMap map/search results.
- The map search experience never displays mock POIs, mock photos, or mock markers.
- If AMap key is missing or request fails, the system shows the provider error and blocks POI insertion for that operation.
- Agent can ask the user to choose among AMap candidates when multiple places match.
- Stored POI includes at least AMap id, name, type, city, district, address, longitude, latitude, source, source URL or provider note, confidence, and photos when available.

### Story 6: Keep Plan Comparison As Auxiliary

As a user, I want to see optional comparison ideas so that I can understand tradeoffs without making comparison the main flow.

Acceptance Criteria:

- The right-panel "行程对比" tab remains available.
- It can display low-budget, photo-first, and relaxed-pace comparison plans when available.
- It is not required to drive the main itinerary state in MVP.
- If comparison is unavailable, the tab shows a clear pending or unavailable state.

## 5. AI System Requirements

### Agent Provider

- MVP uses DeepSeek as the real Agent provider.
- Mock Agent may remain for tests, local fallback, and deterministic development.
- User-facing MVP demo should use DeepSeek when configured.

### Agent Inputs

Agent requests must include:

- Latest user message.
- Conversation history up to the current branch.
- Current itinerary snapshot.
- Current right timeline context.
- Preference card.
- Selected city.
- Selected map POI.
- Candidate AMap POIs.
- Active Day and active segment.
- Existing unresolved questions or confirmation needs.

### Agent Outputs

Agent responses should include:

- User-facing explanation.
- Structured itinerary operation or full itinerary replacement.
- POI resolution requests when needed.
- Confidence or uncertainty notes.
- Warnings when data is estimated.

Supported MVP operations:

- Create itinerary.
- Replace trip title.
- Replace Day title.
- Replace segment start time.
- Add Day.
- Add segment/activity.
- Remove segment/activity.
- Move segment to another time or Day.
- Replace segment POI with an AMap-resolved POI.
- Update Agent notes.

### Evaluation Strategy

MVP Agent quality should be evaluated with scenario tests:

- Generate a 2-day Beijing itinerary from a natural language request.
- Move one attraction to afternoon by conversation.
- Add a nearby restaurant through Agent using AMap candidates.
- Edit a previous user message and verify itinerary state rollback.
- Reject or clarify ambiguous POI requests.

Pass criteria:

- At least 80% of scripted scenarios complete without manual database correction.
- 100% of accepted POI insertions have AMap-backed coordinates.
- 100% of invalid patches are rejected with visible user feedback.

## 6. Technical Specifications

### Architecture Overview

```text
Frontend AppShell
├── Agent conversation panel
│   ├── sends user messages
│   ├── supports editing previous messages
│   └── displays Agent response and stale branch state
├── AMap planning surface
│   ├── searches real POIs
│   ├── displays selected POI and photos
│   └── feeds selected POI into Agent context
└── Right timeline workspace
    ├── renders persisted itinerary state
    ├── supports manual edits
    ├── emits structured context
    └── persists edits through backend API

FastAPI Backend
├── Conversation API
├── Agent orchestration service
├── AMap POI resolution service
├── Itinerary versioning service
├── Itinerary edit/patch API
└── SQLite persistence
```

### Required Backend Capabilities

- Store conversation sessions and turns.
- Store itinerary snapshots or versions per turn.
- Store current active itinerary version.
- Apply validated structured itinerary patches.
- Roll back or restore itinerary state when editing a previous user turn.
- Resolve requested POIs through AMap before adding them.
- Persist manual timeline edits.

### Required Frontend Capabilities

- Conversation input and message history.
- Edit previous user message.
- Show stale/superseded turns after a branch edit.
- Render persisted itinerary state in right timeline.
- Trigger manual timeline edit API calls.
- Send full planning context to Agent.
- Surface Agent patch validation errors.

### Data Model Additions

MVP likely needs these entities or tables:

- `conversation_sessions`
- `conversation_turns`
- `itinerary_versions`
- `itinerary_patches`
- `amap_poi_candidates`

Existing itinerary tables can remain, but they need versioning or snapshot support.

### API Additions

Candidate MVP endpoints:

- `POST /api/agent/sessions`
- `POST /api/agent/sessions/{session_id}/messages`
- `PATCH /api/agent/sessions/{session_id}/messages/{message_id}`
- `GET /api/agent/sessions/{session_id}`
- `POST /api/itineraries/{plan_id}/patch`
- `POST /api/itineraries/{plan_id}/restore-version`
- `POST /api/map/pois/resolve`

## 7. Persistence Requirements

- Every Agent-applied itinerary state must be saved.
- Every manual edit must be saved.
- Every conversation turn must be saved.
- Each saved itinerary state must be restorable.
- Editing an earlier message must not delete historical states immediately; it should mark later turns as superseded or create a new branch.

## 8. Non-Goals

MVP explicitly excludes:

- Login and account system.
- Cross-device sync.
- Checkout, payment, ticket issuance.
- Real email sending.
- Production multiplayer collaboration.
- Mobile browser optimization.
- Hotel booking or restaurant booking closure.
- Export to PDF, image, calendar, or Markdown.
- Fully real ticket, weather, and traffic provider coverage.
- Hard budget optimization.
- Production-grade long-term memory beyond current planning session.

## 9. Risks

### Agent Patch Reliability

Risk: DeepSeek may return prose or malformed operations.
Mitigation: Require structured output schema, validate before applying, and show clear failure states.

### POI Ambiguity

Risk: AMap may return multiple similarly named places.
Mitigation: Ask user to choose from candidates before inserting uncertain POIs.

### Versioning Complexity

Risk: Editing previous conversation turns requires state rollback and branch management.
Mitigation: MVP can implement single-branch supersession first: edit previous message, restore its snapshot, mark later turns superseded, then regenerate.

### Data Consistency

Risk: Manual timeline edits and Agent patches may conflict.
Mitigation: Use one backend patch validator for both manual edits and Agent edits.

## 10. Roadmap

### MVP

- DeepSeek Agent creates itinerary from conversation.
- AMap-backed POI insertion.
- Right timeline manual edits persisted to SQLite.
- Agent modifies right timeline via structured patches.
- Conversation edit restores previous itinerary state.
- Comparison tab remains auxiliary.

### v1.1

- Better POI disambiguation UI.
- More robust itinerary branch history.
- Agent explanation of route, cost, and tradeoffs.
- Ticket/weather/traffic providers upgraded where feasible.

### v2.0

- Account system and cross-device sync.
- Share and collaboration.
- Real booking integrations.
- Mobile experience.
- Export and calendar integration.

## 11. MVP Product Decisions

### Conversation Branching

MVP will use single-branch supersession rather than visible multi-branch history.

When the user edits a previous message, the system restores the itinerary snapshot associated with that message, marks later conversation turns as `superseded`, and regenerates from the edited message and restored itinerary context.

Rationale: this is enough to demonstrate editable Agent planning and rollback while avoiding the product and engineering complexity of a multi-branch conversation UI.

### Itinerary State Storage

MVP will store both full itinerary snapshots and itinerary patches.

- Full snapshots are used for reliable restore, reload, and rollback.
- Patches are used for auditability, debugging, and understanding what the Agent or user changed.

Rationale: patch-only replay is fragile for MVP rollback, while snapshot-only storage makes Agent behavior difficult to inspect.

### Manual Timeline Edits

Manual edits in the right timeline will remain silent in the visible conversation, but they must be saved as itinerary patches and itinerary versions.

The next Agent request must include the latest manually edited itinerary context.

Rationale: this keeps the chat readable while preserving a complete state history for persistence and rollback.

### DeepSeek Model Selection

The exact DeepSeek model should be controlled by configuration and not hard-coded in product logic.

MVP implementation should expose a setting such as `DEEPSEEK_MODEL`, with the selected model documented in `.env.example` and backend configuration.

### Date-Specific Planning

MVP does not require exact calendar dates. Day 1, Day 2, and duration-based planning are acceptable.

If the user provides exact travel dates, the system may store them and pass them to the Agent, but date-specific weather and time-sensitive provider behavior are not mandatory for the first MVP loop.

## 12. Goal Development Prompt

Use this prompt for the next goal-mode development slice:

```text
Continue development for the AI Travel Planner Web Demo according to specs/001-ai-travel-planner/mvp-prd.md.

Goal:
- Build the MVP foundation for Agent-driven itinerary generation and modification.
- The primary loop is: user talks to the Agent, DeepSeek generates or patches the right-side itinerary, the user can manually edit the right timeline, and every itinerary state is persisted to SQLite.
- Keep the existing single-page desktop Web workspace with left Agent conversation, center AMap planning surface, and right itinerary timeline.

Hard requirements:
- Use DeepSeek as the real Agent provider when configured. Keep mock/deterministic behavior only for tests and local fallback.
- Do not hard-code the DeepSeek model. Add configuration such as DEEPSEEK_MODEL and document it in backend/.env.example.
- MVP itinerary POIs must be backed by real AMap POI data. Do not add fake itinerary POIs to the final saved itinerary.
- If AMap cannot resolve a POI confidently, ask the user to choose from candidates or show a clear unresolved state. Do not silently insert a fake location.
- Add persistence for Agent conversation sessions, conversation turns, itinerary versions/snapshots, and itinerary patches.
- Store both full itinerary snapshots and patches.
- Manual right-timeline edits must persist to SQLite and create itinerary patch/version records.
- Manual timeline edits should not create visible chat messages, but the latest edited itinerary must be included in the next Agent request context.
- Implement single-branch supersession for editing previous user messages:
  - restore the itinerary snapshot associated with the edited message,
  - mark later turns as superseded,
  - regenerate a new Agent response from the edited message and restored context.
- Keep the plan comparison tab as auxiliary. Do not make comparison drive the main itinerary state in this slice.
- Exact travel dates are optional in MVP. Day 1 / Day 2 planning is acceptable; preserve dates only when the user provides them.

Suggested implementation order:
1. Add backend models/tables for conversation_sessions, conversation_turns, itinerary_versions, itinerary_patches, and persisted AMap POI references.
2. Add itinerary patch validation and application service shared by Agent patches and manual timeline edits.
3. Add API endpoints for starting/loading Agent sessions, sending messages, editing previous messages, applying itinerary patches, and restoring itinerary versions.
4. Add DeepSeek Agent orchestration with structured output: user-facing reply plus itinerary patch or itinerary replacement.
5. Wire the frontend Agent panel to the new session/message APIs.
6. Wire DailyTimeline manual edits to backend persistence instead of frontend-only state.
7. Ensure Agent requests include current itinerary, preference card, selected city, selected map POI, candidate AMap POIs, active Day, active segment, and conversation history.
8. Add tests for persistence, patch validation, rollback on edited prior message, AMap-backed POI insertion, and manual edit reload.

Acceptance tests:
- User can ask for a 2-day Beijing itinerary and see a saved right-side timeline.
- User can manually edit a segment time, reload the itinerary, and see the saved edit.
- User can ask the Agent to move an attraction to afternoon; the timeline updates through a structured patch and persists.
- User can add a nearby restaurant through Agent only after it is resolved through AMap.
- User can edit a previous user message; later turns are marked superseded and the itinerary restores to that turn's snapshot before regeneration.
- Invalid Agent patches are rejected with a visible error and do not corrupt the saved itinerary.

Verification:
- Run backend tests with .\trip\Scripts\python.exe -m pytest backend/tests.
- Run frontend tests with npm test.
- Run frontend build with npm run build.
```
