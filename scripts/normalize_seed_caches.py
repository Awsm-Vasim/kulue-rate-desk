"""Re-keys the committed geocode/distance cache seed files to match
app/geocode.py's normalize_place_key() lookup scheme.

app/geocode.py's geocode()/geocode_or_suggest()/route_km() were changed to
look up cache entries by a normalized (casefolded, whitespace-collapsed) key
-- necessary so 'Aluva', 'aluva ', and an autocomplete-picked
'Aluva, Ernakulam, Kerala' all hit the same cache entry instead of each
triggering a fresh network lookup. But the committed seed files
(data/geocode_cache_seed.json, data/distance_cache_seed.json) still had their
original un-normalized keys, so EVERY entry in both seed files was silently
missing under the new lookup -- meaning a fresh instance (Render, which has
no persistent disk yet and resets its runtime cache on every restart) would
re-geocode/re-route every place from scratch instead of using the vetted
seed values, which is slow and can resolve to a slightly different distance
than the one the seed was built and verified against.

Run once (re-running is safe/idempotent -- it just re-derives the same keys):

    python3 scripts/normalize_seed_caches.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.data_cleaning import clean_cache_seed, normalize_dist_cache, normalize_geo_cache

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _rewrite(name, new_data):
    path = os.path.join(DATA_DIR, name)
    with open(path, "w") as f:
        json.dump(new_data, f, indent=2, sort_keys=True)


def main():
    geo_path = os.path.join(DATA_DIR, "geocode_cache_seed.json")
    with open(geo_path) as f:
        geo_raw = json.load(f)
    geo_normalized = normalize_geo_cache(geo_raw)
    print(f"geocode_cache_seed.json: {len(geo_raw)} entries -> {len(geo_normalized)} after normalizing "
          f"({len(geo_raw) - len(geo_normalized)} key collisions merged)")
    _rewrite("geocode_cache_seed.json", geo_normalized)

    dist_path = os.path.join(DATA_DIR, "distance_cache_seed.json")
    with open(dist_path) as f:
        dist_raw = json.load(f)
    dist_normalized = normalize_dist_cache(dist_raw)
    dist_cleaned, dropped = clean_cache_seed(dist_normalized)
    print(f"distance_cache_seed.json: {len(dist_raw)} entries -> {len(dist_cleaned)} after normalizing "
          f"and dropping {len(dropped)} oversized/corrupted key(s)")
    _rewrite("distance_cache_seed.json", dist_cleaned)

    print("\nDone. Both seed files now use the normalized key format app/geocode.py expects.")


if __name__ == "__main__":
    main()
