"""Admin dashboard: upload new WhatsApp export batches directly (instead of
running the separate ~/kulue-tools/whatsapp-load-report pipeline by hand),
see how much data exists, the training-progress bar, upload history
(downloadable as Excel), and market-intelligence stats (top loading/
unloading locations, material, vehicle type, states with no data yet).

Every route here requires DATABASE_URL (there's nowhere sane to store a
growing, multi-hundred-thousand-row message log in a JSON file) and a
shared admin password (ADMIN_PASSWORD env var) via HTTP Basic Auth -- the
browser's native prompt, so no custom login page/frontend code is needed.
Unset ADMIN_PASSWORD means the feature is off, not "open to anyone."
"""
import io
import os
import secrets

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import db, pricing_rebuild, whatsapp_ingest
from app.geocode import reverse_state

router = APIRouter()
security = HTTPBasic()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# India's 28 states + 8 union territories -- a fixed list, used to diff
# against the states our known places actually cover (see get_covered_states
# in app/db.py, populated via reverse-geocoding -- see reverse_state() in
# app/geocode.py).
ALL_INDIAN_STATES = sorted([
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan",
    "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh",
    "Lakshadweep", "Puducherry",
])


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Admin dashboard is not configured (ADMIN_PASSWORD not set).")
    ok_user = secrets.compare_digest(credentials.username, "admin")
    ok_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(401, "Incorrect credentials", headers={"WWW-Authenticate": "Basic"})


def _require_db():
    if not db.enabled():
        raise HTTPException(503, "DATABASE_URL is not configured -- the admin dashboard needs Postgres.")


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_page():
    # Deliberately NOT under static/ -- that whole directory is served
    # publicly, unauthenticated, by app/main.py's StaticFiles mount, which
    # would let anyone fetch this file directly by name regardless of the
    # require_admin dependency on this route.
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base_dir, "admin_ui", "admin.html")) as f:
        return f.read()


@router.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status():
    _require_db()
    import app.main as main_module

    with db.get_pool().connection() as conn:
        total_messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        total_priced = conn.execute(
            "SELECT count(*) FROM messages WHERE route_origin IS NOT NULL AND route_dest IS NOT NULL "
            "AND freight IS NOT NULL AND vehicle_type IS NOT NULL"
        ).fetchone()[0]

    return {
        "total_messages": total_messages,
        "total_priced_quotes_raw": total_priced,
        "training_rows": main_module.pricing_model.n_rows,
        "known_corridors": len(main_module.pricing_model.known_corridors),
        "training_progress": main_module.training_progress(),
    }


@router.post("/api/admin/upload", dependencies=[Depends(require_admin)])
async def admin_upload(file: UploadFile = File(...), label: str = Form(default="")):
    _require_db()
    data = await file.read()
    try:
        parsed = whatsapp_ingest.parse_zip_bytes(data, file.filename or "upload.zip")
    except Exception as e:
        raise HTTPException(400, f"Couldn't read this as a WhatsApp export zip: {e}")

    if not parsed:
        raise HTTPException(400, "No messages found in this export.")

    groups_in_batch = len({m["group"] for m in parsed})
    no_timestamp = sum(1 for m in parsed if m["dt"] is None)

    with db.get_pool().connection() as conn:
        batch_id = conn.execute(
            """INSERT INTO batches (label, groups_in_batch, new_messages_in_batch, added,
                                     duplicates_skipped, no_timestamp_count)
               VALUES (%s, %s, %s, 0, 0, %s) RETURNING id""",
            (label.strip() or "Untitled batch", groups_in_batch, len(parsed), no_timestamp),
        ).fetchone()[0]

        # A single executemany() instead of one round-trip per message --
        # a batch can be 15,000+ messages, and this app has no background
        # job queue (see the plan's scoping note), so the whole upload has
        # to finish inside one HTTP request.
        rows = []
        for m in parsed:
            fields = whatsapp_ingest.extract_fields(m["text"])
            rows.append((
                m["group"], whatsapp_ingest.dedupe_key(m["group"]), m["dt"], m["text"], batch_id,
                fields["route_origin"], fields["route_dest"], fields["freight"],
                fields["vehicle_type"], fields["material"],
            ))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO messages (group_raw, group_key, dt, text, batch_id,
                                          route_origin, route_dest, freight, vehicle_type, material)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (group_key, dt, md5(text)) DO NOTHING""",
                rows,
            )

        added = conn.execute(
            "SELECT count(*) FROM messages WHERE batch_id = %s", (batch_id,)
        ).fetchone()[0]
        duplicates = len(parsed) - added

        conn.execute(
            "UPDATE batches SET added = %s, duplicates_skipped = %s WHERE id = %s",
            (added, duplicates, batch_id),
        )

    rebuild_result = pricing_rebuild.rebuild()

    return {
        "batch_id": batch_id,
        "messages_in_upload": len(parsed),
        "groups_in_batch": groups_in_batch,
        "added": added,
        "duplicates_skipped": duplicates,
        **rebuild_result,
    }


@router.get("/api/admin/batches", dependencies=[Depends(require_admin)])
def admin_batches():
    _require_db()
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT id, label, run_at, groups_in_batch, new_messages_in_batch, added, duplicates_skipped
               FROM batches ORDER BY run_at DESC"""
        ).fetchall()
    return {
        "batches": [
            {
                "id": r[0], "label": r[1], "run_at": r[2].isoformat() if r[2] else None,
                "groups_in_batch": r[3], "messages_in_batch": r[4], "added": r[5],
                "duplicates_skipped": r[6],
            }
            for r in rows
        ]
    }


@router.get("/api/admin/batches/export", dependencies=[Depends(require_admin)])
def admin_batches_export():
    _require_db()
    from openpyxl import Workbook

    with db.get_pool().connection() as conn:
        rows = conn.execute(
            """SELECT label, run_at, groups_in_batch, new_messages_in_batch, added, duplicates_skipped
               FROM batches ORDER BY run_at DESC"""
        ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Upload history"
    ws.append(["Label", "Date", "Groups", "Messages in batch", "Added", "Duplicates skipped"])
    for label, run_at, groups, msgs, added, dupes in rows:
        ws.append([label, run_at.strftime("%Y-%m-%d %H:%M") if run_at else "", groups, msgs, added, dupes])
    for col in ws.columns:
        width = max(len(str(c.value)) if c.value is not None else 0 for c in col) + 2
        ws.column_dimensions[col[0].column_letter].width = min(width, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=sahirate_upload_history.xlsx"},
    )


@router.get("/api/admin/analytics", dependencies=[Depends(require_admin)])
def admin_analytics():
    _require_db()

    def top(column, limit=10):
        with db.get_pool().connection() as conn:
            rows = conn.execute(
                f"SELECT {column}, count(*) FROM messages WHERE {column} IS NOT NULL "
                f"GROUP BY {column} ORDER BY count(*) DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [{"name": r[0], "count": r[1]} for r in rows]

    covered_states = set(db.get_covered_states())
    missing_states = sorted(set(ALL_INDIAN_STATES) - covered_states)

    return {
        "top_loading_locations": top("route_origin"),
        "top_unloading_locations": top("route_dest"),
        "top_materials": top("material"),
        "top_vehicle_types": top("vehicle_type"),
        "covered_states": sorted(covered_states),
        "missing_states": missing_states,
    }


@router.post("/api/admin/backfill-states", dependencies=[Depends(require_admin)])
def admin_backfill_states():
    """Resolves the state for any known place that doesn't have one cached
    yet (new places from a recent upload, or a first-time run before
    scripts/backfill_place_states.py has been run against this database)."""
    _require_db()
    resolved, failed = 0, 0
    for place_key, lat, lon in db.get_places_missing_state():
        state = reverse_state(lat, lon)
        if state:
            db.set_place_state(place_key, state)
            resolved += 1
        else:
            failed += 1
    return {"resolved": resolved, "failed": failed}
