"""Postgres backend (Neon free tier) for the runtime geocode/distance caches,
feedback log, and trained pricing data -- replacing the fcntl-locked flat
JSON files, whose biggest problem wasn't concurrency (that was already fixed)
but durability: Render's free tier has no persistent disk, so those files
reset to their committed seed on every redeploy/restart, letting the same
place name re-resolve to a different coordinate over time.

Active only when DATABASE_URL is set (locally via .env, or on Render's
dashboard). Without it, app/geocode.py and app/main.py fall back to the
original JSON-file behavior unchanged -- this lets the app run today, before
Neon is provisioned, and pick up Postgres automatically the moment it is.
"""
import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

_pool = None


def enabled():
    return bool(DATABASE_URL)


def get_pool():
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        # autocommit -- nothing here needs multi-statement transactional
        # atomicity (every write is its own idempotent upsert), and without
        # it a connection can be left sitting "idle in transaction" after a
        # bare read if the caller doesn't explicitly commit, which can stall
        # later queries against the same rows.
        _pool = ConnectionPool(
            DATABASE_URL, min_size=1, max_size=5, open=True,
            kwargs={"autocommit": True},
        )
    return _pool


class DBCache:
    """Dict-like adapter over a two-column Postgres cache table, so
    geocode.py's existing dict-passing code (geocode(), geocode_or_suggest(),
    route_km() all take a plain `cache` dict and do `key in cache` /
    `cache[key]` / `cache[key] = value`) works completely unchanged whether
    the cache is an in-memory dict loaded from JSON or backed live by
    Postgres. Writes go through immediately (no separate save_cache step
    needed -- save_cache() becomes a no-op for this type)."""

    def __init__(self, table, key_col, value_cols, decode, encode):
        self.table = table
        self.key_col = key_col
        self.value_cols = value_cols
        self._decode = decode
        self._encode = encode
        # Every caller in this codebase does `if key in cache: return
        # cache[key]` (geocode(), route_km()) -- without this, that's two
        # round-trips per lookup. Each round-trip against Neon's pooled
        # endpoint costs ~1-1.5s (connection/proxy overhead, not query time),
        # so for a few hundred lookups this alone roughly doubles a rebuild's
        # runtime. One-entry memoization turns the immediately-following
        # __getitem__/get() after a __contains__ check into a free hit.
        self._last_key = None
        self._last_row = None

    def _fetch(self, key):
        if key == self._last_key:
            return self._last_row
        cols = ", ".join(self.value_cols)
        sql = f"SELECT {cols} FROM {self.table} WHERE {self.key_col} = %s"
        with get_pool().connection() as conn:
            row = conn.execute(sql, (key,)).fetchone()
        self._last_key = key
        self._last_row = row
        return row

    def __contains__(self, key):
        return self._fetch(key) is not None

    def get(self, key, default=None):
        row = self._fetch(key)
        if row is None:
            return default
        return self._decode(row)

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        cols = [self.key_col] + self.value_cols
        placeholders = ", ".join(["%s"] * len(cols))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in self.value_cols)
        sql = (
            f"INSERT INTO {self.table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({self.key_col}) DO UPDATE SET {updates}, updated_at = now()"
        )
        with get_pool().connection() as conn:
            conn.execute(sql, (key, *self._encode(value)))
        if key == self._last_key:
            self._last_key = None  # invalidate the memoized read

    def update(self, other):
        for k, v in other.items():
            self[k] = v


def geocode_db_cache():
    return DBCache(
        table="geocode_cache",
        key_col="place_key",
        value_cols=["lat", "lon", "resolved"],
        decode=lambda row: [row[0], row[1]] if row[2] else None,
        encode=lambda value: (value[0], value[1], True) if value else (None, None, False),
    )


def distance_db_cache():
    return DBCache(
        table="distance_cache",
        key_col="pair_key",
        value_cols=["distance_km"],
        decode=lambda row: row[0],
        encode=lambda value: (value,),
    )


def get_places_missing_state():
    """Resolved places in geocode_cache with no state yet -- used by the
    admin dashboard's "states with no data" stat and its one-time backfill
    script (kept separate from geocode_db_cache() above since that adapter's
    [lat, lon] value shape is relied on throughout app/geocode.py)."""
    with get_pool().connection() as conn:
        return conn.execute(
            "SELECT place_key, lat, lon FROM geocode_cache WHERE resolved AND state IS NULL"
        ).fetchall()


def set_place_state(place_key, state):
    with get_pool().connection() as conn:
        conn.execute("UPDATE geocode_cache SET state = %s WHERE place_key = %s", (state, place_key))


def get_covered_states():
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT state FROM geocode_cache WHERE state IS NOT NULL"
        ).fetchall()
    return sorted({r[0] for r in rows if r[0]})


def insert_feedback(accurate, comment, quote):
    import json
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO feedback (accurate, comment, quote) VALUES (%s, %s, %s)",
            (accurate, comment, json.dumps(quote)),
        )


def load_pricing_model_data():
    """Rebuilds the exact dict shape PricingModel.__init__ expects, sourced
    from Postgres instead of training_rows.json. Each stored training_rows
    row is expanded back into occurrence_count in-memory copies before
    handing off, so PricingModel's frequency-weighted statistics (resid_std,
    vehicle_factor medians) come out numerically identical to computing them
    over the original, undeduplicated row list."""
    with get_pool().connection() as conn:
        corridors = conn.execute(
            "SELECT origin_name, destination_name, origin_lat, origin_lon, "
            "destination_lat, destination_lon, n, median_per_km, min_freight, "
            "max_freight, p10_freight, p90_freight, vehicle_freight_stats, vehicles "
            "FROM corridors"
        ).fetchall()
        rows = conn.execute(
            "SELECT origin_raw, destination_raw, distance_km, vehicle_type, "
            "freight, per_km, occurrence_count FROM training_rows"
        ).fetchall()
        buckets = conn.execute(
            "SELECT distance_km_bucket, avg_per_km FROM bucket_avg_per_km"
        ).fetchall()
        run = conn.execute(
            "SELECT generated_at, sample_size, overall_per_km, min_plausible_per_km, notes "
            "FROM model_runs ORDER BY imported_at DESC LIMIT 1"
        ).fetchone()

    known_corridors = []
    for (o, d, o_lat, o_lon, d_lat, d_lon, n, median_per_km, min_f, max_f,
         p10_f, p90_f, vehicle_freight_stats, vehicles) in corridors:
        known_corridors.append({
            "a": o, "b": d,
            "a_coords": [o_lat, o_lon] if o_lat is not None else None,
            "b_coords": [d_lat, d_lon] if d_lat is not None else None,
            "n": n, "median_per_km": median_per_km,
            "min_freight": float(min_f) if min_f is not None else None,
            "max_freight": float(max_f) if max_f is not None else None,
            "p10_freight": float(p10_f) if p10_f is not None else None,
            "p90_freight": float(p90_f) if p90_f is not None else None,
            "vehicle_freight_stats": vehicle_freight_stats or {},
            "vehicles": vehicles,
        })

    training_rows = []
    for (o, d, km, veh, freight, per_km, count) in rows:
        training_rows.extend([{
            "o": o, "d": d, "km": km, "veh": veh,
            "freight": float(freight), "per_km": per_km,
        }] * count)

    return {
        "known_corridors": known_corridors,
        "bucket_avg_per_km": {str(b): v for b, v in buckets},
        "training_rows": training_rows,
        "generated_at": run[0].isoformat() if run and run[0] else None,
        "sample_size": run[1] if run else None,
        "overall_per_km": run[2] if run else None,
        "min_plausible_per_km": run[3] if run else None,
        "notes": run[4] if run else None,
    }
