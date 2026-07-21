import json
import os
from datetime import datetime

from werkzeug.security import generate_password_hash

from db import get_db_connection


JSON_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "storage",
    "swms_data.json",
)


def parse_join_date(value):
    if not value:
        return "2024-01-01"

    for date_format in ("%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return "2024-01-01"


def normalize_role(value):
    role = str(value or "employee").strip().lower()

    if role in ("administrator", "admin"):
        return "admin"

    if role == "manager":
        return "manager"

    return "employee"


def normalize_employment_status(value):
    status = str(value or "Active").strip().lower()

    mapping = {
        "active": "active",
        "on leave": "on_leave",
        "on_leave": "on_leave",
        "inactive": "inactive",
        "suspended": "suspended",
    }

    return mapping.get(status, "active")


def normalize_task_status(value):
    status = str(value or "Not Started").strip().lower()

    mapping = {
        "not started": "not_started",
        "not_started": "not_started",
        "in progress": "in_progress",
        "in_progress": "in_progress",
        "completed": "completed",
        "overdue": "overdue",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }

    return mapping.get(status, "not_started")


def normalize_priority(value):
    priority = str(value or "Medium").strip().lower()

    if priority not in ("low", "medium", "high", "urgent"):
        return "medium"

    return priority


def normalize_leave_status(value):
    status = str(value or "Pending").strip().lower()

    mapping = {
        "pending": "pending",
        "approved": "approved",
        "rejected": "rejected",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }

    return mapping.get(status, "pending")


def load_legacy_data():
    if not os.path.exists(JSON_FILE):
        raise FileNotFoundError(
            f"JSON file tidak dijumpai: {JSON_FILE}"
        )

    with open(JSON_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def fetch_id(cursor, query, parameters):
    cursor.execute(query, parameters)
    result = cursor.fetchone()

    if result is None:
        return None

    return list(result.values())[0]


def migrate():
    data = load_legacy_data()

    connection = get_db_connection()

    if connection is None:
        print("❌ Tidak dapat connect ke MySQL.")
        return

    cursor = connection.cursor(dictionary=True)

    try:
        print("Memulakan migration...")

        # ==============================================================
        # 1. ROLES
        # ==============================================================

        role_data = [
            (
                "admin",
                "Administrator",
                "Full system administration access",
            ),
            (
                "manager",
                "Manager",
                "Manages employees, tasks and leave approvals",
            ),
            (
                "employee",
                "Employee",
                "Standard employee self-service access",
            ),
        ]

        for role_key, role_name, description in role_data:
            cursor.execute(
                """
                INSERT INTO roles (
                    role_key,
                    role_name,
                    description,
                    is_active
                )
                VALUES (%s, %s, %s, TRUE)
                ON DUPLICATE KEY UPDATE
                    role_name = VALUES(role_name),
                    description = VALUES(description),
                    is_active = TRUE
                """,
                (role_key, role_name, description),
            )

        # ==============================================================
        # 2. DEPARTMENTS
        # ==============================================================

        departments = data.get("DEPARTMENTS", [])

        for department in departments:
            department_name = department.get("name", "Operations")

            department_code = (
                department_name.upper()
                .replace(" ", "_")[:20]
            )

            cursor.execute(
                """
                INSERT INTO departments (
                    department_code,
                    department_name,
                    description,
                    is_active
                )
                VALUES (%s, %s, %s, TRUE)
                ON DUPLICATE KEY UPDATE
                    department_name = VALUES(department_name),
                    description = VALUES(description),
                    is_active = TRUE
                """,
                (
                    department_code,
                    department_name,
                    f"{department_name} department",
                ),
            )

        # ==============================================================
        # 3. LEAVE TYPES
        # ==============================================================

        leave_types = [
            ("ANNUAL", "Annual Leave", 14, False),
            ("SICK", "Sick Leave", 14, True),
            ("EMERGENCY", "Emergency Leave", 3, False),
            ("UNPAID", "Unpaid Leave", 0, False),
        ]

        for code, name, default_days, document_required in leave_types:
            cursor.execute(
                """
                INSERT INTO leave_types (
                    leave_type_code,
                    leave_type_name,
                    default_days,
                    requires_document,
                    requires_approval,
                    is_paid,
                    is_active
                )
                VALUES (%s, %s, %s, %s, TRUE, TRUE, TRUE)
                ON DUPLICATE KEY UPDATE
                    leave_type_name = VALUES(leave_type_name),
                    default_days = VALUES(default_days),
                    requires_document = VALUES(requires_document),
                    is_active = TRUE
                """,
                (
                    code,
                    name,
                    default_days,
                    document_required,
                ),
            )

        # ==============================================================
        # 4. USERS / EMPLOYEES
        # ==============================================================

        legacy_users = {
            user.get("email", "").lower(): user
            for user in data.get("USERS", [])
        }

        employees = data.get("EMPLOYEES", [])

        for employee in employees:
            email = str(employee.get("email", "")).strip().lower()

            if not email:
                continue

            legacy_login = legacy_users.get(email, {})

            role_key = normalize_role(
                employee.get("role")
                or legacy_login.get("role")
            )

            role_id = fetch_id(
                cursor,
                """
                SELECT role_id
                FROM roles
                WHERE role_key = %s
                """,
                (role_key,),
            )

            department_name = employee.get(
                "department",
                "Operations",
            )

            department_id = fetch_id(
                cursor,
                """
                SELECT department_id
                FROM departments
                WHERE department_name = %s
                """,
                (department_name,),
            )

            password_hash = legacy_login.get("password")

            if not password_hash:
                password_hash = generate_password_hash("1234")

            cursor.execute(
                """
                INSERT INTO users (
                    employee_code,
                    full_name,
                    email,
                    phone_number,
                    password_hash,
                    position_title,
                    role_id,
                    department_id,
                    employment_status,
                    join_date
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                ON DUPLICATE KEY UPDATE
                    employee_code = VALUES(employee_code),
                    full_name = VALUES(full_name),
                    phone_number = VALUES(phone_number),
                    password_hash = VALUES(password_hash),
                    position_title = VALUES(position_title),
                    role_id = VALUES(role_id),
                    department_id = VALUES(department_id),
                    employment_status = VALUES(employment_status),
                    join_date = VALUES(join_date)
                """,
                (
                    employee.get("employee_id")
                    or f"EMP-{employee.get('id', 0):04d}",
                    employee.get("name", "Employee"),
                    email,
                    employee.get("phone", ""),
                    password_hash,
                    employee.get("position", ""),
                    role_id,
                    department_id,
                    normalize_employment_status(
                        employee.get("status")
                    ),
                    parse_join_date(employee.get("joined")),
                ),
            )

        # ==============================================================
        # 5. LEAVE BALANCES
        # ==============================================================

        annual_leave_type_id = fetch_id(
            cursor,
            """
            SELECT leave_type_id
            FROM leave_types
            WHERE leave_type_name = 'Annual Leave'
            """,
            (),
        )

        current_year = datetime.now().year

        for employee in employees:
            email = str(employee.get("email", "")).strip().lower()

            user_id = fetch_id(
                cursor,
                """
                SELECT user_id
                FROM users
                WHERE email = %s
                """,
                (email,),
            )

            if not user_id:
                continue

            remaining_balance = float(
                employee.get("leave_balance", 14)
            )

            allocated_days = 14.0
            used_days = max(
                allocated_days - remaining_balance,
                0,
            )

            cursor.execute(
                """
                INSERT INTO leave_balances (
                    user_id,
                    leave_type_id,
                    balance_year,
                    allocated_days,
                    carried_forward_days,
                    used_days,
                    pending_days
                )
                VALUES (%s, %s, %s, %s, 0, %s, 0)
                ON DUPLICATE KEY UPDATE
                    allocated_days = VALUES(allocated_days),
                    used_days = VALUES(used_days),
                    pending_days = VALUES(pending_days)
                """,
                (
                    user_id,
                    annual_leave_type_id,
                    current_year,
                    allocated_days,
                    used_days,
                ),
            )

        # ==============================================================
        # 6. TASKS
        # ==============================================================

        manager_id = fetch_id(
            cursor,
            """
            SELECT u.user_id
            FROM users u
            JOIN roles r ON r.role_id = u.role_id
            WHERE r.role_key IN ('manager', 'admin')
            ORDER BY
                CASE WHEN r.role_key = 'manager' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (),
        )

        tasks = data.get("TASKS", [])

        for task in tasks:
            assigned_name = task.get("assigned_to", "")

            assignee_id = fetch_id(
                cursor,
                """
                SELECT user_id
                FROM users
                WHERE full_name = %s
                """,
                (assigned_name,),
            )

            if not assignee_id:
                print(
                    f"⚠️ Task '{task.get('title')}' dilepaskan: "
                    f"employee '{assigned_name}' tidak dijumpai."
                )
                continue

            calendar_data = task.get("google_calendar") or {}
            calendar_event_id = calendar_data.get("event_id")

            task_status = normalize_task_status(
                task.get("status")
            )

            completed_at = (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if task_status == "completed"
                else None
            )

            cursor.execute(
                """
                SELECT task_id
                FROM tasks
                WHERE title = %s
                  AND assignee_id = %s
                  AND due_date = %s
                LIMIT 1
                """,
                (
                    task.get("title", "Untitled Task"),
                    assignee_id,
                    task.get("deadline"),
                ),
            )

            existing_task = cursor.fetchone()

            if existing_task:
                cursor.execute(
                    """
                    UPDATE tasks
                    SET description = %s,
                        created_by = %s,
                        priority = %s,
                        task_status = %s,
                        progress_percent = %s,
                        due_date = %s,
                        completed_at = %s,
                        calendar_event_id = %s
                    WHERE task_id = %s
                    """,
                    (
                        task.get("description", ""),
                        manager_id,
                        normalize_priority(
                            task.get("priority")
                        ),
                        task_status,
                        int(task.get("progress", 0)),
                        task.get("deadline"),
                        completed_at,
                        calendar_event_id,
                        existing_task["task_id"],
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO tasks (
                        title,
                        description,
                        assignee_id,
                        created_by,
                        priority,
                        task_status,
                        progress_percent,
                        due_date,
                        completed_at,
                        calendar_event_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        task.get("title", "Untitled Task"),
                        task.get("description", ""),
                        assignee_id,
                        manager_id,
                        normalize_priority(
                            task.get("priority")
                        ),
                        task_status,
                        int(task.get("progress", 0)),
                        task.get("deadline"),
                        completed_at,
                        calendar_event_id,
                    ),
                )

        # ==============================================================
        # 7. LEAVE REQUESTS
        # ==============================================================

        leaves = data.get("LEAVE_REQUESTS", [])

        for leave in leaves:
            employee_name = leave.get("employee_name", "")

            user_id = fetch_id(
                cursor,
                """
                SELECT user_id
                FROM users
                WHERE full_name = %s
                """,
                (employee_name,),
            )

            if not user_id:
                print(
                    f"⚠️ Leave dilepaskan: "
                    f"employee '{employee_name}' tidak dijumpai."
                )
                continue

            leave_type_name = leave.get(
                "leave_type",
                "Annual Leave",
            )

            leave_type_id = fetch_id(
                cursor,
                """
                SELECT leave_type_id
                FROM leave_types
                WHERE leave_type_name = %s
                """,
                (leave_type_name,),
            )

            if not leave_type_id:
                leave_type_id = annual_leave_type_id

            request_status = normalize_leave_status(
                leave.get("status")
            )

            decided_by = (
                manager_id
                if request_status in ("approved", "rejected")
                else None
            )

            decided_at = (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if decided_by
                else None
            )

            calendar_data = leave.get("google_calendar") or {}
            calendar_event_id = calendar_data.get("event_id")

            cursor.execute(
                """
                SELECT leave_request_id
                FROM leave_requests
                WHERE user_id = %s
                  AND leave_type_id = %s
                  AND start_date = %s
                  AND end_date = %s
                LIMIT 1
                """,
                (
                    user_id,
                    leave_type_id,
                    leave.get("start_date"),
                    leave.get("end_date"),
                ),
            )

            existing_leave = cursor.fetchone()

            values = (
                float(leave.get("duration", 1)),
                leave.get("reason", ""),
                leave.get("support_document", ""),
                request_status,
                leave.get("rejection_reason"),
                decided_by,
                decided_at,
                bool(leave.get("balance_deducted", False)),
                calendar_event_id,
            )

            if existing_leave:
                cursor.execute(
                    """
                    UPDATE leave_requests
                    SET total_days = %s,
                        reason = %s,
                        support_document = %s,
                        request_status = %s,
                        rejection_reason = %s,
                        decided_by = %s,
                        decided_at = %s,
                        balance_deducted = %s,
                        calendar_event_id = %s
                    WHERE leave_request_id = %s
                    """,
                    values
                    + (
                        existing_leave["leave_request_id"],
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO leave_requests (
                        user_id,
                        leave_type_id,
                        start_date,
                        end_date,
                        total_days,
                        reason,
                        support_document,
                        request_status,
                        rejection_reason,
                        decided_by,
                        decided_at,
                        balance_deducted,
                        calendar_event_id
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        user_id,
                        leave_type_id,
                        leave.get("start_date"),
                        leave.get("end_date"),
                    )
                    + values,
                )

        connection.commit()

        print("")
        print("✅ MIGRATION BERJAYA")
        print(
            f"✅ Users/Employees: {len(employees)}"
        )
        print(f"✅ Tasks: {len(tasks)}")
        print(f"✅ Leave requests: {len(leaves)}")
        print("")
        print("Semak dalam MySQL Workbench:")
        print("SELECT * FROM users;")
        print("SELECT * FROM tasks;")
        print("SELECT * FROM leave_requests;")

    except Exception as error:
        connection.rollback()

        print("")
        print("❌ MIGRATION GAGAL")
        print(error)

        raise

    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    migrate()