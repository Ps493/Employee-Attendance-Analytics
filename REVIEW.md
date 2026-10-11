# REVIEW.md

Defects found in the starter `app/main.py`, with how each shows up and what I did about it.

| # | Where | Problem | How to spot it | My fix |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware`, so datetimes come back without a time zone. They can't be mixed with aware ones. | Subtracting in `compute_overtime` raises `TypeError` (naive vs aware) once a punch-out exists. | `tz_aware=True`; everything is UTC-aware. `dt_to_ms` also accepts a naive value. |
| 2 | `compute_late_minutes` | `replace(hour, minute)` puts the shift start on the punch's own date and zone, not IST. Overnight shifts are not handled. | Punch at 09:50 IST (04:20 UTC) is compared with 09:30 UTC, giving a large negative number. | Shift start is built from the attendance date in IST (`shift_window`). |
| 3 | `compute_late_minutes` | The 10-minute grace is tested on already-floored minutes (`minutes > 10`). | 09:40:01 on a 09:30 shift floors to 10, `10 > 10` is false, so late shows 0 instead of 10. | Test seconds (`> 600`), then divide by 60. |
| 4 | `compute_late_minutes`, punch-in | Time is not cut to whole seconds (R1). `int()` also rounds toward zero. | 09:40:00.900 gets treated as late. | `ms_to_dt` drops the sub-second part; only integer maths after that. |
| 5 | `compute_work_hours` | `round()` on a float rounds half to even, but R4 wants half-up. | 14850 s is 4.125 h and comes out as 4.12, not 4.13. | `Decimal` with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | No 30-minute minimum (R3). It uses same-day shift end, wrong for overnight. Same naive/aware crash as #1. | Leaving 5 minutes late reports overtime 5. A 22:00-06:00 worker leaving at 06:40 is compared to 06:00 of the wrong day. | Floor the minutes, count only if at least 30, take the end from `shift_window` (next day when overnight). |
| 7 | Whole file | Half day (R5) is never set, and derived fields aren't computed together. | A 3.5 h day still has `half_day: false`. | One `derive()` function used by punch-in, punch-out and PATCH. |
| 8 | `/health` | Always says ok. | Stop MongoDB and it still returns 200. | Pings the database; returns 503 if that fails. |
| 9 | `create_employee` | Find-then-insert can race, and there is no unique index. | Two identical POSTs at once both return 201. | Unique index on `emp_code`; catch `DuplicateKeyError` and return 409. |
| 10 | `EmployeeIn`, `PunchInIn` | Almost nothing is validated: bad email, bad dates, equal shift times, any status string (even ABSENT), `punched_at` as float, string, seconds or `True`. | `joined_on: "2026-02-30"` returns 201. `punched_at` in seconds returns 201 with a 1970 date. | Patterns, a real-date check, `Literal` statuses, strict integer in a millisecond range. |
| 11 | `create_employee` | `created_at` is naive local time and is returned as an ISO string. | Changing the server's time zone changes the stored value. | UTC, millisecond precision, returned as epoch ms. |
| 12 | `list_employees` | `skip = page * page_size` skips one page too many. | Page 1 returns what should be page 2. | `(page - 1) * page_size`. |
| 13 | `list_employees` | `total` ignores the filter. No sort, so pages are unstable. No limits on paging values. | `?department=Sales` shows the total of everyone. `page_size=0` returns all rows; a negative value gives a 500. | Count with the filter, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 14 | `punch_in` | Unknown employee gives `None`, then `emp["shift_start"]` crashes. | POST with an unknown `emp_code` returns 500. | `get_employee` returns 404. |
| 15 | `punch_in` | Date is taken in the server's time zone, not IST. Early-morning overnight punches don't go to the previous day. `if body.punched_at` treats 0 as missing. | A punch at 00:30 IST on a UTC server lands on the wrong date. | `attendance_date()` follows R1 including overnight; use `is not None`. |
| 16 | `punch_in` | Check-then-insert race. | Parallel punch-ins create two records for one day. | Unique `(emp_code, date)` index; duplicate key becomes 409. |
| 17 | `punch_in` | Response has an `id` and raw datetimes, which the contract does not allow. | Look at the 201 body. | `record_out()` removes `_id` and converts to epoch ms. |
| 18 | `list_attendance` | Loads every matching record into memory, then sorts and pages in Python. No index. | Slow and memory-heavy at 100k records. | Sort, skip and limit in MongoDB, with three compound indexes. |
| 19 | `list_attendance` | Sorts by date only. Dates aren't validated. `date_from > date_to` isn't rejected. Paging unchecked. Old records without `half_day` or `history` come back incomplete. Leaks `id`. | `?date_from=garbage`; `?page=0`; a legacy record with no `history`. | Date validation, 422 on a reversed range, sort by date desc then `emp_code`, defaults filled in `record_out`. |
| 20 | Whole file | No indexes, no startup step, and database errors are not handled. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup (safe to repeat); database errors return 503. |

## Checked and fine
- `load_dotenv()` doesn't override variables already set, so real environment variables win, as the contract requires.
- The synchronous `MongoClient` in normal `def` endpoints is fine: FastAPI uses a thread pool and the client is thread-safe. Correctness comes from the unique indexes.
- `doc.pop("_id")` after `insert_one` works because the insert adds `_id` to the dict. Explicit serialising is just cleaner.
- Using the current time when `punched_at` is missing is the right idea; only its zone and precision were wrong.
