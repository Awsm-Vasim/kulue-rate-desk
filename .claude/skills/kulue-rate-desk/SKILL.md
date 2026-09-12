---
name: kulue-rate-desk
description: "Maintain and retrain the Kulue Rate Desk app in this repo (kulue-rate-desk) -- a self-hosted FastAPI service that prices WhatsApp-style load posts from Kulue's own trained pricing model. Trigger when working in this repo and asked to retrain the pricing model, refresh its data, run/debug it locally, or deploy/update it on Render."
disable-model-invocation: false
---

# Kulue Rate Desk (this repo)

Self-hosted app (no external AI call) that parses a pasted WhatsApp-style load post
and returns a suggested trip price + price/ton, using:
- real geocoding (Nominatim) + real driving distance (OSRM) -- see `app/geocode.py`
- a small trained pricing model fit on Kulue's own WhatsApp corridor data -- see
  `app/pricing.py`
- a regex parser for the labeled post format (`From:`, `To:`, `Vehicle Type:`, etc.)
  -- see `app/parser.py`

It replaced an earlier prototype built as a Claude Artifact (which depended on live
LLM calls per quote and per-viewer Claude credits); this version is fully
self-hosted and free to run once deployed.

## Retraining / refreshing the data

The pricing model is `data/training_rows.json`, a non-PII export (place-pair
distances, vehicle types, freight amounts only -- never raw message text or phone
numbers) from the separate WhatsApp report pipeline at
`~/kulue-tools/whatsapp-load-report`. To refresh after a new WhatsApp export batch:

```bash
cd ~/kulue-tools/whatsapp-load-report
python3 run_report.py --new "<path to new export>"   # or omit --new to just re-run over existing data
cp state/pricing_model.json ~/kulue-rate-desk/data/training_rows.json
```

The model retrains itself from this file at server startup -- no code changes
needed. Commit and push the updated `data/training_rows.json` to redeploy.

## Running locally

```bash
cd ~/kulue-rate-desk
source .venv/bin/activate   # create it first with: python3 -m venv .venv && pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Open http://127.0.0.1:8000 . In VS Code: open `kulue-rate-desk.code-workspace`,
use the "Kulue Rate Desk (debug server)" launch config (F5) or the "Run dev server"
task, and `requests.http` (with the REST Client extension) to hit the API directly.

If a background/detached run of the server gets killed, it's almost always the
host machine running low on memory, not an app bug (the service itself is tiny) --
check what else is using RAM before assuming the code broke.

## Known limitation: obscure place names

There's no LLM in the loop to paper over typos or disambiguate compound place
names, so `app/geocode.py` does deterministic fallbacks instead (see its
docstring): exact match, fuzzy-correction against `data/gazetteer.json`, and for
a "locality, city" post, an anchor-bounded search around the city part. A truly
obscure hamlet not in OpenStreetMap can still fail to resolve, or resolve to nothing
within the bounded search -- that surfaces as an honest error, not a silently wrong
price. If it happens often for a particular place, consider adding that place (and
its coordinates once confirmed) to `data/gazetteer.json` / `data/geocode_cache_seed.json`.

## Deploying

Render, via `render.yaml`, with a custom domain pointed at it via CNAME/ALIAS. See
`README.md` for the exact steps (GitHub push, Render setup, DNS record, free-tier
cold-start note).

See also the (separate, user-level) `kulue-transport-report` skill, which regenerates
the underlying WhatsApp analysis this app's pricing model is trained from.
