# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees(emp_code)` and `attendance_logs(emp_code, date)` to enforce data integrity and prevent race conditions. For query speed, I created `attendance_logs(date: -1, emp_code: 1)` for fast pagination, `attendance_logs(emp_code: 1, punch_in: -1)` for quick punch-out lookups, `attendance_logs(date: 1, late_minutes: -1, emp_code: 1)` for leaderboard ranking, and `employees(joined_on: 1, department: 1)` for headcount lookups. I considered an index on `attendance_logs(status)` but rejected it due to low cardinality (only 5 status values), which offers poor index selectivity.

2. **Punch-in race.**
Both concurrent requests validate the employee and calculate the same attendance date. Both attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` acts as the atomic barrier: one insert succeeds and returns HTTP 201, while the duplicate immediately raises `DuplicateKeyError`, which the API catches and returns as HTTP 409.

3. **Ties.**
The pipeline uses `$setWindowFields` with `$rank: {}` to compute competition ranking (1, 2, 2, 4). The `limit` filter (`rank <= limit`) applies after ranking, so all employees tied at or below the cutoff are returned (even if total items exceed `limit`), deterministically ordered by `emp_code` ascending.

4. **Headcount.**
The department summary pipeline queries `employees` first, filtering by `joined_on <= month_end` and computing headcount with `$sum: 1`. It then joins `attendance_logs` using `$lookup`. Employees with zero attendance logs are therefore preserved and fully counted.

5. **One thing you would change for 100x data.**
I would shard `attendance_logs` using a compound key on `{emp_code: "hashed", date: 1}` to distribute write throughput across nodes, and compute department summaries incrementally via scheduled daily rollups or MongoDB change streams.
