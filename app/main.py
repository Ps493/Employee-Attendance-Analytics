"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env)

Layout of this file
  1. configuration & database      5. request models
  2. time helpers (R1)             6. serializers
  3. business rules (R2-R5)        7. query/pipeline builders (shared by endpoints and /admin/explain)
  4. rounding helpers (R8)         8. endpoints
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime as DateTime
from datetime import time as Time
from datetime import timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration & database
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones,
# so every datetime in this program is unambiguous.
client = MongoClient(
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.environ.get("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = DateTime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

# name -> (collection, keys, options). Names are explicit so queries can hint them.
INDEXES = {
    "employees": [
        # natural key; makes POST /employees race-safe; serves the leaderboard/summary lookups
        ([("emp_code", 1)], {"name": "emp_code_1", "unique": True}),
        # department filter + joined_on range (headcount, summary, trend)
        ([("department", 1), ("joined_on", 1)], {"name": "department_1_joined_on_1"}),
        # joined_on range when no department is given (summary over all departments)
        ([("joined_on", 1)], {"name": "joined_on_1"}),
    ],
    "attendance_logs": [
        # natural key; unique => race-safe punch-in; serves per-employee list/monthly/lookups
        ([("emp_code", 1), ("date", 1)], {"name": "emp_code_1_date_1", "unique": True}),
        # GET /attendance sort order (date desc, emp_code asc) + month range scans (leaderboard)
        ([("date", -1), ("emp_code", 1)], {"name": "date_-1_emp_code_1"}),
        # GET /attendance?status=...: equality, then the same sort order
        ([("status", 1), ("date", -1), ("emp_code", 1)], {"name": "status_1_date_-1_emp_code_1"}),
    ],
}


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    for coll, specs in INDEXES.items():
        for keys, opts in specs:
            db[coll].create_index(keys, **opts)


@asynccontextmanager
async def lifespan(_: FastAPI):
    for attempt in (1, 2):
        try:
            ensure_indexes()
            break
        except PyMongoError as exc:
            log.error("index creation failed (attempt %s): %s", attempt, exc)
            if attempt == 2:
                raise
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> DateTime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(value: DateTime) -> int:
    """Datetime -> epoch ms. A naive datetime is assumed to be UTC (that is how Mongo stores it)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)  # integer maths, no float error


def now_seconds() -> DateTime:
    """Current time, whole seconds: used as the default punched_at."""
    return DateTime.now(UTC).replace(microsecond=0)


def now_millis() -> DateTime:
    """Current time at the precision BSON actually stores (milliseconds)."""
    n = DateTime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


def hhmm(value: str) -> Time:
    h, m = value.split(":")
    return Time(int(h), int(m))


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: DateTime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; an overnight early-morning punch-in belongs to the day before."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(emp) and ist.time() < hhmm(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_window(date_str: str, emp: dict) -> tuple[DateTime, DateTime]:
    """Start and end instants (IST-aware) of the shift that belongs to attendance date `date_str`."""
    day = Date.fromisoformat(date_str)
    start = DateTime.combine(day, hhmm(emp["shift_start"]), tzinfo=IST)
    end = DateTime.combine(day, hhmm(emp["shift_end"]), tzinfo=IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------- #
# 3. Business rules R2-R5  (all inputs are whole-second datetimes -> exact integer maths)
# --------------------------------------------------------------------------- #
def half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def compute_late_minutes(punch_in: DateTime, shift_start: DateTime) -> int:
    """R2: late only if strictly more than 600 s after shift start; then floor minutes since shift start."""
    secs = int((punch_in - shift_start).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_overtime(punch_out: DateTime, shift_end: DateTime) -> int:
    """R3: floor minutes after shift end, counted only when >= 30."""
    secs = int((punch_out - shift_end).total_seconds())
    minutes = secs // 60  # floor, also for negatives
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: DateTime, punch_out: DateTime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals (Decimal, not float round())."""
    secs = int((punch_out - punch_in).total_seconds())
    return float(half_up(Decimal(secs) / Decimal(3600), 2))


def derive(status: str, punch_in: Optional[DateTime], punch_out: Optional[DateTime], emp: dict, date_str: str) -> dict:
    """All stored derived fields for a record in its final state (see the table in DATA_MODEL.md)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    start, end = shift_window(date_str, emp)
    out["late_minutes"] = compute_late_minutes(punch_in, start)
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, end)
        out["half_day"] = out["work_hours"] < 4.5  # R5, on the ROUNDED value
    return out


# --------------------------------------------------------------------------- #
# 4. Validation helpers
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def parse_date(value: str, name: str = "date") -> Date:
    if not DATE_RE.fullmatch(value):
        raise unprocessable(f"{name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} is not a real calendar date")


def month_bounds(month: str) -> tuple[Date, Date]:
    try:
        first = Date(int(month[:4]), int(month[5:7]), 1)
    except ValueError:
        raise unprocessable("month is not valid")
    nxt = Date(first.year + 1, 1, 1) if first.month == 12 else Date(first.year, first.month + 1, 1)
    return first, nxt - timedelta(days=1)


def working_days(first: Date, last: Date, joined_on: Date) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last."""
    day, n = max(first, joined_on), 0
    while day <= last:
        if day.weekday() < 5:
            n += 1
        day += timedelta(days=1)
    return n


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
# strict=True: floats, strings and booleans are rejected (422) instead of silently coerced.


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if not DATE_RE.fullmatch(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        # Optional means "may be omitted"; an explicit null is not a valid value.
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        return self


# --------------------------------------------------------------------------- #
# 6. Serializers (BSON -> API)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


def _ms_or_none(value) -> Optional[int]:
    return dt_to_ms(value) if value is not None else None


def record_out(doc: dict) -> dict:
    """Never leaks _id; applies the 'missing means default' rules for legacy documents."""
    history = []
    for entry in doc.get("history") or []:
        changes = {}
        for field, ft in entry.get("changes", {}).items():
            if field in ("punch_in", "punch_out"):  # dates nested inside the audit entry
                changes[field] = {"from": _ms_or_none(ft.get("from")), "to": _ms_or_none(ft.get("to"))}
            else:
                changes[field] = {"from": ft.get("from"), "to": ft.get("to")}
        history.append({"at": dt_to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_or_none(doc.get("punch_in")),
        "punch_out": _ms_or_none(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 7. Query / pipeline builders. The endpoints AND /admin/explain both call these,
#    so the explained query is by construction the one the endpoint runs.
#    A "plan" is a plain dict: {kind, collection, ...}.
# --------------------------------------------------------------------------- #
def plan_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:  # ISO strings compare chronologically
        raise unprocessable("date_from must not be after date_to")
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    # With a single employee, emp_code is constant so "date desc" alone is the full order and the
    # (emp_code, date) index can be walked backwards without an in-memory sort.
    sort = [("date", -1)] if emp_code is not None else [("date", -1), ("emp_code", 1)]
    return {
        "kind": "find", "collection": "attendance_logs", "filter": q, "sort": sort,
        "skip": (page - 1) * page_size, "limit": page_size,
    }


# ---- shared per-record accumulators (used by the monthly and the department pipelines) ----
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_ON_WEEKDAY = {"$in": [_DOW, [2, 3, 4, 5, 6]]}  # $dayOfWeek: 1 = Sunday ... 7 = Saturday
_HALF = {"$ifNull": ["$half_day", False]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def metric_accumulators() -> dict:
    """$group accumulators. present is counted in HALVES (full day = 2, half day = 1) so it stays an integer."""
    return {
        "present_halves": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _ON_WEEKDAY]}, {"$cond": [_HALF, 1, 2]}, 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, _LATE, 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # work hours summed as integer hundredths: Python finishes the average with exact Decimal maths
        "wh_cents": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
        "wh_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
    }


def plan_employee_monthly(emp_code: str, month: str) -> dict:
    first, last = month_bounds(month)
    return {
        "kind": "aggregate", "collection": "attendance_logs", "hint": "emp_code_1_date_1",
        "pipeline": [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$group": {"_id": None, **metric_accumulators()}},
        ],
    }


def plan_department_summary(month: str, department: Optional[str]) -> dict:
    """Employees drive the pipeline (so people with zero logs still count), logs are joined in per employee."""
    first, last = month_bounds(month)
    match_emp: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        match_emp["department"] = department
    return {
        "kind": "aggregate", "collection": "employees",
        "hint": "department_1_joined_on_1" if department is not None else "joined_on_1",
        "pipeline": [
            {"$match": match_emp},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"c": "$emp_code"},
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                                "$expr": {"$eq": ["$emp_code", "$$c"]}}},
                    {"$group": {"_id": None, **metric_accumulators()}},
                ],
                "as": "m",
            }},
            # preserveNullAndEmptyArrays: an employee with no logs survives with m missing => counts as 0
            {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{k: {"$sum": f"$m.{k}"} for k in
                   ("present_halves", "leave_count", "on_duty_count", "late_count", "total_late_minutes", "wh_cents", "wh_n")},
            }},
            {"$sort": {"_id": 1}},
        ],
    }


def plan_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "let": {"c": "$_id"},
                     "pipeline": [{"$match": {"$expr": {"$eq": ["$emp_code", "$$c"]}}}], "as": "e"}},
        {"$unwind": "$e"},  # no employee => no row (logs of unknown employees are ignored)
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank INSIDE the department
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"kind": "aggregate", "collection": "attendance_logs", "hint": "date_-1_emp_code_1", "pipeline": pipeline}


def plan_department_trend(department: str, date_from: str, date_to: str) -> dict:
    start, end = parse_date(date_from, "from"), parse_date(date_to, "to")
    if end < start:
        raise unprocessable("to must not be before from")
    if (end - start).days + 1 > 92:
        raise unprocessable("range must not exceed 92 days")
    start_dt = DateTime(start.year, start.month, start.day, tzinfo=UTC)
    end_excl = DateTime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    pipeline = [
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "let": {"c": "$emp_code"},
            "pipeline": [
                {"$match": {"date": {"$gte": date_from, "$lte": date_to}, "$expr": {"$eq": ["$emp_code", "$$c"]}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
            ],
            "as": "logs"}},
        {"$unwind": "$logs"},
        {"$group": {
            "_id": "$logs.date",
            "present_halves": {"$sum": {"$cond": [{"$in": ["$logs.status", list(PRESENCE)]},
                                                  {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 1, 2]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one row for every calendar day in [from, to]  (upper bound is exclusive)
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_excl]}}},
        {"$addFields": {
            "present_halves": {"$ifNull": ["$present_halves", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "dow": {"$dayOfWeek": "$d"},
            "day": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
        }},
        {"$addFields": {"is_working_day": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}}},
        # R9 headcount for that day: department employees with joined_on <= day
        {"$lookup": {
            "from": "employees", "let": {"day": "$day"},
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                         {"$count": "n"}],
            "as": "hc"}},
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        # rate in integer 1e-4 units, half-up using integer arithmetic only:
        #   round_half_up(10000 * halves / (2 * headcount)) = floor((10000 * halves + headcount) / (2 * headcount))
        {"$addFields": {"rate_scaled": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$isNumber": "$rate_scaled"}, 1, 0]}}},
        # current row + up to 6 preceding rows, inside the requested range only
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "win_sum": {"$sum": "$rate_scaled", "window": {"documents": [-6, 0]}},
            "win_n": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {"ma_scaled": {"$cond": [
            {"$gt": ["$win_n", 0]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$win_sum", 2]}, "$win_n"]}, {"$multiply": [2, "$win_n"]}]}},
            None]}}},
        {"$project": {
            "_id": 0, "date": "$day", "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$present_halves", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$rate_scaled"}, {"$divide": ["$rate_scaled", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$isNumber": "$ma_scaled"}, {"$divide": ["$ma_scaled", 10000]}, None]},
        }},
        {"$sort": {"date": 1}},
    ]
    return {"kind": "aggregate", "collection": "employees", "hint": "department_1_joined_on_1", "pipeline": pipeline}


def run_plan(plan: dict) -> list:
    coll = db[plan["collection"]]
    if plan["kind"] == "find":
        cur = coll.find(plan["filter"], {"_id": 0}).sort(plan["sort"]).skip(plan["skip"]).limit(plan["limit"])
        return list(cur)
    return list(coll.aggregate(plan["pipeline"], hint=plan["hint"]))


def explain_plan(plan: dict) -> dict:
    if plan["kind"] == "find":
        cmd = {"find": plan["collection"], "filter": plan["filter"], "sort": dict(plan["sort"]),
               "skip": plan["skip"], "limit": plan["limit"], "projection": {"_id": 0}}
    else:
        cmd = {"aggregate": plan["collection"], "pipeline": plan["pipeline"], "cursor": {}, "hint": plan["hint"]}
    raw = db.command("explain", cmd, verbosity="executionStats")
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


# --------------------------------------------------------------------------- #
# 8. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


# ---- employees ----
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # unique index decides, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)  # counts the FILTERED set
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# ---- attendance ----
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive(body.status, ts, None, emp, day),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    # most recent record whose punch_in <= at. Records are walked newest-date-first through the
    # (emp_code, date) index; records without a punch_in (ABSENT/LEAVE) never satisfy $lte.
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": at}}, sort=[("date", -1)])
    if rec is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")
    start = rec["punch_in"]
    if at <= start:
        raise unprocessable("punched_at must be after punch_in")
    if at - start > timedelta(hours=24):
        raise unprocessable("punched_at is more than 24 hours after punch_in")
    d = derive(rec["status"], start, at, emp, rec["date"])
    # compare-and-set: succeeds only while the record is still open and its punch_in is unchanged
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": start},
        {"$set": {"punch_out": at, "work_hours": d["work_hours"],
                  "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(plan["filter"])
    return {"items": [record_out(d) for d in run_plan(plan)], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = get_employee(emp_code)
    day = parse_date(date).isoformat()
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(status_code=404, detail="no record for that date")

    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable("ABSENT/LEAVE clears punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        if new_in is None:
            raise unprocessable("a presence status requires a punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if attendance_date(new_in, emp) != day:
            raise unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            raise unprocessable("punch_out must be after punch_in and within 24 hours")

    old = {
        "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive(new_status, new_in, new_out, emp, day)}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not changes:
        raise unprocessable("request changes nothing")

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history = rec.get("history")
    # optimistic concurrency: only apply if the record is still exactly what we read
    guard = {"_id": rec["_id"], "status": old["status"], "punch_in": old["punch_in"],
             "punch_out": old["punch_out"], "history": {"$size": len(history)} if history is not None else None}
    if history is not None:
        update = {"$set": new, "$push": {"history": entry}}
    else:  # legacy document without a (usable) history array
        update = {"$set": {**new, "history": [entry]}}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(status_code=409, detail="record was modified concurrently; retry")
    return record_out(updated)


# ---- analytics ----
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee(emp_code)
    first, last = month_bounds(month)
    rows = run_plan(plan_employee_monthly(emp_code, month))
    m = rows[0] if rows else {}
    wd = working_days(first, last, Date.fromisoformat(emp["joined_on"]))
    halves = int(m.get("present_halves", 0))
    pct = float(half_up(Decimal(halves) / Decimal(2) * 100 / Decimal(wd), 2)) if wd else None
    return {
        "emp_code": emp_code, "month": month, "working_days": wd,
        "present_days": halves / 2, "leave_days": int(m.get("leave_count", 0)),
        "late_count": int(m.get("late_count", 0)), "total_late_minutes": int(m.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(m.get("total_overtime_minutes", 0)), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in run_plan(plan_department_summary(month, department)):
        n = int(r["wh_n"])
        avg = float(half_up(Decimal(int(r["wh_cents"])) / (Decimal(n) * 100), 2)) if n else None
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": int(r["present_halves"]) / 2,
            "avg_work_hours": avg, "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    return {"month": month, "items": run_plan(plan_late_leaderboard(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    plan = plan_department_trend(department, from_, to)  # validates before the 404 check
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="unknown department")
    return {"department": department, "items": run_plan(plan)}


# ---- admin ----
EXPLAINABLE = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: EXPLAINABLE,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**params):
        missing = [k for k, v in params.items() if v is None]
        if missing:
            raise unprocessable(f"missing required parameter(s): {', '.join(missing)}")

    if endpoint == "attendance_list":
        plan = plan_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        plan = plan_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        need(month=month)
        plan = plan_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        need(month=month)
        plan = plan_late_leaderboard(month, limit, department)
    else:
        need(department=department, **{"from": from_, "to": to})
        plan = plan_department_trend(department, from_, to)
    return {"endpoint": endpoint, "collection": plan["collection"], "explain": explain_plan(plan)}
