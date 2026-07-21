"""MySQL-backed persistence adapter for the existing SWMS application.

The UI and business workflow continue using the original in-memory Python
collections, while this module makes MySQL the primary persistent store. A
lossless JSON snapshot is stored inside MySQL so every existing field remains
available, and key records are mirrored into the normalized enterprise tables
for database reporting and assessment.
"""

import json
from datetime import datetime
from pathlib import Path
from threading import Lock

from db import get_db_connection

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_FILE = STORAGE_DIR / "swms_data.json"  # emergency/local migration backup
_lock = Lock()

COLLECTION_NAMES = [
    "USERS", "EMPLOYEES", "DEPARTMENTS", "NOTIFICATIONS", "INTEGRATIONS",
    "SETTINGS", "LEAVE_REQUESTS", "TASKS", "AUDIT_LOGS",
]


def _ensure_compatibility_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS swms_state (
            state_key VARCHAR(80) PRIMARY KEY,
            state_value JSON NOT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """
    )


def _payload_from_namespace(namespace):
    return {
        name: namespace[name]
        for name in COLLECTION_NAMES
        if name in namespace
    }


def _apply_payload(namespace, payload):
    for name in COLLECTION_NAMES:
        if name not in payload or name not in namespace:
            continue
        current = namespace[name]
        incoming = payload[name]
        if isinstance(current, list) and isinstance(incoming, list):
            current[:] = incoming
        elif isinstance(current, dict) and isinstance(incoming, dict):
            current.clear()
            current.update(incoming)


def _json_backup(payload):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STORAGE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(STORAGE_FILE)


def _parse_join_date(value):
    if not value:
        return datetime.now().date()
    for fmt in ("%d %b %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return datetime.now().date()


def _status_key(value):
    mapping = {
        "active": "active",
        "on leave": "on_leave",
        "inactive": "inactive",
        "suspended": "suspended",
    }
    return mapping.get(str(value or "active").strip().lower(), "active")


def _task_status(value):
    mapping = {
        "not started": "not_started",
        "in progress": "in_progress",
        "completed": "completed",
        "overdue": "overdue",
        "cancelled": "cancelled",
    }
    return mapping.get(str(value or "not started").strip().lower(), "not_started")


def _priority(value):
    key = str(value or "medium").strip().lower()
    return key if key in {"low", "medium", "high", "urgent"} else "medium"


def _leave_status(value):
    key = str(value or "pending").strip().lower()
    return key if key in {"pending", "approved", "rejected", "cancelled"} else "pending"


def _sync_normalized_tables(cursor, payload):
    """Mirror the current SWMS state into normalized MySQL tables safely.

    Natural unique keys such as email, employee_code, role_key, and department
    name are used for updates. Numeric IDs from legacy Python/JSON data are not
    forced into tables that may already contain rows, preventing duplicate-key
    conflicts after migration or repeated Flask restarts.
    """
    users = payload.get("USERS", [])
    employees = payload.get("EMPLOYEES", [])
    departments = payload.get("DEPARTMENTS", [])

    employee_by_email = {
        str(item.get("email", "")).strip().lower(): item
        for item in employees
        if item.get("email")
    }

    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------
    role_rows = [
        ("employee", "Employee", "Standard employee access"),
        ("manager", "Manager", "Manager access"),
        ("admin", "Administrator", "System administrator access"),
    ]
    for role_key, role_name, description in role_rows:
        cursor.execute(
            """
            INSERT INTO roles (role_key, role_name, description, is_active)
            VALUES (%s, %s, %s, TRUE)
            ON DUPLICATE KEY UPDATE
                role_name = VALUES(role_name),
                description = VALUES(description),
                is_active = TRUE
            """,
            (role_key, role_name, description),
        )

    cursor.execute(
        "SELECT role_id, role_key FROM roles "
        "WHERE role_key IN ('employee', 'manager', 'admin')"
    )
    role_id_by_key = {
        row["role_key"]: int(row["role_id"])
        for row in cursor.fetchall()
    }

    # ------------------------------------------------------------------
    # Departments — resolve actual database IDs instead of forcing legacy IDs.
    # ------------------------------------------------------------------
    department_ids = {}
    for index, dept in enumerate(departments, start=1):
        name = str(dept.get("name") or f"Department {index}").strip()
        code = "".join(ch for ch in name.upper() if ch.isalnum())[:20] or f"D{index}"

        cursor.execute(
            """
            SELECT department_id
            FROM departments
            WHERE department_name = %s OR department_code = %s
            ORDER BY department_id
            LIMIT 1
            """,
            (name, code),
        )
        existing = cursor.fetchone()

        if existing:
            department_id = int(existing["department_id"])
            cursor.execute(
                """
                UPDATE departments
                SET department_code = %s,
                    department_name = %s,
                    description = %s,
                    is_active = TRUE
                WHERE department_id = %s
                """,
                (code, name, f"SWMS department: {name}", department_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO departments
                    (department_code, department_name, description, is_active)
                VALUES (%s, %s, %s, TRUE)
                """,
                (code, name, f"SWMS department: {name}"),
            )
            department_id = int(cursor.lastrowid)

        department_ids[name] = department_id

    # ------------------------------------------------------------------
    # Users — upsert by email/employee_code, never by legacy user_id.
    # ------------------------------------------------------------------
    db_user_id_by_name = {}
    db_user_id_by_email = {}

    for user in users:
        email = str(user.get("email") or "").strip().lower()
        if not email:
            continue

        employee = employee_by_email.get(email, {})
        legacy_id = int(user.get("id") or 0)
        employee_code = str(
            employee.get("employee_id") or f"EMP-{1000 + legacy_id}"
        ).strip()
        full_name = str(
            user.get("name") or employee.get("name") or "Unknown User"
        ).strip()
        role_key = str(user.get("role") or employee.get("role") or "employee").lower()
        role_id = role_id_by_key.get(role_key, role_id_by_key.get("employee"))
        department_id = department_ids.get(employee.get("department"))

        cursor.execute(
            """
            SELECT user_id
            FROM users
            WHERE LOWER(email) = %s OR employee_code = %s
            ORDER BY CASE WHEN LOWER(email) = %s THEN 0 ELSE 1 END, user_id
            LIMIT 1
            """,
            (email, employee_code, email),
        )
        existing = cursor.fetchone()

        values = (
            employee_code,
            full_name,
            email,
            employee.get("phone") or None,
            user.get("password") or "",
            employee.get("position") or None,
            role_id,
            department_id,
            _status_key(employee.get("status")),
            _parse_join_date(employee.get("joined")),
        )

        if existing:
            db_user_id = int(existing["user_id"])
            cursor.execute(
                """
                UPDATE users
                SET employee_code = %s,
                    full_name = %s,
                    email = %s,
                    phone_number = %s,
                    password_hash = %s,
                    position_title = %s,
                    role_id = %s,
                    department_id = %s,
                    employment_status = %s,
                    join_date = %s
                WHERE user_id = %s
                """,
                values + (db_user_id,),
            )
        else:
            cursor.execute(
                """
                INSERT INTO users (
                    employee_code, full_name, email, phone_number,
                    password_hash, position_title, role_id, department_id,
                    employment_status, join_date
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                values,
            )
            db_user_id = int(cursor.lastrowid)

        db_user_id_by_name[full_name] = db_user_id
        db_user_id_by_email[email] = db_user_id

    # Department heads must use actual MySQL user IDs.
    for dept in departments:
        department_id = department_ids.get(dept.get("name"))
        head_id = db_user_id_by_name.get(dept.get("head"))
        if department_id and head_id:
            cursor.execute(
                "UPDATE departments SET head_user_id = %s WHERE department_id = %s",
                (head_id, department_id),
            )

    # ------------------------------------------------------------------
    # Leave types and per-employee balances
    # ------------------------------------------------------------------
    leave_defaults = {
        "Annual Leave": 18,
        "Sick Leave": 14,
        "Emergency Leave": 5,
        "Unpaid Leave": 0,
    }
    requested_names = {
        "Sick Leave" if str(item.get("leave_type") or "") == "Medical Leave"
        else str(item.get("leave_type") or "Annual Leave")
        for item in payload.get("LEAVE_REQUESTS", [])
    }
    leave_type_names = sorted(set(leave_defaults) | requested_names)

    leave_type_ids = {}
    for name in leave_type_names:
        allocation = leave_defaults.get(name, 0)
        code = "".join(ch for ch in name.upper() if ch.isalnum())[:20] or "LEAVE"
        cursor.execute(
            "SELECT leave_type_id FROM leave_types WHERE leave_type_name = %s LIMIT 1",
            (name,),
        )
        existing = cursor.fetchone()

        if existing:
            leave_type_id = int(existing["leave_type_id"])
            cursor.execute(
                "UPDATE leave_types SET default_days=%s, is_active=TRUE WHERE leave_type_id=%s",
                (allocation, leave_type_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO leave_types (
                    leave_type_code, leave_type_name, default_days,
                    requires_document, requires_approval, is_paid, is_active
                ) VALUES (%s,%s,%s,FALSE,TRUE,%s,TRUE)
                """,
                (code, name, allocation, name != "Unpaid Leave"),
            )
            leave_type_id = int(cursor.lastrowid)

        leave_type_ids[name] = leave_type_id

    current_year = datetime.now().year
    for employee in employees:
        email = str(employee.get("email") or "").strip().lower()
        user_id = db_user_id_by_email.get(email)
        if not user_id:
            continue

        employee_balances = employee.get("leave_balances") or {}
        legacy_annual = int(employee.get("leave_balance", leave_defaults["Annual Leave"]) or 0)

        for leave_name, allocation in leave_defaults.items():
            entry = employee_balances.get(leave_name, {})
            if leave_name == "Annual Leave":
                remaining = int(entry.get("remaining", legacy_annual) or 0)
            elif leave_name == "Unpaid Leave":
                remaining = 0
            else:
                remaining = int(entry.get("remaining", allocation) or 0)
            used = int(entry.get("used", max(0, allocation - remaining)) or 0)

            cursor.execute(
                """
                INSERT INTO leave_balances (
                    user_id, leave_type_id, balance_year, allocated_days,
                    carried_forward_days, used_days, pending_days
                ) VALUES (%s,%s,%s,%s,0,%s,0)
                ON DUPLICATE KEY UPDATE
                    allocated_days=VALUES(allocated_days),
                    used_days=VALUES(used_days),
                    pending_days=0
                """,
                (user_id, leave_type_ids[leave_name], current_year, allocation, used),
            )

    annual_id = leave_type_ids.get("Annual Leave")

    # ------------------------------------------------------------------
    # Workflow mirrors. Rebuild from the authoritative SWMS payload so UI
    # deletions and edits are reflected exactly.
    # ------------------------------------------------------------------
    cursor.execute("DELETE FROM task_updates")
    cursor.execute("DELETE FROM tasks")
    cursor.execute("DELETE FROM leave_request_history")
    cursor.execute("DELETE FROM leave_requests")
    cursor.execute("DELETE FROM notifications")
    cursor.execute("DELETE FROM audit_logs")

    for leave in payload.get("LEAVE_REQUESTS", []):
        user_id = db_user_id_by_name.get(leave.get("employee_name"))
        if not user_id:
            continue

        leave_name = str(leave.get("leave_type") or "Annual Leave")
        if leave_name == "Medical Leave":
            leave_name = "Sick Leave"
        leave_type_id = leave_type_ids.get(leave_name, annual_id)
        if not leave_type_id:
            continue

        calendar_info = (
            leave.get("google_calendar_invitation")
            or leave.get("google_calendar")
            or {}
        )
        cursor.execute(
            """
            INSERT INTO leave_requests (
                leave_request_id, user_id, leave_type_id, start_date, end_date,
                total_days, reason, support_document, request_status,
                rejection_reason, balance_deducted, calendar_event_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                int(leave.get("id")),
                user_id,
                leave_type_id,
                leave.get("start_date"),
                leave.get("end_date"),
                float(leave.get("duration", 1) or 1),
                leave.get("reason") or None,
                leave.get("support_document") or None,
                _leave_status(leave.get("status")),
                leave.get("rejection_reason") or None,
                bool(leave.get("balance_deducted", False)),
                calendar_info.get("event_id") if isinstance(calendar_info, dict) else None,
            ),
        )

    creator_id = next(
        (
            db_user_id_by_email.get(str(u.get("email") or "").strip().lower())
            for u in users
            if str(u.get("role") or "").lower() in {"manager", "admin"}
        ),
        None,
    )

    for task in payload.get("TASKS", []):
        assignee_id = db_user_id_by_name.get(task.get("assigned_to"))
        if not assignee_id:
            continue

        calendar_info = (
            task.get("google_calendar_invitation")
            or task.get("google_calendar")
            or {}
        )
        cursor.execute(
            """
            INSERT INTO tasks (
                task_id, title, description, assignee_id, created_by, priority,
                task_status, progress_percent, due_date, calendar_event_id
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                int(task.get("id")),
                task.get("title") or "Untitled Task",
                task.get("description") or None,
                assignee_id,
                creator_id,
                _priority(task.get("priority")),
                _task_status(task.get("status")),
                int(task.get("progress", 0) or 0),
                task.get("deadline"),
                calendar_info.get("event_id") if isinstance(calendar_info, dict) else None,
            ),
        )

    for item in payload.get("NOTIFICATIONS", []):
        recipient_id = db_user_id_by_name.get(item.get("recipient_name"))
        cursor.execute(
            """
            INSERT INTO notifications (
                notification_id, user_id, related_entity_type, channel,
                recipient_address, subject, message_body, delivery_status,
                read_at, created_at
            ) VALUES (%s,%s,'system','in_app',%s,%s,%s,%s,%s,NOW())
            """,
            (
                int(item.get("id")),
                recipient_id,
                item.get("recipient_name") or None,
                item.get("title") or item.get("channel") or "SWMS Notification",
                item.get("detail") or "",
                "delivered" if item.get("read") else "sent",
                datetime.now() if item.get("read") else None,
            ),
        )

    for item in payload.get("AUDIT_LOGS", []):
        cursor.execute(
            """
            INSERT INTO audit_logs (
                audit_log_id, actor_user_id, actor_name, module_name, action_type,
                description, ip_address, action_status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                int(item.get("id")),
                db_user_id_by_name.get(item.get("actor")),
                item.get("actor") or None,
                item.get("module") or "System",
                item.get("action") or "UPDATE",
                item.get("message") or "SWMS action",
                item.get("ip_address") or None,
                "success"
                if str(item.get("status", "success")).lower() == "success"
                else "failed",
            ),
        )

    # Integrations are upserted by stable service_key, not numeric ID.
    for item in payload.get("INTEGRATIONS", []):
        name = str(item.get("name") or "Other")
        low = name.lower()
        service_type = (
            "google_calendar" if "calendar" in low
            else "twilio_whatsapp" if "whatsapp" in low
            else "email" if "email" in low
            else "other"
        )
        service_key = (
            "".join(ch for ch in low if ch.isalnum() or ch == "_")[:50]
            or f"service_{item.get('id')}"
        )
        cursor.execute(
            """
            INSERT INTO integrations (
                service_key, service_name, service_type,
                connection_status, is_enabled, last_checked_at
            ) VALUES (%s,%s,%s,%s,%s,NOW())
            ON DUPLICATE KEY UPDATE
                service_name = VALUES(service_name),
                service_type = VALUES(service_type),
                connection_status = VALUES(connection_status),
                is_enabled = VALUES(is_enabled),
                last_checked_at = NOW()
            """,
            (
                service_key,
                name,
                service_type,
                "connected" if item.get("enabled") else "disconnected",
                bool(item.get("enabled")),
            ),
        )

    settings = payload.get("SETTINGS", {})
    for group, values in settings.items():
        if not isinstance(values, dict):
            values = {"value": values}
        for key, value in values.items():
            setting_key = f"{group}.{key}"
            value_type = (
                "boolean" if isinstance(value, bool)
                else "integer" if isinstance(value, int)
                else "json" if isinstance(value, (dict, list))
                else "string"
            )
            cursor.execute(
                """
                INSERT INTO system_settings (
                    setting_key, setting_value, value_type, description, is_public
                ) VALUES (%s,%s,%s,%s,FALSE)
                ON DUPLICATE KEY UPDATE
                    setting_value = VALUES(setting_value),
                    value_type = VALUES(value_type),
                    description = VALUES(description)
                """,
                (
                    setting_key,
                    json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value),
                    value_type,
                    f"SWMS setting: {setting_key}",
                ),
            )


def load_data(namespace):
    """Load the current SWMS state from MySQL; migrate JSON on first run."""
    connection = get_db_connection()
    if connection is None:
        # Safe fallback keeps the app usable if MySQL is temporarily unavailable.
        if STORAGE_FILE.exists():
            try:
                payload = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
                _apply_payload(namespace, payload)
                print("⚠️ MySQL unavailable; SWMS loaded emergency JSON backup.")
                return True
            except (OSError, json.JSONDecodeError):
                return False
        return False

    try:
        cursor = connection.cursor(dictionary=True)
        _ensure_compatibility_table(cursor)
        cursor.execute("SELECT state_value FROM swms_state WHERE state_key='application_state'")
        row = cursor.fetchone()

        if row:
            value = row["state_value"]
            payload = json.loads(value) if isinstance(value, str) else value
            _apply_payload(namespace, payload)
            print("✅ SWMS data loaded from MySQL.")
            connection.commit()
            return True

        # First run: migrate the project's existing JSON state, otherwise seed data.py.
        if STORAGE_FILE.exists():
            try:
                payload = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = _payload_from_namespace(namespace)
        else:
            payload = _payload_from_namespace(namespace)

        cursor.execute(
            "INSERT INTO swms_state (state_key, state_value) VALUES ('application_state', %s)",
            (json.dumps(payload, ensure_ascii=False),),
        )
        _sync_normalized_tables(cursor, payload)
        connection.commit()
        _apply_payload(namespace, payload)
        _json_backup(payload)
        print("✅ Existing SWMS data migrated to MySQL.")
        return True
    except Exception as error:
        connection.rollback()
        print(f"❌ MySQL load/migration failed: {error}")

        # Fallback ke JSON
        if STORAGE_FILE.exists():
            try:
                payload = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
                _apply_payload(namespace, payload)
                print("✅ Loaded emergency JSON backup.")
                return True
            except Exception as e:
                print(e)

        return False
    finally:
        cursor.close()
        connection.close()



def save_audit_entry(entry):
    """Insert one audit record without rebuilding every normalized table.

    This is intentionally lightweight for request paths such as login/logout.
    A full save_data() sync can take too long against a remote cloud database.
    """
    connection = get_db_connection()
    if connection is None:
        return False

    cursor = connection.cursor(dictionary=True)
    try:
        actor_name = entry.get("actor") or "System"
        cursor.execute(
            "SELECT user_id FROM users WHERE full_name = %s LIMIT 1",
            (actor_name,),
        )
        row = cursor.fetchone()
        actor_user_id = int(row["user_id"]) if row else None

        cursor.execute(
            """
            INSERT INTO audit_logs (
                actor_user_id, actor_name, module_name, action_type,
                description, ip_address, action_status, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())
            """,
            (
                actor_user_id,
                actor_name,
                entry.get("module") or "System",
                entry.get("action") or "UPDATE",
                entry.get("message") or "SWMS action",
                entry.get("ip_address") or None,
                "success"
                if str(entry.get("status", "success")).lower() == "success"
                else "failed",
            ),
        )
        connection.commit()
        return True
    except Exception as error:
        connection.rollback()
        print(f"⚠️ Audit log insert failed: {error}")
        return False
    finally:
        cursor.close()
        connection.close()

def save_data(namespace):
    """Save the complete app state and normalized mirror to MySQL."""
    payload = _payload_from_namespace(namespace)
    with _lock:
        connection = get_db_connection()
        if connection is None:
            _json_backup(payload)
            print("⚠️ MySQL unavailable; changes saved to emergency JSON backup.")
            return False

        cursor = connection.cursor(dictionary=True)
        try:
            _ensure_compatibility_table(cursor)
            cursor.execute(
                """
                INSERT INTO swms_state (state_key, state_value)
                VALUES ('application_state', %s)
                ON DUPLICATE KEY UPDATE state_value=VALUES(state_value)
                """,
                (json.dumps(payload, ensure_ascii=False),),
            )
            _sync_normalized_tables(cursor, payload)
            connection.commit()
            _json_backup(payload)
            return True
        except Exception as error:
            connection.rollback()
            _json_backup(payload)
            print(f"❌ MySQL save failed; JSON backup kept: {error}")
            return False
        finally:
            cursor.close()
            connection.close()