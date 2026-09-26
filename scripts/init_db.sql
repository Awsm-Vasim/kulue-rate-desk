-- Idempotent schema for Sahirate's Postgres backend. Safe to re-run.
-- Run via: python3 scripts/init_db.py

CREATE TABLE IF NOT EXISTS corridors (
    id SERIAL PRIMARY KEY,
    origin_name TEXT NOT NULL,
    destination_name TEXT NOT NULL,
    origin_lat DOUBLE PRECISION,
    origin_lon DOUBLE PRECISION,
    destination_lat DOUBLE PRECISION,
    destination_lon DOUBLE PRECISION,
    pair_key TEXT UNIQUE,
    km DOUBLE PRECISION,
    n INTEGER NOT NULL,
    median_per_km DOUBLE PRECISION NOT NULL,
    min_freight NUMERIC,
    max_freight NUMERIC,
    vehicles JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS training_rows (
    id SERIAL PRIMARY KEY,
    origin_raw TEXT NOT NULL,
    destination_raw TEXT NOT NULL,
    distance_km DOUBLE PRECISION NOT NULL CHECK (distance_km > 0),
    vehicle_type TEXT NOT NULL,
    freight NUMERIC NOT NULL CHECK (freight > 0),
    per_km DOUBLE PRECISION NOT NULL CHECK (per_km > 0),
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (origin_raw, destination_raw, distance_km, vehicle_type, freight, per_km)
);

CREATE TABLE IF NOT EXISTS bucket_avg_per_km (
    distance_km_bucket DOUBLE PRECISION PRIMARY KEY,
    avg_per_km DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS vehicle_stats (
    vehicle_type TEXT PRIMARY KEY,
    n INTEGER NOT NULL,
    median_per_km DOUBLE PRECISION,
    sample_quality TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS model_runs (
    id SERIAL PRIMARY KEY,
    generated_at TIMESTAMPTZ,
    sample_size INTEGER,
    overall_per_km DOUBLE PRECISION,
    min_plausible_per_km DOUBLE PRECISION,
    notes TEXT,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    place_key TEXT PRIMARY KEY,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    resolved BOOLEAN NOT NULL,
    state TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE geocode_cache ADD COLUMN IF NOT EXISTS state TEXT;

CREATE TABLE IF NOT EXISTS distance_cache (
    pair_key TEXT PRIMARY KEY,
    distance_km DOUBLE PRECISION,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accurate BOOLEAN NOT NULL,
    comment TEXT,
    quote JSONB NOT NULL
);

-- Admin dashboard: raw ingested WhatsApp messages + per-upload batch history.

CREATE TABLE IF NOT EXISTS batches (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    groups_in_batch INTEGER NOT NULL,
    new_messages_in_batch INTEGER NOT NULL,
    added INTEGER NOT NULL,
    duplicates_skipped INTEGER NOT NULL,
    no_timestamp_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    group_raw TEXT NOT NULL,
    group_key TEXT NOT NULL,
    dt TIMESTAMP,
    text TEXT NOT NULL,
    batch_id INTEGER REFERENCES batches(id),
    route_origin TEXT,
    route_dest TEXT,
    freight NUMERIC,
    vehicle_type TEXT,
    material TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Postgres never treats two NULLs as equal in a unique index, so a message
-- with an unparseable timestamp (dt IS NULL) is never deduped against any
-- other message -- matching the original pipeline's explicit behavior of
-- always keeping (never deduping) messages it couldn't timestamp.
CREATE UNIQUE INDEX IF NOT EXISTS messages_dedupe_key
    ON messages (group_key, dt, text);

CREATE INDEX IF NOT EXISTS messages_route_idx ON messages (route_origin, route_dest)
    WHERE route_origin IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_priced_idx ON messages (vehicle_type)
    WHERE route_origin IS NOT NULL AND freight IS NOT NULL AND vehicle_type IS NOT NULL;
