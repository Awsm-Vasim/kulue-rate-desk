# Kulue Rate Desk

Paste a WhatsApp-style load post, get a suggested trip price and price-per-ton —
grounded entirely in Kulue's own accumulated WhatsApp transport-group data. No
external AI call: pricing comes from a small model trained on real historical
quotes, and distance comes from real geocoding + road routing (OpenStreetMap /
OSRM), not an estimate.

## How it works

- `app/parser.py` — parses the labeled post format (`From:`, `To:`, `Date:`,
  `Vehicle Type:`, `Material:`, `Weight:`).
- `app/geocode.py` — resolves place names to coordinates (Nominatim) and real
  driving distance (OSRM), with disk-cached results and typo/compound-name
  fallback strategies (see the module docstring — there's no LLM here to paper
  over messy input, so this does a few deterministic things instead: exact
  match, fuzzy-correct against a gazetteer of towns from Kulue's own data, and
  for a "locality, city" style post, an anchor-bounded search).
- `app/pricing.py` — the trained model: an empirical ₹/km-by-distance curve
  fit on `data/training_rows.json`, with a per-vehicle-type adjustment and an
  override for routes that already have real historical quotes
  (`known_corridors`).
- `app/main.py` — FastAPI app serving `/api/quote`, `/api/corridors`,
  `/api/health`, and the static frontend in `static/`.

## Data / retraining

`data/training_rows.json` is a non-PII export from the
[WhatsApp report pipeline](../whatsapp-load-report) — place-pair distances,
vehicle types, and freight amounts only, never raw message text or phone
numbers. To refresh it after a new WhatsApp export batch:

```bash
cd ~/kulue-tools/whatsapp-load-report
python3 run_report.py --new "<path to new export>"   # or omit --new to just re-run over existing data
cp state/pricing_model.json ~/kulue-rate-desk/data/training_rows.json
```

Commit and push — the model retrains itself from this file at server startup,
no code changes needed.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000
```

## Known limitation: obscure place names

Without an LLM in the loop, a place name that isn't in OpenStreetMap at all,
or is genuinely ambiguous (a small hamlet sharing a name with a bigger place
elsewhere in India), can fail to resolve or resolve to the wrong one. If a
quote comes back with an unexpected distance, or an error saying a place
couldn't be located, try the nearest well-known town/city name instead of a
small locality.

## Publishing to GitHub

```bash
cd ~/kulue-rate-desk
git init -b main
git add .
git commit -m "Initial commit: Kulue Rate Desk"
git remote add origin https://github.com/<your-username>/kulue-rate-desk.git
git push -u origin main
```

(Create the empty repo on github.com first — no README/license/gitignore, so
it doesn't conflict with what's already here.)

## Deploying on Render, with your own domain

1. On [render.com](https://render.com), **New > Web Service**, connect the
   `kulue-rate-desk` GitHub repo. Render reads `render.yaml` automatically —
   confirm the free plan and click **Create Web Service**. First deploy takes
   a couple of minutes; you'll get a `kulue-rate-desk.onrender.com` URL.
2. **Custom domain:** in the service's **Settings > Custom Domains**, add
   your domain or subdomain (e.g. `ratedesk.kulue.com`). Render shows you a
   CNAME target (`kulue-rate-desk.onrender.com`) — add that as a CNAME record
   at your domain registrar/DNS provider for that subdomain. For a bare
   root domain (`kulue.com` with no subdomain) Render instead gives you an
   A/ALIAS record to add. DNS propagation is usually minutes, sometimes up
   to a few hours.
3. Render auto-issues an HTTPS certificate for the custom domain once DNS
   resolves — no extra setup.
4. Every push to `main` auto-redeploys.

**Free-tier note:** Render's free web services spin down after 15 minutes of
no traffic and take ~30-50s to wake up on the next request — fine for
internal, occasional use; upgrade to a paid instance if that cold-start delay
becomes annoying for daily dispatch use.

## Internal-only access

This app itself has no login — anyone with the URL/domain can use it. If it
needs to be restricted to your team, the simplest option on Render is Basic
Auth in front of it (a small middleware) or Render's paid-tier IP allowlist;
ask if you want that added.
