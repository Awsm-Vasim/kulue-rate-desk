"""Cleans data/training_rows.json's raw rows into a form safe to store once
(deduplicated) while still reproducing today's exact pricing statistics.

training_rows.json is heavily duplicated (~85% of rows are exact repeats of
another row) -- PricingModel currently treats every raw row as one frequency
sample, so collapsing duplicates without preserving how often each one
occurred would silently change resid_std/vehicle-factor medians. Instead this
keeps one row per unique (o, d, km, veh, freight, per_km) tuple plus an
occurrence_count, and expand_rows() below turns that back into the original
per-sample list so PricingModel's statistics are unaffected.

Used standalone (verify_cleaning.py) against the current JSON file, and later
reused as the import stage of scripts/migrate_json_to_postgres.py.
"""
from collections import Counter

MIN_VEHICLE_SAMPLES = 3
MAX_CACHE_KEY_LEN = 200  # matches app/main.py's _ShortStr override cap


def dedupe_training_rows(raw_rows):
    """Returns (unique_rows_with_count, total_before, groups_with_dupes)."""
    counts = Counter()
    order = []
    for r in raw_rows:
        key = (r["o"], r["d"], r["km"], r["veh"], r["freight"], r["per_km"])
        if key not in counts:
            order.append(key)
        counts[key] += 1

    unique_rows = []
    groups_with_dupes = 0
    for key in order:
        o, d, km, veh, freight, per_km = key
        count = counts[key]
        if count > 1:
            groups_with_dupes += 1
        unique_rows.append({
            "o": o, "d": d, "km": km, "veh": veh, "freight": freight,
            "per_km": per_km, "occurrence_count": count,
        })
    return unique_rows, len(raw_rows), groups_with_dupes


def expand_rows(unique_rows):
    """Inverse of dedupe: re-materializes occurrence_count copies of each row
    so downstream frequency-weighted statistics (PricingModel's resid_std,
    vehicle_factor medians) are computed exactly as they are today."""
    expanded = []
    for r in unique_rows:
        copy = {k: v for k, v in r.items() if k != "occurrence_count"}
        expanded.extend([copy] * r["occurrence_count"])
    return expanded


def flag_thin_vehicle_classes(unique_rows):
    """Returns {vehicle_type: {"n": int, "sample_quality": "ok"|"thin"}},
    counting occurrences (not just distinct rows) since that's what
    MIN_VEHICLE_SAMPLES gates in pricing.py."""
    n_by_vehicle = Counter()
    for r in unique_rows:
        n_by_vehicle[r["veh"]] += r["occurrence_count"]
    return {
        veh: {"n": n, "sample_quality": "ok" if n >= MIN_VEHICLE_SAMPLES else "thin"}
        for veh, n in n_by_vehicle.items()
    }


def clean_cache_seed(cache_dict):
    """Drops any key longer than MAX_CACHE_KEY_LEN (the one known-corrupted
    entry in distance_cache_seed.json is ~5000 chars). Returns
    (cleaned_dict, dropped_keys)."""
    cleaned = {}
    dropped = []
    for k, v in cache_dict.items():
        if len(k) > MAX_CACHE_KEY_LEN:
            dropped.append(k)
            continue
        cleaned[k] = v
    return cleaned, dropped
