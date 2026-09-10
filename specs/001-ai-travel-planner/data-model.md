# Data Model: AI Travel Planner Agent Demo

## Entity Overview

### DefaultUser

- `id`: stable local identifier
- `display_name`: default label
- `created_at`

Relationships:

- Has one active `PreferenceProfile`
- Has many `InspirationSet`
- Has many `ItineraryPlan`

### PreferenceProfile

- `id`
- `user_id`
- `budget_range`
- `pace_preference`
- `transport_preferences`
- `food_preferences`
- `photo_preference`
- `accessibility_notes`
- `party_size`
- `traveler_types`: adult, child, elder, pet, reduced_mobility, other
- `updated_at`

Validation:

- `party_size` must be at least 1 when present.
- Traveler type counts must not exceed party size unless marked as unspecified.

### PreferenceSummaryCard

- `id`
- `profile_id`
- `extracted_items`
- `user_confirmed_items`
- `removed_items`
- `source_conversation_id`
- `updated_at`

State:

- `draft` -> `confirmed` -> `revised`

### InspirationSet

- `id`
- `user_id`
- `city`
- `status`: uploading, extracting, needs_confirmation, ready, failed
- `theme_summary`
- `created_at`
- `updated_at`

Relationships:

- Has many `SourceMaterial`
- Has many `ExtractionResult`
- Can generate many `ItineraryPlan`
- Can have one or more `ReminderDraft`

### SourceMaterial

- `id`
- `inspiration_set_id`
- `kind`: screenshot, guide_text, map_screenshot, social_link, scenery_photo, image_set_item
- `raw_text`
- `link_url`
- `thumbnail_path`
- `original_path`
- `original_retention`: temporary_cache, long_term_opt_in, none
- `cache_status`: retained, manually_cleared
- `created_at`

Validation:

- Long-term original storage requires explicit opt-in.
- Thumbnail or structured extraction must exist before original cache can be cleared.

### ExtractionResult

- `id`
- `source_material_id`
- `city_candidates`
- `poi_candidates`
- `style_tags`
- `budget_clues`
- `route_clues`
- `confidence`
- `needs_user_confirmation`
- `source_links`
- `created_at`

### POI

- `id`
- `name`
- `city`
- `category`
- `latitude`
- `longitude`
- `photo_url`
- `source`
- `confidence`

### ItineraryPlan

- `id`
- `user_id`
- `inspiration_set_id`
- `template_type`: low_budget, photo_first, relaxed_pace, custom
- `title`
- `city`
- `budget_target`
- `budget_estimate`
- `budget_delta_explanation`
- `decision_rationale`
- `status`: draft, saved, archived
- `created_at`
- `updated_at`

Relationships:

- Has many `ItineraryDay`
- Has many `PlanComparisonItem`

### ItineraryDay

- `id`
- `plan_id`
- `day_number`
- `date`
- `weather_summary`
- `risk_summary`
- `total_estimated_cost`

### ItinerarySegment

- `id`
- `day_id`
- `segment_order`
- `kind`: activity, transport, meal, rest
- `start_time`
- `end_time`
- `poi_id`
- `transport_mode`
- `estimated_cost`
- `notes`
- `weather_signal_id`
- `traffic_crowding_signal_id`
- `ticket_lookup_result_id`

### RouteOption

- `id`
- `plan_id`
- `from_poi_id`
- `to_poi_id`
- `transport_mode`
- `distance_meters`
- `duration_minutes`
- `cost_estimate`
- `crowding_risk`
- `source`
- `queried_at`

### TicketLookupResult

- `id`
- `segment_id`
- `ticket_type`: train, flight, attraction, reservation
- `status`: available, unavailable, reservation_required, unknown, provider_failed
- `price_estimate`
- `booking_url`
- `source_name`
- `source_url`
- `credibility_rank`: official, aggregator, search
- `queried_at`
- `caveat`

Validation:

- All displayed ticket results must include `queried_at`, `source_name`, and the required caveat.

### WeatherSignal

- `id`
- `city`
- `date`
- `hourly_forecast`
- `daily_summary`
- `risk_level`: ideal, neutral, risky
- `purpose_impact_reason`
- `source`
- `queried_at`

### TrafficCrowdingSignal

- `id`
- `route_option_id`
- `real_data_available`
- `crowding_level`
- `estimated_reason`
- `recommended_departure_adjustment`
- `source`
- `queried_at`

### ReminderDraft

- `id`
- `inspiration_set_id`
- `itinerary_plan_id`
- `email_address`
- `trigger_date`
- `subject`
- `body`
- `simulated_status`: not_created, scheduled, simulated_sent, cancelled
- `created_at`

### ProviderResult

- `id`
- `provider_kind`
- `provider_name`
- `is_mock`
- `status`
- `source_url`
- `queried_at`
- `confidence`
- `credibility_rank`
- `user_visible_caveat`
- `payload_reference`

## State Transitions

- InspirationSet: `uploading` -> `extracting` -> `needs_confirmation` -> `ready` -> `failed`
- ItineraryPlan: `draft` -> `saved` -> `archived`
- PreferenceSummaryCard: `draft` -> `confirmed` -> `revised`
- ReminderDraft: `not_created` -> `scheduled` -> `simulated_sent` or `cancelled`
