from src.core.config import get_settings
from src.core.database import sqlite_path_from_url
import sqlite3


EXTRACTION_RESULT_COLUMNS = {
    "provider_name": "TEXT NOT NULL DEFAULT ''",
    "fallback_used": "INTEGER NOT NULL DEFAULT 0",
    "provider_failure_reason": "TEXT",
    "user_visible_caveat": "TEXT",
}

POI_COLUMNS = {
    "amap_id": "TEXT",
    "parent_poi_id": "TEXT",
    "type": "TEXT NOT NULL DEFAULT ''",
    "district": "TEXT NOT NULL DEFAULT ''",
    "address": "TEXT NOT NULL DEFAULT ''",
    "source_note": "TEXT NOT NULL DEFAULT ''",
    "source_url": "TEXT",
    "photos_json": "TEXT NOT NULL DEFAULT '[]'",
    "provider_type_code": "TEXT",
    "tags_json": "TEXT NOT NULL DEFAULT '[]'",
    "source_claims_json": "TEXT NOT NULL DEFAULT '[]'",
}

ITINERARY_DAY_COLUMNS = {
    "title": "TEXT NOT NULL DEFAULT ''",
}

ITINERARY_PLAN_COLUMNS = {
    "budget_tier": "TEXT NOT NULL DEFAULT 'unknown'",
}

ITINERARY_SEGMENT_COLUMNS = {
    "estimate_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
    "semantic_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
}

ROUTE_OPTION_COLUMNS = {
    "from_segment_id": "TEXT",
    "to_segment_id": "TEXT",
    "provider": "TEXT NOT NULL DEFAULT ''",
    "mode": "TEXT NOT NULL DEFAULT ''",
    "label": "TEXT NOT NULL DEFAULT ''",
    "is_selected": "INTEGER NOT NULL DEFAULT 0",
    "sort_order": "INTEGER NOT NULL DEFAULT 0",
    "duration_seconds": "INTEGER NOT NULL DEFAULT 0",
    "cost_amount": "REAL NOT NULL DEFAULT 0",
    "cost_currency": "TEXT NOT NULL DEFAULT 'CNY'",
    "polyline_json": "TEXT NOT NULL DEFAULT '[]'",
    "steps_json": "TEXT NOT NULL DEFAULT '[]'",
    "provider_payload_json": "TEXT NOT NULL DEFAULT '{}'",
    "error_json": "TEXT",
}

WEATHER_SIGNAL_COLUMNS = {
    "data_status": "TEXT NOT NULL DEFAULT 'unavailable'",
    "confidence": "REAL NOT NULL DEFAULT 0",
    "failure_reason": "TEXT",
    "source_url": "TEXT",
    "user_visible_caveat": "TEXT NOT NULL DEFAULT ''",
    "provider_name": "TEXT NOT NULL DEFAULT ''",
    "fallback_used": "INTEGER NOT NULL DEFAULT 0",
}

PREFERENCE_SUMMARY_CARD_COLUMNS = {
    "summary_text": "TEXT NOT NULL DEFAULT ''",
}

PREFERENCE_MEMORY_COLUMNS = {
    "structured_facts_json": 'TEXT NOT NULL DEFAULT \'{"version":"travel-memory-v1","facts":[],"autoUpdateClassifications":[]}\'',
    "compiled_rules_json": "TEXT NOT NULL DEFAULT '{}'",
    "pending_confirmations_json": "TEXT NOT NULL DEFAULT '[]'",
}

PLANNING_RUN_COLUMNS = {
    "source_assessments_json": "TEXT NOT NULL DEFAULT '[]'",
}

CONVERSATION_TURN_COLUMNS = {
    "planning_run_id": "TEXT",
}

ITINERARY_PATCH_COLUMNS = {
    "planning_run_id": "TEXT",
    "mutation_id": "TEXT",
}

AMAP_POI_CANDIDATE_COLUMNS = {
    "segment_id": "TEXT",
}


def initialize_database() -> None:
    settings = get_settings()
    db_path = sqlite_path_from_url(settings.database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as connection:
        # Journal mode is a database-level setting.  Changing it from every
        # request connection can require an exclusive lock while another Agent
        # turn is writing, which turns a healthy real-provider run into a
        # spurious ``database is locked`` failure.  Configure it once before
        # the API starts accepting requests; request connections only need the
        # busy timeout in ``get_db``.
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS inspiration_sets (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                city TEXT,
                status TEXT NOT NULL,
                theme_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_materials (
                id TEXT PRIMARY KEY,
                inspiration_set_id TEXT,
                kind TEXT NOT NULL,
                raw_text TEXT,
                link_url TEXT,
                thumbnail_path TEXT,
                original_path TEXT,
                original_retention TEXT NOT NULL,
                cache_status TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS extraction_results (
                id TEXT PRIMARY KEY,
                inspiration_set_id TEXT NOT NULL,
                city_candidates TEXT NOT NULL,
                poi_candidates TEXT NOT NULL,
                style_tags TEXT NOT NULL,
                budget_clues TEXT NOT NULL,
                route_clues TEXT NOT NULL,
                confidence REAL NOT NULL,
                needs_user_confirmation INTEGER NOT NULL,
                source_links TEXT NOT NULL,
                provider_name TEXT NOT NULL DEFAULT '',
                fallback_used INTEGER NOT NULL DEFAULT 0,
                provider_failure_reason TEXT,
                user_visible_caveat TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS itinerary_plans (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                inspiration_set_id TEXT NOT NULL,
                template_type TEXT NOT NULL,
                title TEXT NOT NULL,
                city TEXT NOT NULL,
                budget_target REAL,
                budget_tier TEXT NOT NULL DEFAULT 'unknown',
                budget_estimate REAL NOT NULL,
                budget_delta_explanation TEXT NOT NULL,
                decision_rationale TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pois (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                name TEXT NOT NULL,
                city TEXT NOT NULL,
                category TEXT NOT NULL,
                latitude REAL,
                longitude REAL,
                photo_url TEXT,
                source TEXT NOT NULL,
                confidence REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS itinerary_days (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                day_number INTEGER NOT NULL,
                date TEXT,
                weather_summary TEXT NOT NULL,
                risk_summary TEXT NOT NULL,
                total_estimated_cost REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS itinerary_segments (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                day_id TEXT NOT NULL,
                segment_order INTEGER NOT NULL,
                kind TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                poi_id TEXT NOT NULL,
                transport_mode TEXT NOT NULL,
                estimated_cost REAL NOT NULL,
                estimate_metadata_json TEXT NOT NULL DEFAULT '{}',
                semantic_metadata_json TEXT NOT NULL DEFAULT '{}',
                notes TEXT NOT NULL,
                weather_signal_id TEXT,
                traffic_crowding_signal_id TEXT,
                ticket_lookup_result_id TEXT
            );

            CREATE TABLE IF NOT EXISTS route_options (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                from_segment_id TEXT,
                to_segment_id TEXT,
                from_poi_id TEXT NOT NULL,
                to_poi_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL DEFAULT '',
                label TEXT NOT NULL DEFAULT '',
                is_selected INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                transport_mode TEXT NOT NULL,
                distance_meters INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                duration_minutes INTEGER NOT NULL,
                cost_amount REAL NOT NULL DEFAULT 0,
                cost_currency TEXT NOT NULL DEFAULT 'CNY',
                cost_estimate REAL NOT NULL,
                crowding_risk TEXT NOT NULL,
                source TEXT NOT NULL,
                polyline_json TEXT NOT NULL DEFAULT '[]',
                steps_json TEXT NOT NULL DEFAULT '[]',
                provider_payload_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT,
                queried_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS weather_signals (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                city TEXT NOT NULL,
                date TEXT NOT NULL,
                hourly_forecast TEXT NOT NULL,
                daily_summary TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                purpose_impact_reason TEXT NOT NULL,
                source TEXT NOT NULL,
                data_status TEXT NOT NULL DEFAULT 'unavailable',
                confidence REAL NOT NULL DEFAULT 0,
                failure_reason TEXT,
                source_url TEXT,
                user_visible_caveat TEXT NOT NULL DEFAULT '',
                provider_name TEXT NOT NULL DEFAULT '',
                fallback_used INTEGER NOT NULL DEFAULT 0,
                queried_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS traffic_crowding_signals (
                id TEXT PRIMARY KEY,
                route_option_id TEXT NOT NULL,
                real_data_available INTEGER NOT NULL,
                crowding_level TEXT NOT NULL,
                estimated_reason TEXT NOT NULL,
                recommended_departure_adjustment TEXT NOT NULL,
                source TEXT NOT NULL,
                queried_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS poi_risk_alerts (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                poi_name TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT,
                sources_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0,
                failure_reason TEXT,
                user_visible_caveat TEXT NOT NULL DEFAULT '',
                queried_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ticket_lookup_results (
                id TEXT PRIMARY KEY,
                segment_id TEXT NOT NULL,
                ticket_type TEXT NOT NULL,
                status TEXT NOT NULL,
                price_estimate REAL NOT NULL,
                booking_url TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                credibility_rank TEXT NOT NULL,
                queried_at TEXT NOT NULL,
                caveat TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                fallback_used INTEGER NOT NULL,
                provider_failure_reason TEXT,
                confidence REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS preference_profiles (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                budget_range TEXT NOT NULL,
                pace_preference TEXT NOT NULL,
                transport_preferences TEXT NOT NULL,
                food_preferences TEXT NOT NULL,
                photo_preference TEXT NOT NULL,
                accessibility_notes TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                traveler_types TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS preference_summary_cards (
                id TEXT PRIMARY KEY,
                profile_id TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                traveler_types TEXT NOT NULL,
                budget_range TEXT NOT NULL,
                pace_preference TEXT NOT NULL,
                summary_text TEXT NOT NULL DEFAULT '',
                items TEXT NOT NULL,
                removed_items TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS travel_preference_memories (
                user_id TEXT PRIMARY KEY,
                memory_text TEXT NOT NULL,
                structured_facts_json TEXT NOT NULL DEFAULT '{"version":"travel-memory-v1","facts":[],"autoUpdateClassifications":[]}',
                compiled_rules_json TEXT NOT NULL DEFAULT '{}',
                pending_confirmations_json TEXT NOT NULL DEFAULT '[]',
                auto_update_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_preference_memories (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                memory_text TEXT NOT NULL,
                structured_facts_json TEXT NOT NULL DEFAULT '{"version":"travel-memory-v1","facts":[],"autoUpdateClassifications":[]}',
                compiled_rules_json TEXT NOT NULL DEFAULT '{}',
                pending_confirmations_json TEXT NOT NULL DEFAULT '[]',
                auto_update_enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plan_comparisons (
                id TEXT PRIMARY KEY,
                inspiration_set_id TEXT NOT NULL,
                city TEXT NOT NULL,
                plan_ids TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                fallback_used INTEGER NOT NULL,
                user_visible_caveat TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminder_drafts (
                id TEXT PRIMARY KEY,
                inspiration_set_id TEXT,
                itinerary_plan_id TEXT,
                email_address TEXT NOT NULL,
                trigger_date TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                simulated_status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                city TEXT NOT NULL,
                active_plan_id TEXT NOT NULL,
                active_version_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_turns (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                parent_turn_id TEXT,
                itinerary_version_id TEXT,
                planning_run_id TEXT,
                agent_request_json TEXT,
                agent_response_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_choice_executions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source_turn_id TEXT NOT NULL,
                source_user_turn_id TEXT NOT NULL,
                choice_id TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                expected_base_version_id TEXT,
                result_version_id TEXT,
                execution_turn_id TEXT,
                request_turn_id TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                continuation_json TEXT,
                checkpoint_fingerprint TEXT,
                outcome_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, source_turn_id, choice_id)
            );

            CREATE TABLE IF NOT EXISTS segment_visit_facts (
                id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                segment_id TEXT NOT NULL,
                amap_poi_id TEXT NOT NULL,
                visit_date TEXT NOT NULL,
                refresh_status TEXT NOT NULL,
                facts_json TEXT NOT NULL DEFAULT '{}',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_fingerprint TEXT NOT NULL,
                queried_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(segment_id, amap_poi_id, visit_date)
            );
            CREATE INDEX IF NOT EXISTS idx_segment_visit_facts_plan
                ON segment_visit_facts(plan_id, segment_id);

            CREATE TABLE IF NOT EXISTS proposal_segment_visit_facts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                portfolio_id TEXT NOT NULL,
                proposal_id TEXT NOT NULL,
                proposal_segment_id TEXT NOT NULL,
                material_fingerprint TEXT NOT NULL,
                amap_poi_id TEXT NOT NULL,
                visit_date TEXT NOT NULL,
                refresh_status TEXT NOT NULL,
                facts_json TEXT NOT NULL DEFAULT '{}',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_fingerprint TEXT NOT NULL,
                queried_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                UNIQUE(proposal_id, proposal_segment_id, amap_poi_id, visit_date, material_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_proposal_visit_facts_scope
                ON proposal_segment_visit_facts(session_id, portfolio_id, proposal_id);

            CREATE TABLE IF NOT EXISTS agent_plan_portfolios (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source_user_turn_id TEXT NOT NULL,
                source_assistant_turn_id TEXT,
                expected_base_version_id TEXT,
                source_observation_fingerprint TEXT NOT NULL,
                request_contract_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                selected_proposal_id TEXT,
                dominant_proposal_id TEXT,
                summary_json TEXT NOT NULL,
                failure_reason TEXT,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, source_user_turn_id)
            );

            CREATE TABLE IF NOT EXISTS agent_plan_proposals (
                id TEXT PRIMARY KEY,
                portfolio_id TEXT NOT NULL,
                choice_id TEXT NOT NULL,
                rank_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                brief_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                score_json TEXT NOT NULL,
                verifier_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                canonical_signature TEXT NOT NULL,
                generation_lineage_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(portfolio_id, choice_id),
                UNIQUE(portfolio_id, canonical_signature)
            );

            CREATE TABLE IF NOT EXISTS itinerary_versions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_turn_id TEXT,
                source_patch_id TEXT,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS saved_itinerary_versions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, plan_id, version_id)
            );

            CREATE TABLE IF NOT EXISTS itinerary_patches (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                base_version_id TEXT,
                result_version_id TEXT,
                source_type TEXT NOT NULL,
                source_turn_id TEXT,
                planning_run_id TEXT,
                mutation_id TEXT,
                operations_json TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                validation_errors_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS timeline_mutation_transactions (
                mutation_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                source_turn_id TEXT,
                plan_id TEXT NOT NULL,
                base_version_id TEXT NOT NULL,
                result_version_id TEXT,
                patch_id TEXT,
                status TEXT NOT NULL,
                transaction_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS amap_poi_candidates (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                query TEXT NOT NULL,
                segment_id TEXT,
                city TEXT NOT NULL,
                category TEXT NOT NULL,
                status TEXT NOT NULL,
                candidates_json TEXT NOT NULL,
                selected_amap_id TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS planning_runs (
                id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                user_input TEXT NOT NULL,
                preference_summary TEXT NOT NULL,
                itinerary_plan_id TEXT,
                itinerary_version_id TEXT,
                understood_requirements_json TEXT NOT NULL,
                constraint_summary_json TEXT NOT NULL,
                tool_calls_json TEXT NOT NULL,
                source_assessments_json TEXT NOT NULL DEFAULT '[]',
                feasibility_report_json TEXT,
                final_summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        ensure_columns(connection, "extraction_results", EXTRACTION_RESULT_COLUMNS)
        ensure_columns(connection, "pois", POI_COLUMNS)
        ensure_nullable_poi_coordinates(connection)
        ensure_columns(connection, "itinerary_plans", ITINERARY_PLAN_COLUMNS)
        ensure_columns(connection, "itinerary_days", ITINERARY_DAY_COLUMNS)
        ensure_columns(connection, "itinerary_segments", ITINERARY_SEGMENT_COLUMNS)
        ensure_columns(connection, "route_options", ROUTE_OPTION_COLUMNS)
        ensure_columns(connection, "weather_signals", WEATHER_SIGNAL_COLUMNS)
        ensure_columns(connection, "preference_summary_cards", PREFERENCE_SUMMARY_CARD_COLUMNS)
        ensure_columns(connection, "travel_preference_memories", PREFERENCE_MEMORY_COLUMNS)
        ensure_columns(connection, "session_preference_memories", PREFERENCE_MEMORY_COLUMNS)
        ensure_columns(connection, "planning_runs", PLANNING_RUN_COLUMNS)
        ensure_columns(connection, "source_materials", {"metadata_json": "TEXT NOT NULL DEFAULT '{}'"})
        ensure_columns(connection, "conversation_turns", CONVERSATION_TURN_COLUMNS)
        ensure_columns(connection, "itinerary_patches", ITINERARY_PATCH_COLUMNS)
        ensure_columns(connection, "amap_poi_candidates", AMAP_POI_CANDIDATE_COLUMNS)
        ensure_columns(
            connection,
            "agent_choice_executions",
            {
                "request_turn_id": "TEXT",
                "continuation_json": "TEXT",
                "checkpoint_fingerprint": "TEXT",
                "outcome_json": "TEXT",
            },
        )


def ensure_columns(connection: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for column_name, column_definition in columns.items():
        if column_name not in existing:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def ensure_nullable_poi_coordinates(connection: sqlite3.Connection) -> None:
    info = connection.execute("PRAGMA table_info(pois)").fetchall()
    column_by_name = {row[1]: row for row in info}
    latitude = column_by_name.get("latitude")
    longitude = column_by_name.get("longitude")
    if latitude is None or longitude is None or (not latitude[3] and not longitude[3]):
        return

    connection.executescript(
        """
        ALTER TABLE pois RENAME TO pois_old_nullable_migration;

        CREATE TABLE pois (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            name TEXT NOT NULL,
            city TEXT NOT NULL,
            category TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            photo_url TEXT,
            source TEXT NOT NULL,
            confidence REAL NOT NULL,
            amap_id TEXT,
            parent_poi_id TEXT,
            type TEXT NOT NULL DEFAULT '',
            district TEXT NOT NULL DEFAULT '',
            address TEXT NOT NULL DEFAULT '',
            source_note TEXT NOT NULL DEFAULT '',
            source_url TEXT,
            photos_json TEXT NOT NULL DEFAULT '[]',
            provider_type_code TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_claims_json TEXT NOT NULL DEFAULT '[]'
        );

        INSERT INTO pois (
            id, plan_id, name, city, category, latitude, longitude,
            photo_url, source, confidence, amap_id, parent_poi_id, type, district, address,
            source_note, source_url, photos_json, provider_type_code, tags_json, source_claims_json
        )
        SELECT
            id, plan_id, name, city, category, latitude, longitude,
            photo_url, source, confidence, amap_id, parent_poi_id, type, district, address,
            source_note, source_url, photos_json, provider_type_code, tags_json, source_claims_json
        FROM pois_old_nullable_migration;

        DROP TABLE pois_old_nullable_migration;
        """
    )
