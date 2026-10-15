"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, fallback to .env)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Load .env without overriding existing environment variables
load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Database Initialization & Idempotent Indexes
# --------------------------------------------------------------------------- #
def init_db(database=db):
    """
    Creates all required indexes idempotently at startup.
    Safe against re-runs and name conflicts.
    """
    try:
        # 1. employees indexes
        emp_indexes = database.employees.index_information()
        if "idx_emp_code_unique" not in emp_indexes:
            database.employees.create_index(
                [("emp_code", ASCENDING)],
                name="idx_emp_code_unique",
                unique=True
            )
        if "idx_emp_dept_code" not in emp_indexes:
            database.employees.create_index(
                [("department", ASCENDING), ("emp_code", ASCENDING)],
                name="idx_emp_dept_code"
            )
        if "idx_emp_joined_dept" not in emp_indexes:
            database.employees.create_index(
                [("joined_on", ASCENDING), ("department", ASCENDING)],
                name="idx_emp_joined_dept"
            )

        # 2. attendance_logs indexes
        att_indexes = database.attendance_logs.index_information()
        if "idx_att_emp_date_unique" not in att_indexes:
            database.attendance_logs.create_index(
                [("emp_code", ASCENDING), ("date", ASCENDING)],
                name="idx_att_emp_date_unique",
                unique=True
            )
        if "idx_att_date_emp" not in att_indexes:
            database.attendance_logs.create_index(
                [("date", DESCENDING), ("emp_code", ASCENDING)],
                name="idx_att_date_emp"
            )
        if "idx_att_emp_date_desc" not in att_indexes:
            database.attendance_logs.create_index(
                [("emp_code", ASCENDING), ("date", DESCENDING)],
                name="idx_att_emp_date_desc"
            )
        if "idx_att_emp_punch_in_desc" not in att_indexes:
            database.attendance_logs.create_index(
                [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
                name="idx_att_emp_punch_in_desc"
            )
        if "idx_att_date_late_emp" not in att_indexes:
            database.attendance_logs.create_index(
                [("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)],
                name="idx_att_date_late_emp"
            )
        if "idx_att_status_date" not in att_indexes:
            database.attendance_logs.create_index(
                [("status", ASCENDING), ("date", DESCENDING)],
                name="idx_att_status_date"
            )
    except Exception as e:
        # Graceful startup logging if DB not yet reachable
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Business Rules & Helper Functions
# --------------------------------------------------------------------------- #
def round_half_up(value: Optional[float | Decimal], decimals: int = 2) -> Optional[float]:
    """R8 Rounding: exact half-up rounding (not banker's rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def parse_epoch_ms(ms: int) -> datetime:
    """Converts epoch milliseconds to a UTC datetime truncated to whole seconds."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Returns True if shift_end <= shift_start (e.g. 22:00 - 06:00)."""
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    return (eh, em) <= (sh, sm)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Converts punch_in to IST. If overnight shift and IST time < shift_end,
    attendance date is the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        eh, em = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(eh, em):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2 Late calculation.
    Grace period is 10 minutes. Late only if punch_in is strictly more than 10:00
    after shift_start. When late, floor minutes since shift_start, else 0.
    """
    d = datetime.fromisoformat(attendance_date).date()
    sh, sm = map(int, shift_start.split(":"))
    shift_start_ist = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST).replace(microsecond=0)

    diff_sec = (punch_in_ist - shift_start_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    in_sec = int(punch_in_utc.replace(microsecond=0).timestamp())
    out_sec = int(punch_out_utc.replace(microsecond=0).timestamp())
    seconds = Decimal(out_sec - in_sec)
    hours = seconds / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) between shift_end and punch_out, counted only when >= 30, else 0.
    Overnight shifts end on the next calendar day.
    """
    d = datetime.fromisoformat(attendance_date).date()
    eh, em = map(int, shift_end.split(":"))
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date = d + timedelta(days=1)
    else:
        shift_end_date = d

    shift_end_ist = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST).replace(microsecond=0)

    diff_sec = (punch_out_ist - shift_end_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def format_attendance_doc(doc: dict) -> dict:
    """Formats an attendance MongoDB document into the AttendanceRecord schema."""
    history_out = []
    for h in doc.get("history", []):
        entry = {
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {}
        }
        for k, v in h.get("changes", {}).items():
            entry["changes"][k] = {
                "from": to_epoch_ms(v["from"]) if isinstance(v.get("from"), datetime) else v.get("from"),
                "to": to_epoch_ms(v["to"]) if isinstance(v.get("to"), datetime) else v.get("to")
            }
        history_out.append(entry)

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out
    }


def clean_mongo_obj(obj: Any) -> Any:
    """Recursively converts BSON datetimes/ObjectIds to JSON serializable objects."""
    if isinstance(obj, dict):
        return {k: clean_mongo_obj(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_mongo_obj(i) for i in obj]
    elif isinstance(obj, datetime):
        return to_epoch_ms(obj)
    elif hasattr(obj, "__str__") and not isinstance(obj, (int, float, bool, str, type(None))):
        return str(obj)
    return obj


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(ge=100000000000, le=4102444800000, strict=True)]


class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, response_model=Employee, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Creates an employee. Unique emp_code enforced by database unique index.
    Returns 409 on duplicate, 422 on validation error.
    """
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    doc["created_at"] = to_epoch_ms(now_utc)
    doc.pop("_id", None)
    return doc


@app.get("/employees", response_model=EmployeePage, tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists employees sorted by emp_code ascending.
    Total counts filtered set, not entire collection.
    """
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)

    items = []
    for doc in cursor:
        doc["created_at"] = to_epoch_ms(doc.get("created_at"))
        items.append(doc)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, response_model=AttendanceRecord, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Creates attendance record for the day.
    Returns 404 for unknown employee, 409 if already punched in for the date.
    Atomic race protection via unique index on (emp_code, date).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = parse_epoch_ms(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    attendance_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_utc, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status.value,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance record already exists for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, response_model=AttendanceRecord, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes the most recent open record whose punch_in is <= punched_at.
    Handles overnight shifts across midnight.
    Returns 404 if no punch-in found, 409 if already punched out, 422 if invalid timestamp.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = parse_epoch_ms(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    # Find the most recent record whose punch_in is at or before punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)]
    )

    if not record:
        raise HTTPException(status_code=404, detail="No eligible punch-in record found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    elapsed_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if elapsed_sec <= 0 or elapsed_sec > 86400:
        raise HTTPException(
            status_code=422,
            detail="Invalid punch-out time: must be after punch-in and within 24 hours"
        )

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_mins = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    is_half_day = (work_hrs < 4.50)

    # Atomic update to protect against simultaneous punch-out races
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_mins,
                "half_day": is_half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out by concurrent request")

    return format_attendance_doc(updated)


@app.get("/attendance", response_model=AttendancePage, tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Status] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists attendance records sorted by date desc, emp_code asc.
    Uses database filtering, indexing, and pagination.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status.value
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    """
    Manually regularizes an attendance record.
    Recomputes derived values, appends one history entry with actual changes,
    and protects against concurrent overwrite.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    body_dict = body.model_dump(exclude_unset=True)

    # Rule: setting status ABSENT or LEAVE clears both punch times,
    # so supplying punch_in or punch_out with it is 422
    if body.status in (Status.ABSENT, Status.LEAVE):
        if "punch_in" in body_dict or "punch_out" in body_dict:
            raise HTTPException(
                status_code=422,
                detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE"
            )

    new_status = body.status.value if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status
        if body.punch_in is not None:
            new_punch_in = parse_epoch_ms(body.punch_in)
        elif "punch_in" in body_dict and body.punch_in is None:
            new_punch_in = None
        else:
            new_punch_in = record.get("punch_in")
            if new_punch_in and new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        if body.punch_out is not None:
            new_punch_out = parse_epoch_ms(body.punch_out)
        elif "punch_out" in body_dict and body.punch_out is None:
            new_punch_out = None
        else:
            new_punch_out = record.get("punch_out")
            if new_punch_out and new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

        # Rule: a presence status requires a punch_in (supplied or already stored)
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a valid punch_in")

        # Rule: punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in falls on attendance date {calc_date}, does not match record date {date}"
            )

        # Rule: punch_out must be after punch_in and within 24 hours
        if new_punch_out is not None:
            elapsed = (new_punch_out - new_punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")

    # Recompute derived fields
    if new_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = (new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute changes
    old_punch_in = record.get("punch_in")
    if old_punch_in and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    old_status = record.get("status")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes = {}
    if old_status != new_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if old_punch_in != new_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if old_punch_out != new_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # Rule: a request that changes nothing is 422
    if not changes:
        raise HTTPException(status_code=422, detail="No changes detected in regularization request")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency control: match history length to prevent lost updates
    existing_history_len = len(record.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, existing_history_len]}
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Concurrent regularization conflict; record was updated by another request"
        )

    return format_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly, tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """
    Monthly attendance summary for one employee backed by MongoDB aggregation.
    Calculates working_days, present_days, leave_days, late_count, and attendance_pct.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    # Calculate working days (Mon-Fri) from max(month_start, joined_on) to month_end
    if emp["joined_on"] > month_end:
        working_days = 0
    else:
        start_date = max(datetime(year, m, 1).date(), datetime.fromisoformat(emp["joined_on"]).date())
        end_date = datetime(year, m, num_days).date()
        working_days = sum(
            1 for d in range((end_date - start_date).days + 1)
            if (start_date + timedelta(days=d)).weekday() < 5
        )

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": month_start, "$lte": month_end}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                            ]
                        },
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"}
        }}
    ]

    res = list(db.attendance_logs.aggregate(pipeline))
    if res:
        data = res[0]
    else:
        data = {
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up((data["present_days"] / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(data["present_days"]),
        "leave_days": int(data["leave_days"]),
        "late_count": int(data["late_count"]),
        "total_late_minutes": int(data["total_late_minutes"]),
        "total_overtime_minutes": int(data["total_overtime_minutes"]),
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary, tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    """
    Department summary for a month via a cross-collection MongoDB aggregation pipeline.
    Headcount includes eligible employees with zero logs.
    Average work hours computed over all individual present-status records with non-null work_hours.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", month_start]},
                            {"$lte": ["$date", month_end]}
                        ]
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$project": {
            "department": 1,
            "emp_code": 1,
            "present_days": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]}
                                    ]
                                },
                                {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                0.0
                            ]
                        }
                    }
                }
            },
            "late_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                    }
                }
            },
            "total_late_minutes": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$ifNull": ["$$l.late_minutes", 0]}
                    }
                }
            },
            "leave_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                    }
                }
            },
            "on_duty_count": {
                "$sum": {
                    "$map": {
                        "input": "$logs",
                        "as": "l",
                        "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                    }
                }
            },
            "work_hours_list": {
                "$filter": {
                    "input": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]}
                                        ]
                                    },
                                    "$$l.work_hours",
                                    None
                                ]
                            }
                        }
                    },
                    "as": "wh",
                    "cond": {"$ne": ["$$wh", None]}
                }
            }
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
            "all_work_hours": {
                "$push": "$work_hours_list"
            }
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "flattened_wh": {
                "$reduce": {
                    "input": "$all_work_hours",
                    "initialValue": [],
                    "in": {"$concatArrays": ["$$value", "$$this"]}
                }
            }
        }},
        {"$project": {
            "department": 1,
            "headcount": 1,
            "present_days": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$flattened_wh"}, 0]},
                    {"$round": [{"$avg": "$flattened_wh"}, 2]},
                    None
                ]
            }
        }},
        {"$sort": {"department": ASCENDING}}
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard, tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    """
    Ranks employees by total late_minutes in the month (only total > 0).
    Uses standard competition ranking (1, 2, 2, 4) in MongoDB aggregation.
    Applies limit after ranking; orders by total_late_minutes desc, emp_code asc.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline = [
        {"$match": {
            "date": {"$gte": month_start, "$lte": month_end},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=Trend, tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    """
    Daily attendance trend for a department with gap-filling ($densify)
    and 7-day moving average calculated in MongoDB.
    """
    start_dt = datetime.fromisoformat(from_).date()
    end_dt = datetime.fromisoformat(to).date()

    if end_dt < start_dt:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
    if (end_dt - start_dt).days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    # 404 for unknown department (no employees)
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
    to_dt = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)

    pipeline = [
        # 1. Match attendance logs in date range
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        # 2. Join employees to filter by department
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # 3. Group by date
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {
                    "$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {"$gt": ["$late_minutes", 0]},
                        1,
                        0
                    ]
                }
            }
        }},
        # 4. Convert date string to date_dt
        {"$project": {
            "_id": 0,
            "date_str": "$_id",
            "date_dt": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1
        }},
        # 5. Union with a boundary document to ensure at least from_dt exists
        {"$unionWith": {
            "coll": "attendance_logs",
            "pipeline": [
                {"$limit": 1},
                {"$project": {
                    "_id": 0,
                    "date_str": {"$literal": from_},
                    "date_dt": {"$literal": from_dt},
                    "present_count": {"$literal": 0.0},
                    "late_count": {"$literal": 0}
                }}
            ]
        }},
        # 6. Re-group in case union created duplicate for from_
        {"$group": {
            "_id": "$date_dt",
            "date_str": {"$first": "$date_str"},
            "present_count": {"$sum": "$present_count"},
            "late_count": {"$sum": "$late_count"}
        }},
        {"$project": {
            "_id": 0,
            "date_dt": "$_id",
            "date_str": 1,
            "present_count": 1,
            "late_count": 1
        }},
        # 7. Densify to fill all missing days in the range
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt + timedelta(days=1)]
            }
        }},
        # 8. Set fields
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "date_dt": 1,
            "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
            "present_count": {"$ifNull": ["$present_count", 0.0]},
            "late_count": {"$ifNull": ["$late_count", 0]}
        }},
        # 9. Lookup headcount on each calendar day
        {"$lookup": {
            "from": "employees",
            "let": {"cur_date": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$cur_date"]}
                        ]
                    }
                }},
                {"$count": "cnt"}
            ],
            "as": "hc"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$first": "$hc.cnt"}, 0]}
        }},
        # 10. attendance_rate
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                    None
                ]
            }
        }},
        # 11. 7-day moving average
        {"$setWindowFields": {
            "sortBy": {"date_dt": ASCENDING},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {
                "$cond": [
                    {"$ne": ["$moving_avg_7d", None]},
                    {"$round": ["$moving_avg_7d", 4]},
                    None
                ]
            }
        }},
        {"$sort": {"date": ASCENDING}}
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse, tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern="^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Status] = Query(None),
    from_: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Runs MongoDB explain with executionStats for the named endpoint's main query or aggregation pipeline.
    Validates required query parameters for each endpoint.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status.value
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        exp = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_obj(exp)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }},
            {"$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }}
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_obj(exp)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        match_emp = {"joined_on": {"$lte": month_end}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$emp_code", "$$emp"]},
                                {"$gte": ["$date", month_start]},
                                {"$lte": ["$date", month_end]}
                            ]
                        }
                    }}
                ],
                "as": "logs"
            }},
            {"$project": {
                "department": 1,
                "emp_code": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [2, 3, 4, 5, 6]]}
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": {
                            "$map": {
                                "input": "$logs",
                                "as": "l",
                                "in": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$$l.work_hours", None]}
                                            ]
                                        },
                                        "$$l.work_hours",
                                        None
                                    ]
                                }
                            }
                        },
                        "as": "wh",
                        "cond": {"$ne": ["$$wh", None]}
                    }
                }
            }},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "all_work_hours": {"$push": "$work_hours_list"}
            }},
            {"$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "flattened_wh": {
                    "$reduce": {
                        "input": "$all_work_hours",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }},
            {"$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$flattened_wh"}, 0]},
                        {"$round": [{"$avg": "$flattened_wh"}, 2]},
                        None
                    ]
                }
            }},
            {"$sort": {"department": ASCENDING}}
        ]
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_mongo_obj(exp)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        month_start = f"{month}-01"
        month_end = f"{month}-{num_days:02d}"

        pipeline = [
            {"$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
        ]

        if department:
            pipeline.append({"$match": {"emp.department": department}})

        pipeline.extend([
            {"$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }},
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}}
        ])
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_obj(exp)}

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")

        start_dt = datetime.fromisoformat(from_).date()
        end_dt = datetime.fromisoformat(to).date()

        if end_dt < start_dt:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")
        if (end_dt - start_dt).days > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        from_dt = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
        to_dt = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)

        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                }
            }},
            {"$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }},
            {"$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {"$limit": 1},
                    {"$project": {
                        "_id": 0,
                        "date_str": {"$literal": from_},
                        "date_dt": {"$literal": from_dt},
                        "present_count": {"$literal": 0.0},
                        "late_count": {"$literal": 0}
                    }}
                ]
            }},
            {"$group": {
                "_id": "$date_dt",
                "date_str": {"$first": "$date_str"},
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"}
            }},
            {"$project": {
                "_id": 0,
                "date_dt": "$_id",
                "date_str": 1,
                "present_count": 1,
                "late_count": 1
            }},
            {"$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt + timedelta(days=1)]
                }
            }},
            {"$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "date_dt": 1,
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$department", department]},
                                {"$lte": ["$joined_on", "$$cur_date"]}
                            ]
                        }
                    }},
                    {"$count": "cnt"}
                ],
                "as": "hc"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$first": "$hc.cnt"}, 0]}
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"date_dt": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$round": ["$moving_avg_7d", 4]},
                        None
                    ]
                }
            }},
            {"$sort": {"date": ASCENDING}}
        ]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_mongo_obj(exp)}

    raise HTTPException(status_code=422, detail="Unsupported endpoint for explain")
