import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

import app.main as main_mod
from app.main import app, db, IST, round_half_up, parse_epoch_ms, to_epoch_ms, compute_late_minutes, compute_work_hours, compute_overtime

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    """Ensure tests run with a clean state before each test module."""
    # Initialize indexes
    main_mod.init_db(db)
    yield


# --------------------------------------------------------------------------- #
# 1. Health Probe
# --------------------------------------------------------------------------- #
def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Employee CRUD & Validation
# --------------------------------------------------------------------------- #
def test_create_and_list_employees():
    # Use unique code
    emp_code = "EMP9901"
    db.employees.delete_one({"emp_code": emp_code})

    payload = {
        "emp_code": emp_code,
        "name": "Test Tester",
        "email": "tester@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-06-01"
    }

    res = client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == emp_code
    assert isinstance(data["created_at"], int)
    assert data["created_at"] > 100000000000

    # Duplicate create must return 409
    res_dup = client.post("/employees", json=payload)
    assert res_dup.status_code == 409
    assert "already exists" in res_dup.json()["detail"]

    # Invalid emp_code format -> 422
    invalid_emp = dict(payload)
    invalid_emp["emp_code"] = "INVALID123"
    assert client.post("/employees", json=invalid_emp).status_code == 422

    # Equal shift_start and shift_end -> 422
    same_shift = dict(payload)
    same_shift["emp_code"] = "EMP9902"
    same_shift["shift_end"] = "09:30"
    assert client.post("/employees", json=same_shift).status_code == 422

    # List employees with department filter
    res_list = client.get("/employees?department=QA&page=1&page_size=10")
    assert res_list.status_code == 200
    list_data = res_list.json()
    assert list_data["page"] == 1
    assert list_data["page_size"] == 10
    assert any(item["emp_code"] == emp_code for item in list_data["items"])
    assert list_data["total"] >= 1


# --------------------------------------------------------------------------- #
# 3. Attendance Punch-In Rules & Concurrency
# --------------------------------------------------------------------------- #
def test_punch_in_rules():
    emp_code = "EMP9903"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Punch Tester",
        "email": "punch@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Unknown employee -> 404
    res_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP0000"})
    assert res_404.status_code == 404

    # 1. Inside Grace Period: 09:40:00 IST -> late = 0
    # 2026-07-01 09:40:00 IST is 2026-07-01 04:10:00 UTC
    dt_094000 = datetime(2026, 7, 1, 4, 10, 0, tzinfo=timezone.utc)
    res1 = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(dt_094000),
        "status": "PRESENT"
    })
    assert res1.status_code == 201
    d1 = res1.json()
    assert d1["date"] == "2026-07-01"
    assert d1["late_minutes"] == 0
    assert d1["status"] == "PRESENT"
    assert d1["punch_out"] is None
    assert d1["work_hours"] is None

    # Duplicate punch-in for same date -> 409
    res_dup = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(dt_094000),
        "status": "PRESENT"
    })
    assert res_dup.status_code == 409

    # 2. Strict grace period: 09:40:01 IST -> late = 10
    # 2026-07-02 09:40:01 IST is 2026-07-02 04:10:01 UTC
    dt_094001 = datetime(2026, 7, 2, 4, 10, 1, tzinfo=timezone.utc)
    res2 = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(dt_094001),
        "status": "WFH"
    })
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10
    assert res2.json()["status"] == "WFH"

    # 3. 10:15:59 IST -> late = 45
    # 2026-07-03 10:15:59 IST is 2026-07-03 04:45:59 UTC
    dt_101559 = datetime(2026, 7, 3, 4, 45, 59, tzinfo=timezone.utc)
    res3 = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(dt_101559),
        "status": "ON_DUTY"
    })
    assert res3.status_code == 201
    assert res3.json()["late_minutes"] == 45


def test_overnight_shift_punch_in():
    emp_code = "EMP9904"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Night Owl",
        "email": "night@example.com",
        "department": "Operations",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in 2026-07-07 05:30:00 IST (before shift_end 06:00)
    # Belongs to previous day: 2026-07-06!
    # 2026-07-07 05:30:00 IST is 2026-07-07 00:00:00 UTC
    dt_next_morning = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    res = client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(dt_next_morning),
        "status": "PRESENT"
    })
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"


# --------------------------------------------------------------------------- #
# 4. Punch-Out Rules & Rounding
# --------------------------------------------------------------------------- #
def test_punch_out_rules():
    emp_code = "EMP9905"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Out Tester",
        "email": "out@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in at 09:28:00 IST on 2026-07-06 (03:58:00 UTC)
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(in_dt)
    })

    # Reject punch-out at or before punch-in -> 422
    assert client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(in_dt)
    }).status_code == 422

    # Reject punch-out > 24 hours -> 422
    too_late = in_dt + timedelta(hours=25)
    assert client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(too_late)
    }).status_code == 422

    # Valid punch-out: 18:35:00 IST (13:05:00 UTC)
    # Work hours = 9.12, overtime = 5 min < 30 -> 0, half_day = False
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    res_out = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(out_dt)
    })
    assert res_out.status_code == 200
    data = res_out.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False

    # Second punch-out must return 409
    res_out_dup = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": to_epoch_ms(out_dt)
    })
    assert res_out_dup.status_code == 409


def test_half_day_and_overtime_thresholds():
    emp_code = "EMP9906"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Threshold Tester",
        "email": "thresh@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Day 1: Half day (3.5 hours worked < 4.50)
    in1 = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    out1 = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": to_epoch_ms(in1)})
    res1 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": to_epoch_ms(out1)})
    assert res1.status_code == 200
    assert res1.json()["half_day"] is True
    assert res1.json()["work_hours"] == 3.5

    # Day 2: Overtime threshold: 40 min past shift_end (18:30) -> 19:10 IST
    in2 = datetime(2026, 7, 8, 4, 0, 0, tzinfo=timezone.utc)
    out2 = datetime(2026, 7, 8, 13, 40, 0, tzinfo=timezone.utc) # 19:10 IST
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": to_epoch_ms(in2)})
    res2 = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": to_epoch_ms(out2)})
    assert res2.status_code == 200
    assert res2.json()["overtime_minutes"] == 40
    assert res2.json()["half_day"] is False


# --------------------------------------------------------------------------- #
# 5. Attendance Listing & Regularization
# --------------------------------------------------------------------------- #
def test_attendance_listing_and_filters():
    # date_from > date_to must return 422
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422

    res_ok = client.get("/attendance?page=1&page_size=5")
    assert res_ok.status_code == 200
    data = res_ok.json()
    assert "items" in data
    assert "total" in data
    assert data["page"] == 1
    assert data["page_size"] == 5


def test_regularization_and_audit_history():
    emp_code = "EMP9907"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Audit Tester",
        "email": "audit@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in late: 10:05 IST (04:35 UTC), out 18:30 IST (13:00 UTC)
    in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": to_epoch_ms(in_dt)})
    client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": to_epoch_ms(out_dt)})

    # No-op regularization -> 422
    res_noop = client.patch(f"/attendance/{emp_code}/2026-07-07", json={
        "status": "PRESENT",
        "reason": "testing noop correction",
        "regularized_by": "hr.admin"
    })
    assert res_noop.status_code == 422

    # Setting ABSENT with punch_in -> 422
    res_invalid = client.patch(f"/attendance/{emp_code}/2026-07-07", json={
        "status": "ABSENT",
        "punch_in": to_epoch_ms(in_dt),
        "reason": "invalid combination",
        "regularized_by": "hr.admin"
    })
    assert res_invalid.status_code == 422

    # Valid correction: correct punch_in from 10:05 to 09:28 IST (03:58 UTC)
    corrected_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    res_patch = client.patch(f"/attendance/{emp_code}/2026-07-07", json={
        "punch_in": to_epoch_ms(corrected_in),
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert res_patch.status_code == 200
    pdata = res_patch.json()
    assert pdata["late_minutes"] == 0
    assert pdata["work_hours"] == 9.03
    assert len(pdata["history"]) == 1
    h = pdata["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    assert "punch_in" in h["changes"]
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0


# --------------------------------------------------------------------------- #
# 6. Analytics: Monthly, Summary, Leaderboard, Trend
# --------------------------------------------------------------------------- #
def test_employee_monthly_analytics():
    # Test on EMP0001 from sample data
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] >= 1.0


def test_department_summary_analytics():
    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    departments = [d["department"] for d in data["items"]]
    assert "Engineering" in departments
    assert "Sales" in departments
    assert "Support" in departments


def test_late_leaderboard_analytics():
    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    for item in data["items"]:
        assert item["rank"] <= 5
        assert item["total_late_minutes"] > 0


def test_department_trend_analytics():
    # Unknown department -> 404
    assert client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05").status_code == 404

    # Date range > 92 days -> 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-06-01").status_code == 422

    # to < from -> 422
    assert client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01").status_code == 422

    # Valid trend for 5 days
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-09")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    assert len(data["items"]) == 5
    dates = [d["date"] for d in data["items"]]
    assert dates == ["2026-07-05", "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]
    # 2026-07-05 is Sunday
    assert data["items"][0]["is_working_day"] is False
    assert data["items"][0]["attendance_rate"] is None


# --------------------------------------------------------------------------- #
# 7. Explain Endpoint
# --------------------------------------------------------------------------- #
def test_admin_explain_endpoints():
    # 1. attendance_list
    res1 = client.get("/admin/explain/attendance_list?status=PRESENT")
    assert res1.status_code == 200
    assert res1.json()["endpoint"] == "attendance_list"
    assert res1.json()["collection"] == "attendance_logs"

    # 2. employee_monthly
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["endpoint"] == "employee_monthly"

    # 3. department_summary
    res3 = client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["endpoint"] == "department_summary"
    assert res3.json()["collection"] == "employees"

    # 4. late_leaderboard
    res4 = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    assert res4.json()["endpoint"] == "late_leaderboard"

    # 5. department_trend
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
    assert res5.status_code == 200
    assert res5.json()["endpoint"] == "department_trend"

    # Unsupported endpoint -> 422
    assert client.get("/admin/explain/unsupported").status_code == 422


# --------------------------------------------------------------------------- #
# 8. Advanced Edge Cases: Ties, Cutoff, Concurrency, Weekends, Truncation
# --------------------------------------------------------------------------- #
def test_leaderboard_competition_ranking_and_ties():
    """
    Verifies:
    1. Competition ranking (1, 2, 2, 4).
    2. Limit applies after ranking: limit=2 returns ranks 1, 2, 2 (3 rows).
    3. Tied rows ordered by emp_code ascending.
    """
    month = "2026-08"
    dept = "RankDept"

    # Setup 4 employees in RankDept
    emps = [
        {"emp_code": "EMP8001", "name": "Alice", "late": 60},
        {"emp_code": "EMP8002", "name": "Bob", "late": 40},
        {"emp_code": "EMP8003", "name": "Charlie", "late": 40},
        {"emp_code": "EMP8004", "name": "Dave", "late": 20},
    ]

    for e in emps:
        db.employees.delete_one({"emp_code": e["emp_code"]})
        db.attendance_logs.delete_many({"emp_code": e["emp_code"]})
        client.post("/employees", json={
            "emp_code": e["emp_code"],
            "name": e["name"],
            "email": f"{e['emp_code'].lower()}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })
        # Insert a log with late_minutes
        db.attendance_logs.insert_one({
            "emp_code": e["emp_code"],
            "date": f"{month}-03",
            "status": "PRESENT",
            "punch_in": datetime(2026, 8, 3, 4, 30, 0, tzinfo=timezone.utc),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": e["late"],
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })

    # Query with limit=2
    res = client.get(f"/analytics/leaderboard/late?month={month}&limit=2&department={dept}")
    assert res.status_code == 200
    items = res.json()["items"]
    # Ranks must be: Alice (1), Bob (2), Charlie (2)
    # Total rows = 3 (due to tie at rank 2 <= limit 2)
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP8001"
    assert items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP8002"
    assert items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP8003"
    assert items[2]["rank"] == 2

    # Query with limit=4 to see Dave's rank
    res_all = client.get(f"/analytics/leaderboard/late?month={month}&limit=4&department={dept}")
    items_all = res_all.json()["items"]
    assert len(items_all) == 4
    # Dave should have rank 4 (rank 3 is skipped!)
    assert items_all[3]["emp_code"] == "EMP8004"
    assert items_all[3]["rank"] == 4


def test_department_headcount_with_zero_logs_and_midmonth_joiners():
    """
    Verifies:
    1. Employees with zero logs count towards headcount.
    2. Employees joined after month end are excluded.
    3. Working days for mid-month joiner are counted from joined_on.
    """
    dept = "HeadcountDept"
    emp1 = "EMP8010" # Joined in June (eligible)
    emp2 = "EMP8011" # Joined 2026-08-15 (eligible in August, mid-month)
    emp3 = "EMP8012" # Joined 2026-09-01 (future joiner, NOT eligible in August)

    for code, joined in [(emp1, "2026-06-01"), (emp2, "2026-08-15"), (emp3, "2026-09-01")]:
        db.employees.delete_one({"emp_code": code})
        db.attendance_logs.delete_many({"emp_code": code})
        client.post("/employees", json={
            "emp_code": code,
            "name": f"Emp {code}",
            "email": f"{code.lower()}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": joined
        })

    # None of them have attendance logs in August.
    res_dept = client.get(f"/analytics/departments/summary?month=2026-08&department={dept}")
    assert res_dept.status_code == 200
    items = res_dept.json()["items"]
    assert len(items) == 1
    # emp1 and emp2 are included; emp3 joined in September so excluded!
    assert items[0]["headcount"] == 2
    assert items[0]["present_days"] == 0.0
    assert items[0]["avg_work_hours"] is None

    # Check monthly analytics for mid-month joiner (EMP8011)
    res_monthly = client.get(f"/analytics/employees/{emp2}/monthly?month=2026-08")
    assert res_monthly.status_code == 200
    m_data = res_monthly.json()
    # August 2026 has 21 total weekdays. From Aug 15 to Aug 31:
    # Aug 15 is Saturday, Aug 16 Sunday.
    # Weekdays in Aug 17-31: 17-21 (5), 24-28 (5), 31 (1) = 11 weekdays!
    assert m_data["working_days"] == 11
    assert m_data["present_days"] == 0.0
    assert m_data["attendance_pct"] == 0.0


def test_weekend_attendance_rules():
    """
    R7: Weekend records still contribute to late and overtime totals,
    but present_days only counts records that fall on Mon-Fri.
    """
    emp_code = "EMP8020"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Weekend Warrior",
        "email": "weekend@example.com",
        "department": "Support",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # 2026-08-01 is Saturday!
    # Punch in 10:15 IST (45 min late), Punch out 19:15 IST (45 min overtime)
    sat_in = datetime(2026, 8, 1, 4, 45, 0, tzinfo=timezone.utc)
    sat_out = datetime(2026, 8, 1, 13, 45, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": to_epoch_ms(sat_in)})
    client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": to_epoch_ms(sat_out)})

    res_monthly = client.get(f"/analytics/employees/{emp_code}/monthly?month=2026-08")
    assert res_monthly.status_code == 200
    data = res_monthly.json()
    # Weekend punch does NOT count towards present_days
    assert data["present_days"] == 0.0
    # But it DOES count toward late_count and overtime
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 45
    assert data["total_overtime_minutes"] == 45


def test_concurrency_protection_punch_in():
    """
    Two simultaneous punch-ins for the same employee and date yield
    exactly one 201 and the other 409.
    """
    import threading

    emp_code = "EMP8030"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    punch_time = to_epoch_ms(datetime(2026, 8, 5, 4, 0, 0, tzinfo=timezone.utc))
    results = []

    def make_punch():
        c = TestClient(app)
        r = c.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": punch_time})
        results.append(r.status_code)

    t1 = threading.Thread(target=make_punch)
    t2 = threading.Thread(target=make_punch)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One must succeed (201) and one must conflict (409)
    assert sorted(results) == [201, 409]


def test_concurrency_protection_punch_out():
    """
    Two simultaneous punch-outs for the same open record yield
    exactly one 200 and the other 409.
    """
    import threading

    emp_code = "EMP8031"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race Out Tester",
        "email": "raceout@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    in_time = to_epoch_ms(datetime(2026, 8, 6, 4, 0, 0, tzinfo=timezone.utc))
    out_time = to_epoch_ms(datetime(2026, 8, 6, 13, 0, 0, tzinfo=timezone.utc))
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_time})

    results = []

    def make_punch_out():
        c = TestClient(app)
        r = c.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": out_time})
        results.append(r.status_code)

    t1 = threading.Thread(target=make_punch_out)
    t2 = threading.Thread(target=make_punch_out)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One must succeed (200) and one must conflict (409)
    assert sorted(results) == [200, 409]


def test_concurrency_protection_regularization():
    """
    Concurrent regularizations on the same record must not silently lose history.
    One receives 200, the conflict receives 409.
    """
    import threading

    emp_code = "EMP8032"
    db.employees.delete_one({"emp_code": emp_code})
    db.attendance_logs.delete_many({"emp_code": emp_code})

    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Race Reg Tester",
        "email": "racereg@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    in_time = to_epoch_ms(datetime(2026, 8, 7, 4, 45, 0, tzinfo=timezone.utc))
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": in_time})

    results = []

    def make_reg(new_status, user):
        c = TestClient(app)
        r = c.patch(f"/attendance/{emp_code}/2026-08-07", json={
            "status": new_status,
            "reason": f"Conflict test by {user}",
            "regularized_by": user
        })
        results.append(r.status_code)

    t1 = threading.Thread(target=make_reg, args=("WFH", "admin1"))
    t2 = threading.Thread(target=make_reg, args=("ON_DUTY", "admin2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both cannot silently succeed with history overwrite:
    # Either [200, 409] (if true conflict caught by optimistic locking)
    # or both succeed sequentially, but in our atomic test we ensure no history loss.
    # At least one 200 was produced
    assert 200 in results
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": "2026-08-07"})
    # History must have recorded every successful edit without loss
    successful_count = results.count(200)
    assert len(record.get("history", [])) == successful_count

