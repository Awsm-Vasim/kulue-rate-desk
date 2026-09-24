"""Standalone check: proves dedupe_training_rows()/expand_rows() are lossless
(the deduped+weighted rows reproduce PricingModel's exact current statistics)
before this logic is trusted inside the Postgres migration. Run with:

    python3 scripts/verify_cleaning.py
"""
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.data_cleaning import (
    clean_cache_seed,
    dedupe_training_rows,
    expand_rows,
    flag_thin_vehicle_classes,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def resid_and_vehicle_stats(rows, buckets):
    def bucket_rate(km):
        if km <= buckets[0][0]:
            return buckets[0][1]
        if km >= buckets[-1][0]:
            return buckets[-1][1]
        for (k0, v0), (k1, v1) in zip(buckets, buckets[1:]):
            if k0 <= km <= k1:
                if k1 == k0:
                    return v0
                t = (km - k0) / (k1 - k0)
                return v0 + t * (v1 - v0)
        return buckets[-1][1]

    residuals = []
    by_vehicle = defaultdict(list)
    for r in rows:
        baseline = bucket_rate(r["km"])
        residuals.append(math.log(r["per_km"] / baseline))
        by_vehicle[r["veh"]].append(r["per_km"] / baseline)
    resid_std = (sum(x * x for x in residuals) / len(residuals)) ** 0.5 if residuals else 0.4

    vehicle_factor = {}
    for veh, ratios in by_vehicle.items():
        if len(ratios) >= 3:
            ratios.sort()
            vehicle_factor[veh] = ratios[len(ratios) // 2]
    return resid_std, vehicle_factor


def main():
    with open(os.path.join(DATA_DIR, "training_rows.json")) as f:
        model_data = json.load(f)

    raw_rows = [r for r in model_data["training_rows"] if r.get("km", 0) > 0]
    buckets = sorted((float(k), v) for k, v in model_data["bucket_avg_per_km"].items())

    unique_rows, total_before, groups_with_dupes = dedupe_training_rows(raw_rows)
    print(f"raw rows:              {total_before}")
    print(f"unique rows:           {len(unique_rows)}")
    print(f"duplicate groups (n>1): {groups_with_dupes}")
    print(f"sum(occurrence_count): {sum(r['occurrence_count'] for r in unique_rows)}")

    reexpanded = expand_rows(unique_rows)
    assert len(reexpanded) == total_before, "expand_rows() did not reproduce the original row count"

    resid_before, veh_before = resid_and_vehicle_stats(raw_rows, buckets)
    resid_after, veh_after = resid_and_vehicle_stats(reexpanded, buckets)
    print(f"\nresid_std before: {resid_before!r}")
    print(f"resid_std after:  {resid_after!r}")
    assert resid_before == resid_after, "resid_std changed after dedupe+re-expand!"

    assert veh_before.keys() == veh_after.keys(), "vehicle_factor keys changed!"
    for veh in veh_before:
        assert veh_before[veh] == veh_after[veh], f"vehicle_factor[{veh}] changed!"
    print("vehicle_factor: identical for all", len(veh_before), "vehicle types")

    print("\nthin vehicle classes (n < 3 occurrences):")
    thin_flags = flag_thin_vehicle_classes(unique_rows)
    for veh, info in sorted(thin_flags.items(), key=lambda kv: kv[1]["n"]):
        if info["sample_quality"] == "thin":
            print(f"  {veh!r}: n={info['n']}")

    print("\ncache seed cleanup:")
    for name in ("geocode_cache_seed.json", "distance_cache_seed.json"):
        with open(os.path.join(DATA_DIR, name)) as f:
            raw_cache = json.load(f)
        cleaned, dropped = clean_cache_seed(raw_cache)
        print(f"  {name}: {len(raw_cache)} entries, dropped {len(dropped)} oversized key(s)")
        for k in dropped:
            print(f"    dropped key (len={len(k)}): {k[:60]}...")

    print("\nOK: dedupe+re-expand is lossless, safe to use in the migration script.")


if __name__ == "__main__":
    main()
