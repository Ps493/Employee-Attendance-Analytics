# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
Indexes are created idempotently at startup; there are no manual steps. Interactive docs: `/docs`.

## Status
Everything in the contract is implemented (Parts A, B, C).

## Notes / interpretations
- `moving_avg_7d` averages the *reported* (4-decimal-rounded) `attendance_rate` values, then rounds again.
- A trend range of 92 days is allowed (counted inclusively); 93 is a 422.
- `PATCH` uses optimistic concurrency: a request that loses a race gets 409 and can be retried.
- Analytics queries pass an explicit `hint` so the intended index is always used.
- Requires MongoDB 6.0+ (`$densify`, `$setWindowFields`).
