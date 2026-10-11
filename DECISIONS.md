# DECISIONS.md

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)` (the natural key, makes punch-in race-safe, serves per-employee queries); `(date desc, emp_code)` (matches the list sort and month range scans); `(status, date desc, emp_code)` (status filter keeps the same order). `employees`: unique `emp_code`; `(department, joined_on)` and `joined_on` for headcount. I rejected a plain `status` index: it has very few distinct values and would still need a sort.

2. **Punch-in race.** Both requests try `insert_one`. The unique `(emp_code, date)` index lets the database accept one and reject the other with a duplicate-key error, which I turn into 409. There is no "check first" step, so there is no gap to race through.

3. **Ties.** Ranks use `$rank`, so tied employees share a rank and the next rank is skipped. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned and the list can be longer than `limit`.

4. **Headcount.** The pipeline starts from `employees` and joins each person's logs with `$lookup`. `$unwind` keeps people with no logs, so every employee who joined by month end counts once.

5. **100x data.** I would stop aggregating raw logs per request and keep pre-computed daily or monthly summaries per employee, updated on each punch or correction.
