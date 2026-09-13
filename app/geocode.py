"""Real geocoding (Nominatim) + driving distance (OSRM), disk-cached.

Same approach as the WhatsApp report pipeline: this gives an actual road
distance instead of an LLM's guess, which is the point of this service over
the earlier prototype. Caches persist to disk under DATA_DIR so repeat
corridors don't re-hit the network -- important because Nominatim's usage
policy caps unauthenticated use to ~1 req/sec.

Because there's no LLM in the loop to paper over typos or disambiguate a
compound "locality, city" post like "Begur, Mysuru", geocode() tries a few
deterministic strategies before giving up:
  1. the raw string as typed;
  2. fuzzy-corrected against a ~150-place gazetteer of towns that actually
     appear in Kulue's WhatsApp corridor data (fixes "Udumulpettai" ->
     "Udumalpet"-style typos);
  3. for a comma-separated "local, anchor" string, geocoding the anchor
     first and then searching the local part bounded to a box around it
     (helps when the local part alone is ambiguous).
If none resolve, it returns None and the caller should ask the poster for a
better-known nearby place name -- guessing wrong here silently mis-prices a
trip, which is worse than asking.
"""
import difflib
import fcntl
import json
import os
import ssl
import time
import urllib.parse
import urllib.request

import certifi

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
UA = "kulue-rate-desk/1.0 (internal tool; contact: ops@kulue.example)"
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_MIN_INTERVAL = 1.0  # Nominatim usage policy: max ~1 req/sec
_last_call = [0.0]


def _throttle():
    wait = _MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def _get_json(url, timeout=10):
    _throttle()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cache_path(name):
    return os.path.join(DATA_DIR, name)


def load_cache(name, seed_name=None):
    path = _cache_path(name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    if seed_name:
        seed_path = _cache_path(seed_name)
        if os.path.exists(seed_path):
            with open(seed_path) as f:
                return json.load(f)
    return {}


def save_cache(name, data):
    """Merges `data` into whatever is currently on disk, under an exclusive
    file lock, instead of blindly overwriting the file with this request's
    in-memory snapshot.

    Without this, two concurrent requests each load the cache, add their own
    new place, and save -- the second save's whole-file write clobbers the
    first request's new entry. Locking the read-merge-write cycle (and
    merging rather than replacing) means concurrent requests each contribute
    their new keys instead of racing to overwrite one another."""
    path = _cache_path(name)
    lock_path = path + ".lock"
    with open(lock_path, "a+") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            current = {}
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        current = json.load(f)
                except (json.JSONDecodeError, OSError):
                    current = {}
            current.update(data)
            tmp_path = path + f".tmp.{os.getpid()}"
            with open(tmp_path, "w") as f:
                json.dump(current, f)
            os.replace(tmp_path, path)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def _load_gazetteer():
    path = _cache_path("gazetteer.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


GAZETTEER = _load_gazetteer()


def _fuzzy_correct(name, cutoff=0.74):
    if not name or not GAZETTEER:
        return name
    matches = difflib.get_close_matches(name, GAZETTEER, n=1, cutoff=cutoff)
    return matches[0] if matches else name


def _search(q, viewbox=None):
    params = {"q": q, "format": "json", "limit": 1}
    if viewbox:
        params["viewbox"] = viewbox
        params["bounded"] = 1
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
        return [float(data[0]["lat"]), float(data[0]["lon"])] if data else None
    except Exception:
        return None


def _viewbox_around(lat, lon, deg=0.6):
    return f"{lon - deg},{lat + deg},{lon + deg},{lat - deg}"


def geocode(place, cache):
    """Resolves a place name to [lat, lon], trying a few strategies. Cached by
    the exact raw string so repeat lookups (even ones that needed fallback
    strategies) are free after the first."""
    if place in cache:
        return cache[place]
    result = _resolve(place, cache)
    cache[place] = result
    return result


def _resolve(place, cache):
    # 1. exact string as typed
    result = _search(f"{place}, India")
    if result:
        return result

    # 2. whole-string fuzzy correction (fixes a misspelled single town name,
    #    e.g. "Udumulpettai" -> "Udumalpet")
    corrected = _fuzzy_correct(place)
    if corrected != place:
        result = _search(f"{corrected}, India")
        if result:
            return result

    # 3. "local, anchor" compound (e.g. "Begur, Mysuru"): resolve the anchor
    #    first and search the local part BOUNDED to a box around it -- this
    #    must take priority over a bare unbounded local search, which can
    #    silently return a same-named place in a totally different state.
    if "," in place:
        parts = [p.strip() for p in place.split(",") if p.strip()]
        if len(parts) >= 2:
            local = ",".join(parts[:-1])
            anchor = parts[-1]
            anchor_coords = geocode(anchor, cache) or geocode(_fuzzy_correct(anchor), cache)
            if anchor_coords:
                vb = _viewbox_around(*anchor_coords)
                local_corrected = _fuzzy_correct(local)
                result = _search(local, viewbox=vb) or _search(local_corrected, viewbox=vb)
                if result:
                    return result
            else:
                # no anchor to bound against -- an unbounded guess is the
                # best that's left, accepted only in this fallback case
                local_corrected = _fuzzy_correct(local)
                result = _search(f"{local}, India") or _search(f"{local_corrected}, India")
                if result:
                    return result
    return None


INDIA_BBOX = "68,6,98,38"  # lon_min,lat_min,lon_max,lat_max -- rough India extent


def search_suggestions(q, limit=8):
    """Live place-name suggestions for autocomplete, as the poster is still
    typing.

    Nominatim's /search (used elsewhere in this file for final geocoding)
    only matches *complete* words -- "perinthalm" finds nothing even though
    "Perinthalmanna" exists, which makes it useless for suggest-as-you-type.
    Photon (also free, also OSM-data-backed, run by Komoot) does real
    prefix/fragment matching, so it's used here instead, restricted to a
    rough India bounding box. Returns [] on any network hiccup -- caller
    should fall back to the local gazetteer.

    Each label is formatted as 'Locality, District, State' so same-named
    places in different states/districts (there are several in India) don't
    look identical in the dropdown.
    """
    params = {"q": q, "limit": limit, "lang": "en", "bbox": INDIA_BBOX}
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url)
    except Exception:
        return []

    seen = set()
    out = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        if p.get("countrycode") != "IN" or p.get("osm_key") != "place":
            continue
        name = p.get("name")
        if not name:
            continue
        parts = [name]
        for extra in (p.get("county") or p.get("state_district"), p.get("state")):
            if extra and extra not in parts:
                parts.append(extra)
        label = ", ".join(parts)
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def _typo_candidates(place, n=4):
    seen = set()
    out = []

    def add(name):
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)

    add(_fuzzy_correct(place))
    for m in difflib.get_close_matches(place, GAZETTEER, n=n, cutoff=0.6):
        add(m)

    if "," in place:
        local = ",".join(p.strip() for p in place.split(",")[:-1] if p.strip())
        if local:
            add(_fuzzy_correct(local))
            for m in difflib.get_close_matches(local, GAZETTEER, n=n, cutoff=0.6):
                add(m)

    return out[:n]


def geocode_or_suggest(place, cache):
    """Like geocode(), but never silently accepts a fuzzy-corrected spelling.

    Returns (coords, suggestions):
      - coords set, suggestions None  -> resolved with confidence, use it.
      - coords None, suggestions [..] -> looks like a typo; ask the poster
        to pick one of these known place names instead of guessing.
      - coords None, suggestions []   -> genuinely couldn't locate it.
    """
    if place in cache:
        return cache[place], None

    result = _search(f"{place}, India")
    if result:
        cache[place] = result
        return result, None

    # "local, anchor" compound (e.g. "Begur, Mysuru") -- resolving the local
    # part bounded to its anchor is a locality lookup, not a spelling fix.
    if "," in place:
        parts = [p.strip() for p in place.split(",") if p.strip()]
        if len(parts) >= 2:
            local = ",".join(parts[:-1])
            anchor = parts[-1]
            anchor_coords = _search(f"{anchor}, India")
            if anchor_coords:
                result = _search(local, viewbox=_viewbox_around(*anchor_coords))
                if result:
                    cache[place] = result
                    return result, None

    return None, _typo_candidates(place)


def route_km(origin, destination, geo_cache, dist_cache):
    key = " | ".join(sorted([origin, destination]))
    if key in dist_cache:
        return dist_cache[key]
    go, gd = geo_cache.get(origin), geo_cache.get(destination)
    if not go or not gd:
        dist_cache[key] = None
        return None
    lat1, lon1 = go
    lat2, lon2 = gd
    url = f"https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
    try:
        data = _get_json(url)
        dist_cache[key] = round(data["routes"][0]["distance"] / 1000, 1) if data.get("code") == "Ok" else None
    except Exception:
        dist_cache[key] = None
    return dist_cache[key]
