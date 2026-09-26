"""Recomputes the trained pricing data (known_corridors / training_rows /
bucket_avg_per_km) from the live `messages` table and writes it into
Postgres, then reloads the in-process pricing model so an admin-dashboard
upload takes effect immediately, without a redeploy.

This is the same "pay-per-km analysis" stage the standalone WhatsApp-report
pipeline does (run_report.py's run_km_analysis()/export_pricing_model()),
ported to read from the `messages` table instead of a pickle, and reusing
scripts/migrate_json_to_postgres.py's existing upsert functions (they only
need a plain `model_data` dict -- the same shape data/training_rows.json
already has -- so no rework was needed there).
"""
import statistics
from collections import defaultdict
from datetime import datetime

from app import db
from app.geocode import geocode, normalize_place_key, route_km
from scripts.migrate_json_to_postgres import migrate_corridors, migrate_training_data

# Same bounds run_report.py uses to exclude a mislabeled per-ton rate (too
# low) or a bulk-post's price mismatched to the wrong short-hop pair of
# names (too high, unless the amount itself is small -- a genuine short
# local trip can carry a flat minimum fee).
MIN_PLAUSIBLE_PER_KM = 8.0
MAX_PLAUSIBLE_PER_KM = 300.0
SMALL_FREIGHT_CEILING = 20000
MAX_KNOWN_CORRIDORS = 200


def _fetch_priced_messages(conn):
    return conn.execute(
        "SELECT route_origin, route_dest, freight, vehicle_type FROM messages "
        "WHERE route_origin IS NOT NULL AND route_dest IS NOT NULL "
        "AND freight IS NOT NULL AND vehicle_type IS NOT NULL"
    ).fetchall()


def rebuild():
    """Returns {"priced_quotes": n, "known_corridors": n}. No-ops (returns
    zeros) if there isn't yet enough data to build anything."""
    print("rebuild: fetching priced messages...", flush=True)
    with db.get_pool().connection() as conn:
        priced = _fetch_priced_messages(conn)
    print(f"rebuild: {len(priced)} priced messages", flush=True)

    if not priced:
        return {"priced_quotes": 0, "known_corridors": 0}

    # Only DISTINCT places/pairs go through the network-bound, per-key
    # DBCache adapter (one round-trip per lookup) -- bounded by how many
    # unique places/routes exist, not by how many historical messages there
    # are. Once resolved, everything downstream reads from plain local
    # dicts (bulk-loaded once below) so a loop over thousands of raw
    # messages never does a network round-trip per message.
    geo_cache = db.geocode_db_cache()
    dist_cache = db.distance_db_cache()
    pairs = sorted(set((o, d) for o, d, _, _ in priced))
    places = sorted({p for pair in pairs for p in pair})
    print(f"rebuild: resolving {len(places)} places, {len(pairs)} pairs (rate-limited)...", flush=True)
    for p in places:
        geocode(p, geo_cache)
    for o, d in pairs:
        route_km(o, d, geo_cache, dist_cache)
    print("rebuild: place/route resolution done", flush=True)

    with db.get_pool().connection() as conn:
        local_geo = {
            row[0]: ([row[1], row[2]] if row[3] else None)
            for row in conn.execute("SELECT place_key, lat, lon, resolved FROM geocode_cache").fetchall()
        }
        local_dist = {
            row[0]: row[1]
            for row in conn.execute("SELECT pair_key, distance_km FROM distance_cache").fetchall()
        }

    rows = []
    for o, d, freight, veh in priced:
        # Must match route_km()'s own key exactly (app/geocode.py) --
        # normalized (casefolded) place names, sorted, then joined.
        key = " | ".join(sorted([normalize_place_key(o), normalize_place_key(d)]))
        km = local_dist.get(key)
        if km is None or km < 5:
            continue
        per_km = round(float(freight) / km, 1)
        if per_km < MIN_PLAUSIBLE_PER_KM:
            continue
        if per_km > MAX_PLAUSIBLE_PER_KM and float(freight) > SMALL_FREIGHT_CEILING:
            continue
        rows.append({"o": o, "d": d, "km": km, "veh": veh, "freight": float(freight), "per_km": per_km})

    if not rows:
        return {"priced_quotes": 0, "known_corridors": 0}

    by_corridor = defaultdict(list)
    for r in rows:
        by_corridor[tuple(sorted([r["o"], r["d"]]))].append(r)
    known_corridors = []
    for (a, b), qs in by_corridor.items():
        freights = [q["freight"] for q in qs]
        per_kms = [q["per_km"] for q in qs]
        known_corridors.append({
            "a": a, "b": b, "n": len(qs),
            "median_per_km": round(statistics.median(per_kms), 1),
            "min_freight": min(freights), "max_freight": max(freights),
            "vehicles": sorted(set(q["veh"] for q in qs)),
        })
    known_corridors.sort(key=lambda x: -x["n"])
    known_corridors = known_corridors[:MAX_KNOWN_CORRIDORS]

    def bucket(km):
        return round(km / 50) * 50

    by_bucket = defaultdict(list)
    for r in rows:
        by_bucket[bucket(r["km"])].append(r)
    bucket_avg = {str(b): round(statistics.mean(x["per_km"] for x in v), 1) for b, v in by_bucket.items()}

    per_km_vals = [r["per_km"] for r in rows]
    overall_per_km = {
        "n": len(per_km_vals), "min": min(per_km_vals), "max": max(per_km_vals),
        "avg": round(statistics.mean(per_km_vals), 1), "median": round(statistics.median(per_km_vals), 1),
    }

    model_data = {
        "generated_at": datetime.now().isoformat(),
        "sample_size": len(rows),
        "overall_per_km": overall_per_km,
        "bucket_avg_per_km": bucket_avg,
        "known_corridors": known_corridors,
        "training_rows": rows,
        "min_plausible_per_km": MIN_PLAUSIBLE_PER_KM,
        "notes": "Rebuilt from live WhatsApp uploads via the admin dashboard.",
    }

    print(f"rebuild: writing {len(rows)} rows / {len(known_corridors)} corridors...", flush=True)
    with db.get_pool().connection() as conn:
        migrate_training_data(conn, model_data)
        migrate_corridors(conn, model_data, local_geo)  # already-resolved, plain dict -- no network calls
    print("rebuild: write done, reloading pricing model...", flush=True)

    import app.main as main_module
    main_module.pricing_model = main_module._build_pricing_model()
    print("rebuild: done", flush=True)

    return {"priced_quotes": len(rows), "known_corridors": len(known_corridors)}
