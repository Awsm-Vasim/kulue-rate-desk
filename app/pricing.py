"""Trained freight-rate model.

Baseline is the empirical ₹/km-by-distance-bucket curve from Kulue's
accumulated WhatsApp corridor data (linearly interpolated between bucket
midpoints) -- short hauls cost far more per km than long-haul routes, and
this non-parametric curve tracks that decay-then-plateau shape more
faithfully across a 14km-1200km range than a single power-law fit would
(a global log-log regression over-extrapolates on the long-haul tail).
Vehicle types with enough samples get a multiplicative adjustment measured
against that same curve. An exact (or near-exact) known corridor match
overrides everything with its own historical median, since real quotes
beat any interpolation.

Retrain by re-running the WhatsApp report pipeline and copying its refreshed
state/pricing_model.json over data/training_rows.json in this repo.
"""
import math
import re
from collections import defaultdict

MIN_CORRIDOR_N = 2
MIN_VEHICLE_SAMPLES = 3


def _normalize_vehicle(s):
    """Collapses cosmetic variation ('12 Wheels', '12-wheel', '12  wheel')
    down to the same key, so a poster's exact spelling/pluralization doesn't
    silently flip whether the vehicle-rate adjustment gets applied -- that
    looked like the price randomly changing for what was meant as the same
    vehicle."""
    s = re.sub(r'[-_]+', ' ', s.strip().lower())
    s = re.sub(r'\s+', ' ', s)
    s = s.replace("wheeler", "wheel")
    s = re.sub(r'\bfeet\b', 'ft', s)
    s = re.sub(r'\b(wheel|ft|tyre|tire)s\b', r'\1', s)
    return s

# Typical payload capacity (tons) for the vehicle classes that actually show
# up in Kulue's WhatsApp corridor data. training_rows.json has no capacity
# field (only rate-per-km), so these are industry rule-of-thumb figures for
# Indian commercial trucks, used only to *suggest* a vehicle when a poster
# didn't name one -- not part of the trained price itself.
VEHICLE_CAPACITY_TONS = [
    ("9 ft", 3),
    ("6 wheel", 9),
    ("17 ft", 9),
    ("18 ft", 10),
    ("19 ft", 11),
    ("20 ft", 15),
    ("10 wheel", 16),
    ("22 ft", 18),
    ("12 wheel", 21),
    ("container", 24),
    ("14 wheel", 27),
]


def suggest_vehicle_type(weight_tons):
    """Smallest known vehicle class that can carry weight_tons, or the
    largest known class if the load is heavier than all of them."""
    if not weight_tons or weight_tons <= 0:
        return None
    for name, capacity in VEHICLE_CAPACITY_TONS:
        if capacity >= weight_tons:
            return name
    return VEHICLE_CAPACITY_TONS[-1][0]


class PricingModel:
    def __init__(self, model_data):
        self.known_corridors = model_data.get("known_corridors", [])
        self._known_by_key = {self._key(c["a"], c["b"]): c for c in self.known_corridors}

        self.buckets = sorted(
            (float(k), v) for k, v in model_data.get("bucket_avg_per_km", {}).items()
        )
        if not self.buckets:
            self.buckets = [(0.0, 60.0)]

        rows = [r for r in model_data.get("training_rows", []) if r.get("km", 0) > 0]
        self.n_rows = len(rows)

        residuals = []
        by_vehicle = defaultdict(list)
        for r in rows:
            baseline = self._bucket_rate(r["km"])
            residuals.append(math.log(r["per_km"] / baseline))
            by_vehicle[r["veh"]].append(r["per_km"] / baseline)
        self.resid_std = (sum(x * x for x in residuals) / len(residuals)) ** 0.5 if residuals else 0.4

        self.vehicle_factor = {}
        for veh, ratios in by_vehicle.items():
            if len(ratios) >= MIN_VEHICLE_SAMPLES:
                ratios.sort()
                self.vehicle_factor[veh] = ratios[len(ratios) // 2]

        # normalized-key -> (canonical name, factor), so lookups tolerate
        # spelling/format variation without conflating genuinely different
        # vehicle classes (a "12" vs a "14" wheeler stay distinct).
        self._vehicle_factor_norm = {
            _normalize_vehicle(veh): (veh, factor) for veh, factor in self.vehicle_factor.items()
        }

    @staticmethod
    def _key(a, b):
        return tuple(sorted([a.strip().lower(), b.strip().lower()]))

    def _bucket_rate(self, km):
        buckets = self.buckets
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

    def predict(self, origin, destination, distance_km, vehicle_type=None):
        corridor = self._known_by_key.get(self._key(origin, destination))
        if corridor and corridor["n"] >= MIN_CORRIDOR_N:
            rate = corridor["median_per_km"]
            total = round(distance_km * rate)
            return {
                "rate_per_km": rate,
                "total": total,
                "total_low": min(corridor["min_freight"], total),
                "total_high": max(corridor["max_freight"], total),
                "basis": (f"Matched known corridor {corridor['a']} ↔ {corridor['b']} "
                          f"({corridor['n']} historical WhatsApp quotes, median ₹{rate}/km)."),
            }

        base_rate = self._bucket_rate(distance_km)
        factor = 1.0
        veh_note = ""
        if vehicle_type:
            match = self._vehicle_factor_norm.get(_normalize_vehicle(vehicle_type))
            if match:
                canonical, factor = match
                veh_note = f", adjusted for {canonical} vehicles"
        rate = base_rate * factor
        total = distance_km * rate
        low = total * math.exp(-self.resid_std)
        high = total * math.exp(self.resid_std)
        return {
            "rate_per_km": round(rate, 1),
            "total": round(total),
            "total_low": round(low),
            "total_high": round(high),
            "basis": (f"Distance-based model fit on {self.n_rows} historical quotes "
                      f"({round(distance_km)} km){veh_note}."),
        }
