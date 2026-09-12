# CLAUDE.md

## Project overview
This repo is the Kulue Rate Desk app: a FastAPI service that takes WhatsApp-style load posts and returns a suggested trip price and rate-per-ton using Kulue's own pricing model and route data.

## Key files
- `app/main.py` — FastAPI app and routes
- `app/parser.py` — parses load-post text
- `app/geocode.py` — geocoding and OSRM route distance logic
- `app/pricing.py` — pricing model and corridor logic
- `data/training_rows.json` — historical quote data used to train the model
- `static/index.html` — frontend UI

## Local development
Use the workspace virtual environment:

```bash
cd /Users/vasimkt/kulue-rate-desk
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

## Health check
```bash
curl -s http://127.0.0.1:8000/api/health
```

## Working style for this repo
- Keep changes focused on the parser, geocoder, pricing logic, or API contracts.
- Verify behavior with a relevant request or health check after patching.
- Prefer targeted, minimal edits over broad refactors.
- If changing pricing logic, confirm it still matches the expected route/quote data patterns.

## Important notes
- This app intentionally does not use an external LLM for the quote.
- Distance resolution relies on real geocoding/OSRM routing and local cache files.
- `data/training_rows.json` is the training source for the model.

## Useful commands
```bash
cd /Users/vasimkt/kulue-rate-desk
source .venv/bin/activate
python -m pytest   # if tests are added later
uvicorn app.main:app --reload --port 8000
```
