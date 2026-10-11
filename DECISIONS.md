# DECISIONS.md

**1. Indexes.** On `attendance_logs`: unique `(emp_code, date)`, the record's real identity, which also blocks double punch-ins; `(date desc, emp_code)` for the list order and month scans; `(status, date desc, emp_code)` so a status filter stays sorted. On `employees`: unique `emp_code`, plus `(department, joined_on)` and `joined_on` for headcount. I skipped a `status`-only index: five values, and it would not remove the sort.

**2. Punch-in race.** Both requests just try to insert. The unique index lets MongoDB accept the first and reject the second with a duplicate-key error, which I return as 409. I never read first, so there is no gap between checking and writing.

**3. Ties.** `$rank` gives equal totals the same rank and skips the next (1, 2, 2, 4). The limit is applied after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`.

**4. Headcount.** The pipeline starts from `employees`. Each employee gets their logs via `$lookup`, and `$unwind` keeps those with none, so everyone who joined by month end counts.

**5. 100x data.** I would keep a pre-computed monthly summary per employee, updated on each punch or correction, instead of aggregating raw logs per request.
