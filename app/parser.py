"""Parses a WhatsApp-style load post into structured fields.

Handles fully labeled posts (emoji prefixes and punctuation are tolerated):
    From: Begur, mysuru
    To: Udumulpettai
    Date: 11/09/2026 Friday
    Vehicle Type: 12 wheel
    Material: feed
    Weight: 25ton

...and also falls back to free-text extraction when the sender only typed
the two locations and a weight, e.g. "Begur to Udumulpettai, 25 ton" or two
bare lines "Begur, mysuru" / "Udumulpettai" plus a "25 ton" line.
"""
import difflib
import json
import os
import re

_MATERIALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "materials.json"
)


def _load_materials():
    if os.path.exists(_MATERIALS_PATH):
        with open(_MATERIALS_PATH) as f:
            return json.load(f)
    return []


MATERIALS = _load_materials()

FIELD_PATTERNS = {
    "origin": re.compile(r'\bFrom\s*[:\-]\s*(.+)', re.IGNORECASE),
    "destination": re.compile(r'\bTo\s*[:\-]\s*(.+)', re.IGNORECASE),
    "date": re.compile(r'\bDate\s*[:\-]\s*(.+)', re.IGNORECASE),
    "vehicle_type": re.compile(r'\bVehicle\s*Type\s*[:\-]\s*(.+)', re.IGNORECASE),
    "material": re.compile(r'\bMaterial\s*[:\-]\s*(.+)', re.IGNORECASE),
    "weight_raw": re.compile(r'\bWeight\s*[:\-]\s*(.+)', re.IGNORECASE),
    "loading_unloading": re.compile(r'\bLoading\s*(?:&|and)?\s*Unloading\s*[:\-]\s*(.*)', re.IGNORECASE),
}

WEIGHT_RE = re.compile(r'([\d.]+)\s*(ton|tonne|tonnes|tons|t\b|kg)', re.IGNORECASE)
WEIGHT_ONLY_RE = re.compile(r'^[\d.]+\s*(ton|tonne|tonnes|tons|t|kg)s?\.?$', re.IGNORECASE)
WEIGHT_SUFFIX_RE = re.compile(
    r'^(.*?)[\s,]+[\d.]+\s*(ton|tonne|tonnes|tons|t|kg)s?\.?\s*$', re.IGNORECASE
)
ROUTE_TO_RE = re.compile(r'^(.*?)\s+to\s+(.*)$', re.IGNORECASE)
ROUTE_DELIM_RE = re.compile(r'\s*(?:->|–|—|-|,|/)\s*')


def _clean(v):
    return v.strip().strip(',').strip()


def _is_weight_only(s):
    return bool(WEIGHT_ONLY_RE.match(s.strip()))


def _strip_weight_suffix(s):
    m = WEIGHT_SUFFIX_RE.match(s.strip())
    if m and m.group(1).strip():
        return m.group(1).strip()
    return s.strip()


def _correct_material(raw):
    """Fuzzy-corrects a material against Kulue's known commodity list (the
    same vocabulary its WhatsApp report pipeline recognizes), so a typo like
    'feeed' or 'sement' still displays as the real word. Leaves unrecognized
    text (a legitimate material just not in the list) untouched rather than
    guessing."""
    if not raw or not MATERIALS:
        return raw
    low = raw.lower()
    for m in MATERIALS:
        if m.lower() == low:
            return m.capitalize()
    match = difflib.get_close_matches(low, [m.lower() for m in MATERIALS], n=1, cutoff=0.72)
    if match:
        idx = [m.lower() for m in MATERIALS].index(match[0])
        return MATERIALS[idx].capitalize()
    return raw


def _extract_weight_tons(text):
    wm = WEIGHT_RE.search(text)
    if not wm:
        return None
    val = float(wm.group(1))
    unit = wm.group(2).lower()
    return round(val / 1000, 3) if unit == "kg" else val


def _fill_route_from_freetext(lines, used_lines):
    """Best-effort origin/destination guess for posts with no From:/To: labels."""
    candidates = [
        line for i, line in enumerate(lines)
        if line and i not in used_lines and not _is_weight_only(line)
    ]
    if not candidates:
        return None, None

    for line in candidates:
        m = ROUTE_TO_RE.match(line)
        if m:
            origin = _clean(m.group(1))
            destination = _clean(_strip_weight_suffix(m.group(2)))
            if origin and destination:
                return origin, destination

    if len(candidates) == 1:
        parts = [p for p in ROUTE_DELIM_RE.split(candidates[0]) if p.strip()]
        parts = [p.strip() for p in parts if not _is_weight_only(p)]
        if len(parts) == 2:
            return _clean(parts[0]), _clean(_strip_weight_suffix(parts[1]))
        return None, None

    origin, destination = candidates[0], candidates[1]
    return _clean(origin), _clean(_strip_weight_suffix(destination))


def parse_post(text):
    fields = {}
    lines = [raw_line.strip() for raw_line in text.splitlines()]
    used_lines = set()
    for i, line in enumerate(lines):
        if not line:
            continue
        for key, rx in FIELD_PATTERNS.items():
            if key in fields:
                continue
            m = rx.search(line)
            if m:
                fields[key] = _clean(m.group(1))
                used_lines.add(i)

    weight_tons = _extract_weight_tons(fields["weight_raw"]) if fields.get("weight_raw") else None

    origin = fields.get("origin")
    destination = fields.get("destination")
    if not origin or not destination:
        fb_origin, fb_destination = _fill_route_from_freetext(lines, used_lines)
        origin = origin or fb_origin
        destination = destination or fb_destination

    if weight_tons is None:
        weight_tons = _extract_weight_tons(text)

    return {
        "origin": origin,
        "destination": destination,
        "date": fields.get("date"),
        "vehicle_type": fields.get("vehicle_type"),
        "material": _correct_material(fields.get("material")),
        "weight_tons": weight_tons,
        "loading_unloading": fields.get("loading_unloading"),
    }
