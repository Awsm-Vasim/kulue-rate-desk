"""Parses an uploaded WhatsApp export .zip into priced-quote fields, for the
admin dashboard's upload flow.

Regexes and extraction logic are lifted from the standalone WhatsApp-report
pipeline (~/kulue-tools/whatsapp-load-report/run_report.py), which this
module supersedes for ingestion purposes -- that script's parsing/analysis
sections (its own "1. PARSING" and "3. ANALYSIS") are pure, stdlib-only
regex logic with no dependency on its pickle-file store, so they're ported
here near-verbatim. Its HTML/PDF report rendering is NOT ported; the admin
dashboard has its own UI instead.
"""
import io
import os
import re
import zipfile
from datetime import datetime

LINE_RE = re.compile(
    r'^‎?\[?(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),\s*'
    r'(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s?(?:[APMapm\.]{2,4})?)\]?\s?-?\s?'
    r'(?P<sender>[^:]{1,80}):\s(?P<msg>.*)$'
)


def normalize_group_name(fname):
    name = fname
    if name.lower().endswith('.zip'):
        name = name[:-4]
    if name.startswith('WhatsApp Chat - '):
        name = name[len('WhatsApp Chat - '):]
    return name.strip()


def dedupe_key(name):
    """Collapses WhatsApp's re-export disambiguation suffix ('Group (2)')
    and punctuation/case differences so the same real group always dedupes
    against itself across separate uploads."""
    base = re.sub(r'\s*\(\d+\)\s*$', '', name)
    base = re.sub(r'[^\w]+', '', base, flags=re.UNICODE).lower()
    return base


def parse_dt(date_s, time_s):
    try:
        d, mo, y = date_s.split('/')
        y = int(y)
        if y < 100:
            y += 2000
        d, mo = int(d), int(mo)
        t = time_s.strip().upper().replace('.', '')
        ampm = None
        if t.endswith('AM') or t.endswith('PM'):
            ampm = t[-2:]
            t = t[:-2].strip()
        parts = t.split(':')
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        ss = int(parts[2]) if len(parts) > 2 else 0
        if ampm == 'PM' and hh != 12:
            hh += 12
        if ampm == 'AM' and hh == 12:
            hh = 0
        return datetime(y, mo, d, hh % 24, mm, ss)
    except Exception:
        return None


def _parse_group_txt(zip_bytes, gname):
    """One per-group WhatsApp export zip (containing _chat.txt or a single
    .txt) -> a flat list of {group, dt, text} message dicts."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        txt_names = [n for n in z.namelist()
                     if n.lower().endswith('.txt') and not os.path.basename(n).startswith('._')]
        if not txt_names:
            return []
        txt_names.sort(key=lambda n: (0 if os.path.basename(n) == '_chat.txt' else 1, n))
        raw = z.read(txt_names[0])
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')

    messages = []
    cur = None
    for line in text.split('\n'):
        line = line.rstrip('\r')
        clean = line.lstrip('‎‏')
        m = LINE_RE.match(clean)
        if m:
            if cur:
                messages.append(cur)
            dt = parse_dt(m.group('date'), m.group('time'))
            cur = {"group": gname, "dt": dt, "text": m.group('msg')}
        elif cur is not None and line.strip():
            cur["text"] += "\n" + line
    if cur:
        messages.append(cur)
    return messages


def parse_zip_bytes(data, upload_filename="upload.zip"):
    """Accepts either a zip-of-per-group-zips (the normal case -- a WhatsApp
    export Archive.zip containing many "WhatsApp Chat - <group>.zip" files,
    however deeply nested) or a zip that directly contains one group's own
    _chat.txt (a single-chat export). Returns a flat list of message dicts.
    """
    messages = []
    with zipfile.ZipFile(io.BytesIO(data)) as outer:
        inner_zip_names = [
            n for n in outer.namelist()
            if n.lower().endswith('.zip') and not os.path.basename(n).startswith('._')
        ]
        if inner_zip_names:
            for name in sorted(inner_zip_names):
                gname = normalize_group_name(os.path.basename(name))
                try:
                    messages.extend(_parse_group_txt(outer.read(name), gname))
                except Exception:
                    continue
        else:
            gname = normalize_group_name(os.path.basename(upload_filename)) or "Uploaded chat"
            messages.extend(_parse_group_txt(data, gname))
    return messages


# ============================================================
# Route / freight / vehicle / material extraction (per message)
# ============================================================

GAZETTEER = [
 "Perundurai","Coimbatore","Chennai","Pollachi","Bangalore","Bengaluru","Ariyalur","Hosur",
 "Kozhikode","Calicut","Tirunelveli","Pune","Malappuram","Muvattupuzha","Hyderabad","Cochin",
 "Ernakulam","Kangeyam","Madurai","Trichy","Tiruchirappalli","Madathikulam","Trivandrum",
 "Thiruvananthapuram","Thrissur","Kollam","Wayanad","Kanagapura","Kasaragod","Erode","Tirupur",
 "Kuttiady","Salem","Namakkal","Karur","Dindigul","Theni","Virudhunagar","Nagercoil",
 "Kanyakumari","Thoothukudi","Tuticorin","Ramanathapuram","Sivagangai","Pudukkottai",
 "Perambalur","Cuddalore","Villupuram","Vellore","Tiruvannamalai","Krishnagiri","Dharmapuri",
 "Kanchipuram","Chengalpattu","Tiruvallur","Nagapattinam","Thanjavur","Tiruvarur","Alappuzha",
 "Pathanamthitta","Kottayam","Idukki","Palakkad","Kannur","Bhiwandi","Nashik","Nagpur","Indore",
 "Bhopal","Ahmedabad","Surat","Vadodara","Rajkot","Jaipur","Lucknow","Kanpur","Gwalior","Tumkur",
 "Mysore","Mangalore","Hubli","Belgaum","Vijayawada","Guntur","Visakhapatnam","Nellore","Kurnool",
 "Tirupati","Chittoor","Warangal","Karimnagar","Kottakkal","Perumbavoor","Angamaly","Aluva",
 "Thodupuzha","Palladam","Chavdi","Tirupathur","Attur","Rasipuram","Sankagiri","Bhavani",
 "Gobichettipalayam","Mettupalayam","Coonoor","Ooty","Udumalpet","Valparai","Vellakoil",
 "Dharapuram","Kodumudi","Chennimalai","Sathyamangalam","Talavadi","Thalassery","Ottapalam",
 "Delhi","Mumbai","Kolkata","Thalappara","Payyoli","Vadakara","Parambra",
 "Balussery","Odakali","Gummidipoondi","Gummidipondi","Srikalahasti","Valayar",
 "Cheranalloor","Chettipalyam","Redhills","Pondicherry","Aurangabad","Bellary","Hassan","Kolar",
 "Sri City","Stuart Hill","Uthiramerur","Guindy","Yercaud","Katpadi","Kudankulam","Sengottai",
 "Kallakurichi","Gujiliamparai","Malur","Ambalamugal","Thalapady",
]
GAZETTEER = sorted(set(GAZETTEER), key=len, reverse=True)
PLACE_RE = re.compile(r'\b(' + '|'.join(re.escape(p) for p in GAZETTEER) + r')\b', re.IGNORECASE)

VEHICLE_PATTERNS = [
    (re.compile(r'\b(\d{1,2})\s*(?:ft|feet)\b', re.IGNORECASE), lambda m: f"{m.group(1)} ft"),
    (re.compile(r'\b(\d{1,2})\s*wheel(?:er)?\b', re.IGNORECASE), lambda m: f"{m.group(1)} wheel"),
    (re.compile(r'\bcontainer\b', re.IGNORECASE), lambda m: "container"),
]

COMMODITIES = ["container","steel","scrap","feed","wood","plywood","oil","furniture","plastic",
    "bags","cotton","machinery","cement","tiles","marble","granite","rice","sugar","chemicals",
    "tyres","tyre","paper","glass","food grains","textile","yarn","coconut","rubber","spare parts",
    "electronics","battery","cable","pipes","fertilizer","tea","coffee","spices","onion","banana"]
COMMODITY_RE = {c: re.compile(r'\b' + re.escape(c) + r's?\b', re.IGNORECASE) for c in COMMODITIES}

# Some groups post structured templates with the field label wrapped in
# WhatsApp text-formatting characters, e.g. "`FREIGHT` : 14000" -- _FMT
# absorbs those so the label-to-value match still goes through.
_FMT = r'[`*_]*'
FROM_RE = re.compile(r'(?:FROM|LOADING|PICKUP|PICK\s*UP)' + _FMT + r'\s*[:\-]?\s*([A-Za-z][A-Za-z .]{2,30})', re.IGNORECASE)
TO_RE = re.compile(r'\b(?:TO|UNLOADING|UNLOAD|DROP)' + _FMT + r'\s*[:\-]?\s*([A-Za-z][A-Za-z .]{2,30})', re.IGNORECASE)

_PSEP = r'[`*_\s:.\-–—()]*'
FREIGHT_FWD_RE = re.compile(
    r'(?:FREIGHT\s*(?:RATE)?|RATE|RS\.?|INR|₹)' + _PSEP + r'(?:RS\.?|INR|₹)?' + _PSEP + r'([\d,]{3,7})\b',
    re.IGNORECASE)
FREIGHT_REV_RE = re.compile(
    r'([\d,]{3,7})' + _PSEP + r'(?:RS\.?|INR|₹)?' + _PSEP + r'(?:FREIGHT|RATE)\b',
    re.IGNORECASE)
FREIGHT_SPAM_RE = re.compile(
    r'\b(invest|investment|profit|trading|forex|mining|bitcoin|crypto|earning|scheme|salary)\b',
    re.IGNORECASE)


def find_freight(text):
    m = FREIGHT_FWD_RE.search(text) or FREIGHT_REV_RE.search(text)
    if not m or FREIGHT_SPAM_RE.search(text):
        return None
    return m


def extract_place(text):
    m = PLACE_RE.search(text)
    return m.group(1) if m else None


def find_route(msg):
    fm = FROM_RE.search(msg)
    tm = TO_RE.search(msg)
    origin = extract_place(fm.group(1)) if fm else None
    dest = extract_place(tm.group(1)) if tm else None
    if origin and dest and origin.lower() != dest.lower():
        return (origin, dest)
    hits = []
    for m in PLACE_RE.finditer(msg):
        p = m.group(1)
        if not hits or hits[-1].lower() != p.lower():
            hits.append(p)
    seen = []
    for h in hits:
        if h.lower() not in [s.lower() for s in seen]:
            seen.append(h)
        if len(seen) == 2:
            return (seen[0], seen[1])
    return None


def canon(place):
    return place.strip().title()


def find_vehicle(text):
    for rx, fmt in VEHICLE_PATTERNS:
        m = rx.search(text)
        if m:
            return fmt(m)
    return None


def find_material(text):
    for c, rx in COMMODITY_RE.items():
        if rx.search(text):
            return c
    return None


def extract_fields(text):
    """Computed once per message at ingest time so analytics/training are
    plain SQL aggregates afterward, instead of re-scanning every message's
    raw text on every dashboard load.

    One simplification vs. the original pipeline: run_report.py's own
    "most mentioned vehicle/material" report counts every regex match across
    every message; storing a single first-match value per message here means
    each message contributes at most once to those stats -- arguably better
    (one bulk multi-item post can't inflate the count), but not numerically
    identical to the old report's figures."""
    route = find_route(text)
    origin, dest = (canon(route[0]), canon(route[1])) if route else (None, None)

    freight_val = None
    fm = find_freight(text)
    if fm:
        try:
            val = int(fm.group(1).replace(',', ''))
            if 100 <= val <= 300000:
                freight_val = val
        except ValueError:
            pass

    return {
        "route_origin": origin,
        "route_dest": dest,
        "freight": freight_val,
        "vehicle_type": find_vehicle(text),
        "material": find_material(text),
    }
