"""One-time: imports the existing WhatsApp pipeline's full accumulated
history (~/kulue-tools/whatsapp-load-report/state/master_messages.pkl --
324,748 messages / 9 batches as of 2026-09-26) into this app's own Postgres
`messages`/`batches` tables, so the admin dashboard starts with full
history instead of needing all 9 old export zips re-uploaded by hand.

Route/freight/vehicle/material fields are recomputed fresh via
app.whatsapp_ingest.extract_fields() for every message rather than trusting
anything already in the pickle, so results are consistent with what a live
upload through the admin dashboard would produce.

The pickle's message dicts don't record which batch each one came from
(only aggregate per-run stats live in its `batches` list) -- so imported
`batches` rows get accurate historical counts, but all imported messages
are attributed to the last batch as a placeholder foreign key; this only
affects an unused historical drill-down, not any dashboard stat shown today.

Run once, locally -- the pickle only exists on this machine:
    python3 scripts/import_master_pickle.py
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, whatsapp_ingest

PICKLE_PATH = os.path.expanduser("~/kulue-tools/whatsapp-load-report/state/master_messages.pkl")
CHUNK = 5000


def main():
    if not db.enabled():
        print("DATABASE_URL is not set (check your .env) -- nothing to import into.")
        sys.exit(1)
    if not os.path.exists(PICKLE_PATH):
        print(f"Pickle not found at {PICKLE_PATH} -- nothing to import.")
        sys.exit(1)

    with open(PICKLE_PATH, "rb") as f:
        master = pickle.load(f)

    messages = master.get("messages", [])
    batches = master.get("batches", [])
    print(f"Loaded {len(messages)} messages, {len(batches)} recorded batches from the pickle.")

    with db.get_pool().connection() as conn:
        batch_ids = []
        for b in batches:
            row = conn.execute(
                """INSERT INTO batches (label, run_at, groups_in_batch, new_messages_in_batch,
                                         added, duplicates_skipped, no_timestamp_count)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    b.get("label") or "Imported batch", b.get("run_at"),
                    b.get("groups_in_batch", 0), b.get("new_messages_in_batch", 0),
                    b.get("added", 0), b.get("duplicates_skipped", 0), b.get("no_timestamp", 0),
                ),
            ).fetchone()
            batch_ids.append(row[0])
        last_batch_id = batch_ids[-1] if batch_ids else None
        print(f"Inserted {len(batch_ids)} batch record(s).")

        rows = []
        for m in messages:
            fields = whatsapp_ingest.extract_fields(m["text"])
            rows.append((
                m["group"], whatsapp_ingest.dedupe_key(m["group"]), m["dt"], m["text"], last_batch_id,
                fields["route_origin"], fields["route_dest"], fields["freight"],
                fields["vehicle_type"], fields["material"],
            ))

        print(f"Inserting {len(rows)} messages...")
        with conn.cursor() as cur:
            for i in range(0, len(rows), CHUNK):
                cur.executemany(
                    """INSERT INTO messages (group_raw, group_key, dt, text, batch_id,
                                              route_origin, route_dest, freight, vehicle_type, material)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (group_key, dt, text) DO NOTHING""",
                    rows[i:i + CHUNK],
                )
                print(f"  ... {min(i + CHUNK, len(rows))}/{len(rows)}")

        total = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        priced = conn.execute(
            "SELECT count(*) FROM messages WHERE route_origin IS NOT NULL AND route_dest IS NOT NULL "
            "AND freight IS NOT NULL AND vehicle_type IS NOT NULL"
        ).fetchone()[0]

    print(f"\nDone. messages table now has {total} rows, {priced} of them priced quotes.")
    print('Next: python3 -c "from app.pricing_rebuild import rebuild; print(rebuild())" '
          "to build corridors/training_rows from this data.")


if __name__ == "__main__":
    main()
