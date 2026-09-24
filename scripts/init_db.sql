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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
