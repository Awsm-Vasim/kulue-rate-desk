"""One-time: resolves the Indian state for every already-geocoded place
that doesn't have one cached yet (see the `state` column on geocode_cache,
scripts/init_db.sql), via Nominatim reverse-geocoding. Powers the admin
dashboard's "states with no data" stat.

Rate-limited to ~1/sec automatically (reverse_state() shares app/geocode.py's
existing Nominatim/OSRM throttle) -- for ~150-200 known places this takes a
few minutes; safe to re-run (only fetches what's still missing).

    python3 scripts/backfill_place_states.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db
from app.geocode import reverse_state


def main():
    if not db.enabled():
        print("DATABASE_URL is not set (check your .env) -- nothing to backfill.")
        sys.exit(1)

    places = db.get_places_missing_state()
    print(f"{len(places)} place(s) need a state resolved (rate-limited to ~1/sec)...")

    resolved, failed = 0, 0
    for place_key, lat, lon in places:
        state = reverse_state(lat, lon)
        if state:
            db.set_place_state(place_key, state)
            resolved += 1
        else:
            failed += 1
            print(f"  could not resolve state for {place_key!r} ({lat}, {lon})")

    print(f"\nDone: {resolved} resolved, {failed} failed.")
    print("Covered states:", ", ".join(db.get_covered_states()) or "(none)")


if __name__ == "__main__":
    main()
