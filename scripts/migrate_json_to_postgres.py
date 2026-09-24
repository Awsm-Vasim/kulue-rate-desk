"""One-time (re-runnable/idempotent) import of the current JSON-file data
into Postgres. Run against the Neon "dev" branch first to verify, then
against "main" for the production cutover:

    python3 scripts/init_db.py                       # once per branch
    python3 scripts/migrate_json_to_postgres.py

Requires DATABASE_URL to be set (via .env locally, or the environment) --
point it at whichever Neon branch you're migrating into before running.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.geocode import geocode, normalize_place_key
from app.pricing import COORD_GRID_PRECISION
from scripts.data_cleaning import clean_cache_seed, dedupe_training_rows, flag_thin_vehicle_classes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def _load_json(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _normalize_geo_cache(raw):
    """The committed seed files use un-normalized keys (e.g. 'Aluva'), but
    app/geocode.py's geocode()/route_km() now look up by normalize_place_key()
    (casefolded, whitespace-collapsed). Re-key here so migrated data actually
    gets hit by live lookups instead of silently missing and re-geocoding."""
    out = {}
    for key, value in raw.items():
        out[normalize_place_key(key)] = value
    return out


def _normalize_dist_cache(raw):
    """distance_cache keys are '<origin> | <destination>' sorted on the RAW
    strings; route_km() now sorts on normalized strings, so each key must be
    rebuilt from its two parts rather than just re-cased as a whole string."""
    out = {}
    for key, value in raw.items():
        parts = key.split(" | ")
        if len(parts) != 2:
            continue  # e.g. the known-corrupted single-giant-key entry
        new_key = " | ".join(sorted(normalize_place_key(p) for p in parts))
        out[new_key] = value
    return out


def _corridor_pair_key(a_coords, b_coords):
    a = (round(a_coords[0], COORD_GRID_PRECISION), round(a_coords[1], COORD_GRID_PRECISION))
    b = (round(b_coords[0], COORD_GRID_PRECISION), round(b_coords[1], COORD_GRID_PRECISION))
    lo, hi = sorted([a, b])
    return f"{lo[0]},{lo[1]}|{hi[0]},{hi[1]}"


def migrate_training_data(conn, model_data):
    unique_rows, total_before, groups_with_dupes = dedupe_training_rows(model_data["training_rows"])
    print(f"training_rows: {total_before} raw -> {len(unique_rows)} unique "
          f"({groups_with_dupes} duplicate groups)")

    for r in unique_rows:
        conn.execute(
            """INSERT INTO training_rows
                   (origin_raw, destination_raw, distance_km, vehicle_type, freight, per_km, occurrence_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (origin_raw, destination_raw, distance_km, vehicle_type, freight, per_km)
               DO UPDATE SET occurrence_count = EXCLUDED.occurrence_count""",
            (r["o"], r["d"], r["km"], r["veh"], r["freight"], r["per_km"], r["occurrence_count"]),
        )

    thin_flags = flag_thin_vehicle_classes(unique_rows)
    thin_list = [v for v, info in thin_flags.items() if info["sample_quality"] == "thin"]
    print(f"vehicle classes flagged thin (n < 3): {thin_list or 'none'}")
    for veh, info in thin_flags.items():
        conn.execute(
            """INSERT INTO vehicle_stats (vehicle_type, n, sample_quality)
               VALUES (%s, %s, %s)
               ON CONFLICT (vehicle_type) DO UPDATE SET n = EXCLUDED.n, sample_quality = EXCLUDED.sample_quality""",
            (veh, info["n"], info["sample_quality"]),
        )

    for bucket, avg in model_data.get("bucket_avg_per_km", {}).items():
        conn.execute(
            """INSERT INTO bucket_avg_per_km (distance_km_bucket, avg_per_km) VALUES (%s, %s)
               ON CONFLICT (distance_km_bucket) DO UPDATE SET avg_per_km = EXCLUDED.avg_per_km""",
            (float(bucket), avg),
        )

    conn.execute(
        """INSERT INTO model_runs (generated_at, sample_size, overall_per_km, min_plausible_per_km, notes)
           VALUES (%s, %s, %s, %s, %s)""",
        (
            model_data.get("generated_at"),
            model_data.get("sample_size"),
            model_data.get("overall_per_km"),
            model_data.get("min_plausible_per_km"),
            model_data.get("notes"),
        ),
    )


def migrate_corridors(conn, model_data, geo_cache):
    n_ok, n_failed = 0, 0
    for c in model_data.get("known_corridors", []):
        a_coords = geocode(c["a"], geo_cache)
        b_coords = geocode(c["b"], geo_cache)
        if not a_coords or not b_coords:
            n_failed += 1
            print(f"  could not geocode corridor endpoint(s): {c['a']} <-> {c['b']}")
            continue
        n_ok += 1
        conn.execute(
            """INSERT INTO corridors
                   (origin_name, destination_name, origin_lat, origin_lon,
                    destination_lat, destination_lon, pair_key, n, median_per_km,
                    min_freight, max_freight, vehicles)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (pair_key) DO UPDATE SET
                   n = EXCLUDED.n, median_per_km = EXCLUDED.median_per_km,
                   min_freight = EXCLUDED.min_freight, max_freight = EXCLUDED.max_freight,
                   vehicles = EXCLUDED.vehicles, updated_at = now()""",
            (
                c["a"], c["b"], a_coords[0], a_coords[1], b_coords[0], b_coords[1],
                _corridor_pair_key(a_coords, b_coords), c["n"], c["median_per_km"],
                c.get("min_freight"), c.get("max_freight"), json.dumps(c.get("vehicles")),
            ),
        )
    print(f"corridors: {n_ok} geocoded and inserted, {n_failed} failed")


def migrate_caches(conn, geo_cache):
    n = 0
    for key, value in geo_cache.items():
        lat, lon = (value[0], value[1]) if value else (None, None)
        conn.execute(
            """INSERT INTO geocode_cache (place_key, lat, lon, resolved) VALUES (%s, %s, %s, %s)
               ON CONFLICT (place_key) DO UPDATE SET
                   lat = EXCLUDED.lat, lon = EXCLUDED.lon, resolved = EXCLUDED.resolved, updated_at = now()""",
            (key, lat, lon, value is not None),
        )
        n += 1
    print(f"geocode_cache: {n} entries")

    dist_seed = _load_json("distance_cache_seed.json") or {}
    dist_runtime = _load_json("distance_cache.json") or {}
    merged = _normalize_dist_cache({**dist_seed, **dist_runtime})
    cleaned, dropped = clean_cache_seed(merged)
    for key, value in cleaned.items():
        conn.execute(
            """INSERT INTO distance_cache (pair_key, distance_km) VALUES (%s, %s)
               ON CONFLICT (pair_key) DO UPDATE SET distance_km = EXCLUDED.distance_km, updated_at = now()""",
            (key, value),
        )
    print(f"distance_cache: {len(cleaned)} entries, dropped {len(dropped)} oversized/corrupted key(s)")


def migrate_feedback(conn):
    entries = _load_json("feedback.json")
    if not entries:
        print("feedback.json: none found, skipping")
        return
    for e in entries:
        conn.execute(
            "INSERT INTO feedback (created_at, accurate, comment, quote) VALUES (%s, %s, %s, %s)",
            (e.get("at"), e["accurate"], e.get("comment"), json.dumps(e["quote"])),
        )
    print(f"feedback: {len(entries)} entries backfilled")


def main():
    if not db.enabled():
        print("DATABASE_URL is not set (check your .env) -- nothing to migrate into.")
        sys.exit(1)

    model_data = _load_json("training_rows.json")
    if not model_data:
        print("data/training_rows.json not found.")
        sys.exit(1)

    geo_seed = _load_json("geocode_cache_seed.json") or {}
    geo_runtime = _load_json("geocode_cache.json") or {}
    geo_cache = _normalize_geo_cache({**geo_seed, **geo_runtime})  # runtime wins

    with db.get_pool().connection() as conn:
        migrate_training_data(conn, model_data)
        migrate_corridors(conn, model_data, geo_cache)
        migrate_caches(conn, geo_cache)
        migrate_feedback(conn)

    print("\nDone. Spot-check a few rows before pointing the app at this database.")


if __name__ == "__main__":
    main()
