"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from fastapi import FastAPI, HTTPException, status
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
# pyrefly: ignore [missing-import]
from pymongo import MongoClient
# pyrefly: ignore [missing-import]
from pymongo.errors import PyMongoError, DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1000)
db = client[os.getenv("MONGO_DB", "mongodb-local")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Unique index for employees (prevents concurrent duplicate emp_code)
    db.employees.create_index([("emp_code", 1)], unique=True)
    # 2. Indexes for employee list & department queries
    db.employees.create_index([("department", 1)])
    db.employees.create_index([("joined_on", 1)])
    # 3. Compound unique index for attendance (guarantees one record per (emp_code, date))
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    # 4. Sorting & pagination index for list attendance (/attendance)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # 5. Index for closing open punch-out (/attendance/punch-out)
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    # 6. Index for analytics queries
    db.attendance_logs.create_index([("date", 1), ("status", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1)])
    yield
    client.close()

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str,  date: str) -> int:
    h, m = map(int, shift_start.split(":"))
    y, mon, d = map(int, date.split("-"))
    start = datetime(y, mon, d, h, m, 0, tzinfo=IST)
    punch_in_ist = punch_in.astimezone(IST)
    grace = start + timedelta(minutes=10)
    if punch_in_ist > grace:
        return int((punch_in_ist - start).total_seconds() // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    diff = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = diff / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date: str, shift_start: str ) -> int:
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    y, mon, d = map(int, date.split("-"))
    end = datetime(y, mon, d, eh_h, eh_m, 0, tzinfo=IST)
    if (eh_h, eh_m) <= (sh_h, sh_m):
        end += timedelta(days=1)
    punch_out_ist = punch_out.astimezone(IST)
    minutes = int((punch_out_ist - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0

def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50

def parse_epoch_millis(ms: Optional[int]) -> datetime:
    if ms is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(ms, bool) or not isinstance(ms, int) or ms < 100_000_000_000 or ms > 4_102_444_800_000:
        raise HTTPException(422, "Invalid epoch milliseconds")
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)

def to_epoch_millis(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
    
def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    dt_ist = punch_in_utc.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    if (eh_h, eh_m) <= (sh_h, sh_m) and (dt_ist.hour, dt_ist.minute, dt_ist.second) < (eh_h, eh_m, 0):
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

@field_validator("emp_code")
@classmethod
def validate_emp_code(cls, v: str) -> str:
    if not re.match(r"^EMP\d{4,6}$", v):
        raise ValueError("emp_code must match format")


@field_validator("email")
@classmethod
def validate_email(cls, v: str) -> str:
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
        raise ValueError("Invalid email format")
    return v

@field_validator("shift_end")
@classmethod
def validate_shifts(cls, v: str, info) -> str:
    shift_start = info.data.get("shift_start")
    if shift_start and v == shift_start:
        raise ValueError("shift_start must differ from shift_end")
    return v

@field_validator
@classmethod
def validate_status(cls, v: str) -> str:
    if v not in ("PRESENT", "WFM", "ON_DUTY"):
        raise ValueError("status must be PRESENT, WFM, or ON_DUTY")
    return v


# -------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_millis(now_utc),
    }


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")

    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = []
    for emp in cursor:
        emp["created_at"] = to_epoch_millis(emp.get("created_at"))
        items.append(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be greater than date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, "Invalid status")
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = []
    for rec in cursor:
        rec["punch_in"] = to_epoch_millis(rec.get("punch_in"))
        rec["punch_out"] = to_epoch_millis(rec.get("punch_out"))
        rec["late_minutes"] = rec.get("late_minutes", 0)
        rec["overtime_minutes"] = rec.get("overtime_minutes", 0)
        rec["half_day"] = rec.get("half_day", False)
        
        # Serialize history instants if any exist
        history_list = []
        for h in rec.get("history", []):
            formatted_entry = {
                "at": to_epoch_millis(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {},
            }
            for k, change in h.get("changes", {}).items():
                formatted_entry["changes"][k] = {
                    "from": to_epoch_millis(change.get("from")) if k in ("punch_in", "punch_out") else change.get("from"),
                    "to": to_epoch_millis(change.get("to")) if k in ("punch_in", "punch_out") else change.get("to"),
                }
            history_list.append(formatted_entry)
            
        rec["history"] = history_list
        items.append(rec)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
