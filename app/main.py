import difflib
import json
import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.geocode import (
    GAZETTEER,
    geocode,
    geocode_or_suggest,
    load_cache,
    route_km,
    save_cache,
    search_suggestions,
)
from app.parser import parse_post
from app.pricing import PricingModel, suggest_vehicle_type

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")

with open(os.path.join(DATA_DIR, "training_rows.json")) as f:
    MODEL_DATA = json.load(f)
pricing_model = PricingModel(MODEL_DATA)

app = FastAPI(title="Kulue Rate Desk")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class QuoteRequest(BaseModel):
    text: str
    overrides: dict[str, str] | None = None


class FeedbackRequest(BaseModel):
    accurate: bool
    comment: str | None = None
    quote: dict


@app.get("/api/health")
def health():
    return {"ok": True, "training_rows": pricing_model.n_rows, "known_corridors": len(pricing_model.known_corridors)}


@app.get("/api/places")
def places(q: str = ""):
    q = q.strip()
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

    geo_cache = load_cache("geocode_cache.json", seed_name="geocode_cache_seed.json")
    dist_cache = load_cache("distance_cache.json", seed_name="distance_cache_seed.json")

    needs_confirmation = []
    for field in ("origin", "destination"):
        place = parsed[field]
        if field in overrides:
            # the poster already picked this spelling from a suggestion list --
            # resolve it with full effort and stop asking.
            if not geocode(place, geo_cache):
                save_cache("geocode_cache.json", geo_cache)
                raise HTTPException(422, f"Couldn't locate '{place}' on the map -- check the spelling.")
            continue
        result, suggestions = geocode_or_suggest(place, geo_cache)
        if result is None:
            if suggestions:
                needs_confirmation.append({"field": field, "raw": place, "suggestions": suggestions})
            else:
                save_cache("geocode_cache.json", geo_cache)
                raise HTTPException(422, f"Couldn't locate '{place}' on the map -- check the spelling.")

    save_cache("geocode_cache.json", geo_cache)
    if needs_confirmation:
        return {"needs_confirmation": needs_confirmation, "parsed": parsed}

    km = route_km(parsed["origin"], parsed["destination"], geo_cache, dist_cache)
    save_cache("distance_cache.json", dist_cache)
    if km is None:
        raise HTTPException(422, "Couldn't compute a driving route between these two places.")

    suggested_vehicle_type = None
    if not parsed.get("vehicle_type") and parsed.get("weight_tons"):
        suggested_vehicle_type = suggest_vehicle_type(parsed["weight_tons"])

    # No vehicle named -> price against the weight-appropriate vehicle's own
    # historical rate factor instead of a generic all-vehicle average, since
    # a 25-ton load actually runs on a 14-wheeler at 14-wheeler rates, not
    # some blend across mini-trucks and trailers alike.
    pricing_vehicle = parsed.get("vehicle_type") or suggested_vehicle_type
    pred = pricing_model.predict(parsed["origin"], parsed["destination"], km, pricing_vehicle)
    per_ton = round(pred["total"] / parsed["weight_tons"]) if parsed.get("weight_tons") else None

    return {
        "parsed": parsed,
        "distance_km": km,
        "rate_per_km": pred["rate_per_km"],
        "basis": pred["basis"],
        "suggested_vehicle_type": suggested_vehicle_type,
        "total_price_low": pred["total_low"],
        "total_price_high": pred["total_high"],
        "total_price_suggested": pred["total"],
        "price_per_ton": per_ton,
    }


FEEDBACK_PATH = os.path.join(DATA_DIR, "feedback.json")


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    """Stores a poster's accuracy rating (and optional comment/correction) on
    a quote they just got. Reviewed by hand and folded into
    manual_quotes.json (see the whatsapp-load-report pipeline's --add-quote)
    -- not auto-trained on directly, since a raw comment needs a human to
    turn it into a real route/vehicle/freight data point."""
    entries = []
    if os.path.exists(FEEDBACK_PATH):
        with open(FEEDBACK_PATH) as f:
            entries = json.load(f)
    entries.append({
        "at": datetime.now().isoformat(timespec="seconds"),
        "accurate": req.accurate,
        "comment": (req.comment or "").strip() or None,
        "quote": req.quote,
    })
    with open(FEEDBACK_PATH, "w") as f:
        json.dump(entries, f, indent=2)
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
