# Employee Attendance & Analytics API

A high-performance backend attendance and analytics service built with **Python 3.11+**, **FastAPI**, and **MongoDB 7.0+**. The service handles employee management, race-proof punch-in and punch-out operations, manual regularizations with complete audit trails, and analytical aggregation pipelines.

---

## 1. Technology Stack

* **Language:** Python 3.11+
* **Framework:** FastAPI
* **Database:** MongoDB 6.0+ (Tested on MongoDB 7.0.14)
* **Driver:** PyMongo 4.7+
* **Validation & Settings:** Pydantic v2
* **Server:** Uvicorn
* **Configuration:** python-dotenv
* **Testing:** Pytest, HTTPX

---

## 2. API Endpoints

### System
* `GET /health`: Readiness probe returning HTTP 200 `{"status": "ok"}` on successful database ping, or HTTP 503 when MongoDB is unreachable.

### Employees
* `POST /employees`: Register a new employee with unique `emp_code`, schedule validation, and epoch millisecond timestamps (HTTP 201 on success, 409 on duplicate, 422 on validation error).
* `GET /employees`: Paginated employee listing sorted by `emp_code` ascending with exact department filtering.

### Attendance
* `POST /attendance/punch-in`: Records punch-in for an employee with IST timezone conversion, overnight shift handling, 10-minute grace period calculation, and duplicate key concurrency protection (HTTP 201 on success, 404 for unknown employee, 409 for duplicate date).
* `POST /attendance/punch-out`: Atomically closes the latest open attendance record, calculates work hours with decimal half-up rounding (R4), evaluates 30-minute overtime thresholds (R3), and determines half-day status (R5) (HTTP 200 on success, 404 if not found, 409 if already punched out).
* `GET /attendance`: Paginated attendance history sorted by date descending and `emp_code` ascending with date range and status filters.
* `PATCH /attendance/{emp_code}/{date}`: Regularizes a single day's attendance record with optimistic concurrency control, recomputed derived values, and append-only audit history.

### Analytics & Reporting
* `GET /analytics/employees/{emp_code}/monthly`: Aggregated monthly summary computing working days (Mon-Fri with mid-month joiner handling), present days (0.5 for half-days), leave count, late count, and attendance percentage.
* `GET /analytics/departments/summary`: Cross-collection aggregation across `employees` and `attendance_logs` calculating department headcount (including employees with zero logs), total present days, average work hours over individual records, and late/leave totals.
* `GET /analytics/leaderboard/late`: Standard competition ranking (1, 2, 2, 4) of late-comers by total late minutes using MongoDB `$setWindowFields`, with limit applied after ranking and ties preserved.
* `GET /analytics/departments/{department}/trend`: Daily attendance series with `$densify` gap filling for missing dates and weekends, and 7-day moving average calculation.

### Admin
* `GET /admin/explain/{endpoint}`: Returns raw MongoDB `executionStats` and target collection for the main queries and aggregation pipelines of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, and `department_trend`.

---

## 3. Prerequisites

* **Python:** 3.11 or newer
* **MongoDB:** 6.0 or newer (local installation or MongoDB Atlas URI)

---

## 4. Installation & Local Setup

### Windows PowerShell

1. **Clone the repository and navigate to the project directory:**
   ```powershell
   cd "D:\New folder"
   ```

2. **Create and activate a virtual environment:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install dependencies:**
   ```powershell
   pip install -r requirements.txt pytest httpx
   ```

4. **Configure environment variables:**
   Create a `.env` file from `.env.example`:
   ```powershell
   Copy-Item .env.example .env
   ```
   *Real environment variables take precedence over `.env`.*
   * `MONGO_URI`: MongoDB connection string (default: `mongodb://localhost:27017`).
   * `MONGO_DB`: MongoDB database name (default: `attendance_db`).

5. **Seed sample data (optional):**
   ```powershell
   python sample_seed.py
   ```

6. **Start the application:**
   ```powershell
   uvicorn app.main:app --port 8000
   ```
   The application creates all required indexes automatically and idempotently on startup.

---

## 5. Running Automated Tests

Run the complete test suite with pytest:
```powershell
pytest -v
```

The test suite validates:
* Readiness health probe and MongoDB connectivity.
* Unique employee constraints and validation errors.
* Punch-in grace periods, overnight shifts, and duplicate prevention.
* Atomic punch-out, 24-hour window validation, and decimal half-up rounding.
* Attendance listing, date boundary validation, and pagination.
* Regularization audit history and optimistic locking.
* Monthly analytics, department summaries, and headcount calculation.
* Competition ranking with tied cutoff limits.
* Daily trend gap filling with `$densify` and moving averages.
* Explain plans and index usage (`IXSCAN`).
* Threaded concurrency simulation for simultaneous punch-in, punch-out, and regularization.

---

## 6. Example API Requests

### 1. Health Probe
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
```

### 2. Punch In
```powershell
$body = @{
    emp_code = "EMP0001"
    status = "PRESENT"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/attendance/punch-in" -Method Post -Body $body -ContentType "application/json"
```

### 3. Punch Out
```powershell
$body = @{
    emp_code = "EMP0001"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8000/attendance/punch-out" -Method Post -Body $body -ContentType "application/json"
```

### 4. Department Summary
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/analytics/departments/summary?month=2026-07"
```

### 5. Late Leaderboard
```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=5"
```

---

## 7. Key Architecture & Design Decisions

* **Database Indexing:** Compound indexes on `(emp_code, date)` and `(date, emp_code)` enforce uniqueness and provide index scans (`IXSCAN`) for pagination and aggregations.
* **Concurrency Protection:** Unique database indexes reject simultaneous punch-in attempts with HTTP 409. Punch-out uses atomic `find_one_and_update` filtering for `punch_out: null`. Manual corrections utilize history-length version checks to prevent concurrent write loss.
* **100% Database-Driven Analytics:** All summary, leaderboard, and trend calculations are executed directly within MongoDB aggregation stages (`$match`, `$lookup`, `$group`, `$densify`, and `$setWindowFields`) without loading unpaginated datasets into Python memory.
* **Accurate Financial Rounding:** Employs Python `decimal.Decimal` with `ROUND_HALF_UP` for all reported durations and ratios.

---

## 8. Known Limitations

* **Single Server Clock Dependency:** Creation timestamps and default punch times depend on the system clock. In a multi-region distributed cluster, network time protocol (NTP) synchronization is recommended.
* **Holiday Calendars:** Working day analytics currently assume standard Monday–Friday schedules in accordance with Rule R7; organization-specific holiday calendars are not factored into working day totals.
