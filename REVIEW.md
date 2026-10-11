# REVIEW.md

Review of the starter `app/main.py`. Line references are to the starter file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | Client is not `tz_aware`, so PyMongo returns **naive** datetimes. Naive and aware values cannot be subtracted or compared, and the zone is a guess. | `compute_overtime(punch_out, ...)` raises `TypeError: can't subtract offset-naive and offset-aware datetimes` once punch-out exists. | `tz_aware=True`; all datetimes are aware UTC; `dt_to_ms` also copes with a naive value. |
| 2 | `compute_late_minutes` | `punch_in.replace(hour=h, minute=m)` anchors the shift start to the punch's own (UTC/local) date and zone, not IST. Overnight shifts are ignored. | Punch-in 09:50 IST = 04:20 UTC gives a shift start of 09:30 **UTC** and a hugely negative result. | Shift start is built from the attendance date in IST (`shift_window`). |
| 3 | `compute_late_minutes` | Grace test is done on floored minutes: `minutes > 10`. | 09:40:01 on a 09:30 shift floors to 10, `10 > 10` is false, so late = 0 (should be 10). | Compare **seconds**: `secs > 600`, then `secs // 60`. |
| 4 | `compute_late_minutes` / punch-in | Instants are not truncated to whole seconds (R1). `int(float/60)` also truncates toward zero. | 09:40:00.900 treated as late. | `ms_to_dt` floors to whole seconds; integer maths only. |
| 5 | `compute_work_hours` | Python `round()` is banker's rounding on a float (R4 wants half-up). | 14850 s = 4.125 h gives 4.12 instead of 4.13. | `Decimal` + `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3); `date_str` + shift_end assumes the same day, so overnight shifts are wrong; naive/aware crash (see #1). | 5 minutes after shift end reports overtime 5; 06:40 for a 22:00-06:00 shift is compared with 06:00 of the *start* day. | Minutes floored, counted only if >= 30; end instant comes from `shift_window` (next day when overnight). |
| 7 | Missing rule | Half day (R5) is never computed, and nothing derives the fields as one unit. | Punch-out for 3.5 h leaves `half_day` false. | One `derive()` used by punch-in, punch-out and PATCH. |
| 8 | `/health` | Always returns ok, even if MongoDB is down. | Stop mongod; health still 200. | Pings MongoDB; 503 on failure. |
| 9 | `create_employee` | `find_one` then `insert_one` is a race; no unique index, so duplicate `emp_code`s can be created. | Fire two identical POSTs in parallel: two 201s. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 10 | `EmployeeIn` / `PunchInIn` | Almost no validation: any `emp_code`/email/`joined_on`, shift times unchecked, `shift_start == shift_end` allowed, `status` is a free string (ABSENT accepted at punch-in), `punched_at` accepts floats, numeric strings, seconds, `True`. | `joined_on: "2026-02-30"` -> 201; `punched_at: 1783312500` (seconds) -> 201 and a 1970 date. | Pydantic patterns, real-date validator, `Literal` statuses, strict ms range for instants. |
| 11 | `create_employee` | `created_at = datetime.now()` is naive **local** time stored as if UTC; response returns an ISO datetime instead of epoch ms. | Different server time zone shifts the stored value. | UTC, ms precision, returned as epoch ms. |
| 12 | `list_employees` | `skip = page * page_size` skips a whole page; page 1 returns page 2. | `?page=1` on a short list returns nothing. | `(page - 1) * page_size`. |
| 13 | `list_employees` | `total` counts the whole collection, not the filter; no sort, so paging is unstable; no bounds on `page`/`page_size` (0 returns everything, negative -> 500). | `?department=Sales` shows total of all employees. | `count_documents(filter)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 14 | `punch_in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` -> `TypeError` -> **500** (should be 404). | POST with an unknown `emp_code`. | `get_employee` raises 404. |
| 15 | `punch_in` | Date comes from `datetime.fromtimestamp(...)` in the **server's** zone and float division, not IST (R1); overnight early-morning punch-ins are not assigned to the previous day; `if body.punched_at` treats 0 as "missing". | 00:30 IST punch on a UTC server lands on the previous date. | `attendance_date()` implements R1 incl. overnight; `is not None` checks. |
| 16 | `punch_in` | Check-then-insert race: two simultaneous punch-ins both pass `find_one`, both insert. | Parallel requests -> two records for one (emp, date). | Unique index `(emp_code, date)` + `DuplicateKeyError` -> 409. |
| 17 | `punch_in` | Response includes `id` (an ObjectId string) and raw datetimes; the contract forbids ids and requires epoch ms. | Look at the 201 body. | `record_out()` serialiser (no `_id`, ms instants). |
| 18 | `list_attendance` | Loads the **entire** matching set into memory, sorts and paginates in Python; no index. | 100k records: slow and memory-heavy. | Database sort/skip/limit and `count_documents`, backed by three compound indexes. |
| 19 | `list_attendance` | Sort is by `date` only (ties unordered, spec wants `emp_code` asc); no validation of dates (`date_from=abc` accepted), no `date_from > date_to` 422, no bounds on paging; legacy docs missing `half_day`/`history` are returned incomplete; `id` and datetimes leak. | `?date_from=garbage`; `?page=0` (negative slice); legacy record lacks `history`. | Real-date validation, 422 on inverted range, sort `(date desc, emp_code asc)`, defaults applied in `record_out`. |
| 20 | Whole file | No indexes at all and no startup hook; also nothing maps DB failures to a sensible status. | `explain` shows COLLSCAN. | `ensure_indexes()` in the lifespan (idempotent); `PyMongoError` -> 503. |

## Looked at and judged fine
- `load_dotenv()` does not override existing environment variables, so real env vars win (contract requirement).
- Using the synchronous `MongoClient` inside plain `def` endpoints: FastAPI runs them in a thread pool and `MongoClient` is thread-safe, so it is fine; correctness comes from the unique index, not from the driver.
- `doc.pop("_id")` after `insert_one`: correct (insert adds `_id` to the dict), though serialising explicitly is cleaner.
- Default `ts = datetime.now()` when `punched_at` is omitted is right in spirit (use "now"); only the zone/truncation were wrong.
