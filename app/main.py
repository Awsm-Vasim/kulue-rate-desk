import difflib
import fcntl
import json
import os
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.geocode import (
    GAZETTEER,
    geocode,
    geocode_or_suggest,
    get_cached_coords,
    load_cache,
    normalize_place_key,
    route_km,
    save_cache,
    search_suggestions,
)
from app.parser import MATERIALS, parse_post
from app.pricing import PricingModel, assess_offered_price, suggest_vehicle_type

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")


def _build_pricing_model():
    """Loads the trained model data and resolves each known corridor's
    endpoints to coordinates -- PricingModel matches corridors by resolved
    coordinates, not by the raw place-name string a device happened to send,
    so 'Aluva' and an autocomplete-picked 'Aluva, Ernakulam, Kerala' hit the
    same corridor. A corridor whose endpoints fail to geocode is silently
    dropped from exact-corridor matching (still contributes to the generic
    distance curve via training_rows)."""
    with open(os.path.join(DATA_DIR, "training_rows.json")) as f:
        model_data = json.load(f)

    geo_cache = load_cache("geocode_cache.json", seed_name="geocode_cache_seed.json")
    for corridor in model_data.get("known_corridors", []):
        corridor["a_coords"] = geocode(corridor["a"], geo_cache)
        corridor["b_coords"] = geocode(corridor["b"], geo_cache)
    save_cache("geocode_cache.json", geo_cache)

    return PricingModel(model_data)


pricing_model = _build_pricing_model()

app = FastAPI(title="Kulue Rate Calculator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """This app ships as a single-page static bundle that gets redeployed
    often -- without this, a phone's browser can keep serving yesterday's
    cached index.html indefinitely, so two devices can show genuinely
    different prices simply because one of them is running stale client
    code against the current API, not because the pricing itself differs."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# A WhatsApp load post is never anywhere near this long -- capping input
# length keeps a stray paste (or a deliberately huge request) from being
# forwarded on to Nominatim/OSRM/Photon at full size.
_ShortStr = Annotated[str, Field(max_length=200)]


class QuoteRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    overrides: dict[str, _ShortStr] | None = None
    offered_price: float | None = Field(default=None, gt=0)
    offered_unit: Literal["total", "per_ton"] = "total"


class FeedbackRequest(BaseModel):
    accurate: bool
    comment: str | None = Field(default=None, max_length=2000)
    quote: dict


@app.get("/api/health")
def health():
    return {"ok": True, "training_rows": pricing_model.n_rows, "known_corridors": len(pricing_model.known_corridors)}


@app.get("/api/places")
def places(q: str = ""):
    q = q.strip()[:200]
    if len(q) < 3:
        return {"places": []}

    results = search_suggestions(q, limit=8)
    if results:
        return {"places": results}

    # Nominatim unreachable/no hits -- fall back to the local corridor
    # gazetteer so the field still offers something while typing.
    ql = q.lower()
    starts = [p for p in GAZETTEER if p.lower().startswith(ql)]
    contains = [p for p in GAZETTEER if ql in p.lower() and p not in starts]
    results = starts + contains
    if len(results) < 5:
        for f in difflib.get_close_matches(q, GAZETTEER, n=8, cutoff=0.6):
            if f not in results:
                results.append(f)
    return {"places": results[:8]}


@app.get("/api/materials")
def materials(q: str = ""):
    """Same response shape as /api/places ("places" key) so the frontend
    can reuse one autocomplete implementation for both fields -- this is a
    small local list, not an external geocoder, so it's cheap to return
    matches (or the whole list) even for a short/empty query."""
    q = q.strip()[:200].lower()
    if not q:
        return {"places": MATERIALS[:8]}
    starts = [m for m in MATERIALS if m.lower().startswith(q)]
    contains = [m for m in MATERIALS if q in m.lower() and m not in starts]
    return {"places": (starts + contains)[:8]}


@app.get("/api/corridors")
def corridors():
    top = sorted(pricing_model.known_corridors, key=lambda c: -c["n"])[:10]
    return {"corridors": top}


@app.post("/api/quote")
def quote(req: QuoteRequest):
    parsed = parse_post(req.text)
    if not parsed["origin"] or not parsed["destination"]:
        raise HTTPException(400, "Couldn't find both a 'From' and a 'To' place in the pasted text.")

    overrides = req.overrides or {}
    parsed["origin"] = overrides.get("origin") or parsed["origin"]
    parsed["destination"] = overrides.get("destination") or parsed["destination"]

    if normalize_place_key(parsed["origin"]) == normalize_place_key(parsed["destination"]):
        raise HTTPException(400, "Origin and destination can't be the same place.")

    geo_cache = load_cache("geocode_cache.json", seed_name="geocode_cache_seed.json")
    dist_cache = load_cache("distance_cache.json", seed_name="distance_cache_seed.json")

    needs_confirmation = []
    unresolvable = False
    for field in ("origin", "destination"):
        place = parsed[field]
        if field in overrides:
            # the poster already picked this spelling from a suggestion list --
            # resolve it with full effort and stop asking.
            if not geocode(place, geo_cache):
                unresolvable = True
            continue
        result, suggestions = geocode_or_suggest(place, geo_cache)
        if result is None:
            if suggestions:
                needs_confirmation.append({"field": field, "raw": place, "suggestions": suggestions})
            else:
                unresolvable = True

    save_cache("geocode_cache.json", geo_cache)
    if needs_confirmation:
        return {"needs_confirmation": needs_confirmation, "parsed": parsed}

    km = None
    if not unresolvable:
        km = route_km(parsed["origin"], parsed["destination"], geo_cache, dist_cache)
        save_cache("distance_cache.json", dist_cache)

    suggested_vehicle_type = None
    if not parsed.get("vehicle_type") and parsed.get("weight_tons"):
        suggested_vehicle_type = suggest_vehicle_type(parsed["weight_tons"])

    # No vehicle named -> price against the weight-appropriate vehicle's own
    # historical rate factor instead of a generic all-vehicle average, since
    # a 25-ton load actually runs on a 14-wheeler at 14-wheeler rates, not
    # some blend across mini-trucks and trailers alike.
    pricing_vehicle = parsed.get("vehicle_type") or suggested_vehicle_type

    # A place that can't be pinpointed on the map (or two valid places with
    # no drivable route between them) used to be a hard error with zero
    # price. Instead, fall back to a rough market-average estimate -- an
    # honestly-labeled low-confidence number beats forcing the poster to give
    # up entirely just because a small village isn't on the map.
    if km is None:
        pred = pricing_model.predict_without_distance(pricing_vehicle)
    else:
        # Matched by resolved coordinates, not the raw origin/destination
        # strings -- route_km() above already required both to be in
        # geo_cache, so these lookups are guaranteed to hit.
        origin_coords = get_cached_coords(parsed["origin"], geo_cache)
        destination_coords = get_cached_coords(parsed["destination"], geo_cache)
        pred = pricing_model.predict(origin_coords, destination_coords, km, pricing_vehicle)
    per_ton = round(pred["total"] / parsed["weight_tons"]) if parsed.get("weight_tons") else None

    offered_price_check = None
    if req.offered_price is not None:
        if req.offered_unit == "per_ton" and not parsed.get("weight_tons"):
            raise HTTPException(400, "Enter a weight to check a per-ton offered price.")
        offered_price_check = assess_offered_price(
            req.offered_price, req.offered_unit, pred, parsed.get("weight_tons"), km
        )

    return {
        "parsed": parsed,
        "distance_km": km,
        "rate_per_km": pred["rate_per_km"],
        "basis": pred["basis"],
        "confidence": pred["confidence"],
        "confidence_reason": pred["confidence_reason"],
        "suggested_vehicle_type": suggested_vehicle_type,
        "total_price_low": pred["total_low"],
        "total_price_high": pred["total_high"],
        "total_price_suggested": pred["total"],
        "price_per_ton": per_ton,
        "offered_price_check": offered_price_check,
    }


FEEDBACK_PATH = os.path.join(DATA_DIR, "feedback.json")


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    """Stores a poster's accuracy rating (and optional comment/correction) on
    a quote they just got. Reviewed by hand and folded into
    manual_quotes.json (see the whatsapp-load-report pipeline's --add-quote)
    -- not auto-trained on directly, since a raw comment needs a human to
    turn it into a real route/vehicle/freight data point."""
    lock_path = FEEDBACK_PATH + ".lock"
    with open(lock_path, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            entries = []
            if os.path.exists(FEEDBACK_PATH):
                try:
                    with open(FEEDBACK_PATH) as f:
                        entries = json.load(f)
                except (json.JSONDecodeError, OSError):
                    entries = []
            entries.append({
                "at": datetime.now().isoformat(timespec="seconds"),
                "accurate": req.accurate,
                "comment": (req.comment or "").strip() or None,
                "quote": req.quote,
            })
            tmp_path = FEEDBACK_PATH + f".tmp.{os.getpid()}"
            with open(tmp_path, "w") as f:
                json.dump(entries, f, indent=2)
            os.replace(tmp_path, FEEDBACK_PATH)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
