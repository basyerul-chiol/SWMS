from datetime import datetime
from werkzeug.security import generate_password_hash

USERS = [
    {
        "id": 1,
        "name": "Ahmad Faiz",
        "email": "basyerul@gmail.com",
        "password": generate_password_hash("1234"),
        "role": "employee",
    },
    {
        "id": 2,
        "name": "Sarah Menon",
        "email": "manager@swms.com",
        "password": generate_password_hash("1234"),
        "role": "manager",
    },
    {
        "id": 3,
        "name": "Admin User",
        "email": "admin@swms.com",
        "password": generate_password_hash("1234"),
        "role": "admin",
    },
]


EMPLOYEES = [
    {
        "id": 1,
        "employee_id": "EMP-1001",
        "name": "Ahmad Faiz",
        "email": "basyerul@gmail.com",
        "phone": "",
        "department": "Sales",
        "position": "Sales Executive",
        "status": "Active",
        "joined": "03 Jan 2024",
        "role": "Employee",
        "leave_balance": 14
    },
    {
        "id": 2,
        "employee_id": "EMP-1002",
        "name": "Nur Aisyah",
        "email": "aisyah@swms.com",
        "phone": "",
        "department": "Finance",
        "position": "Finance Officer",
        "status": "Active",
        "joined": "22 Feb 2025",
        "role": "Employee",
        "leave_balance": 14
    },
    {
        "id": 3,
        "employee_id": "EMP-1003",
        "name": "Ravi Kumar",
        "email": "ravi@swms.com",
        "phone": "",
        "department": "Sales",
        "position": "Sales Executive",
        "status": "On Leave",
        "joined": "14 Aug 2022",
        "role": "Employee",
        "leave_balance": 14
    },
    {
        "id": 4,
        "employee_id": "EMP-1004",
        "name": "Sarah Menon",
        "email": "manager@swms.com",
        "phone": "",
        "department": "Sales",
        "position": "Sales Manager",
        "status": "Active",
        "joined": "14 Aug 2022",
        "role": "Manager",
        "leave_balance": 14
    },
    {
        "id": 5,
        "employee_id": "EMP-1005",
        "name": "Admin User",
        "email": "admin@swms.com",
        "phone": "",
        "department": "Operations",
        "position": "Administrator",
        "status": "Active",
        "joined": "01 Jan 2022",
        "role": "Administrator",
        "leave_balance": 14
    },
]


DEPARTMENTS = [
    {"id": 1, "name": "Sales", "head": "Sarah Menon"},
    {"id": 2, "name": "Finance", "head": "Nur Aisyah"},
    {"id": 3, "name": "Operations", "head": "Maya Tan"},
]


NOTIFICATIONS = [
    {"id": 1, "channel": "WhatsApp API", "status": "Connected", "detail": "Reminders enabled"},
    {"id": 2, "channel": "Email Delivery", "status": "Delivered", "detail": "Daily summaries sent"},
    {"id": 3, "channel": "Task Reminder", "status": "Failed", "detail": "Retry pending"},
]


INTEGRATIONS = [
    {"id": 1, "name": "Slack", "enabled": False},
    {"id": 2, "name": "WhatsApp API", "enabled": True},
    {"id": 3, "name": "Email Service", "enabled": True},
]


SETTINGS = {
    "notifications": {"email": True, "whatsapp": False, "reminders": True},
    "system": {"timezone": "UTC", "workweek": "Mon-Fri"},
}


LEAVE_REQUESTS = [
    {
        "id": 1,
        "employee_name": "Ahmad Faiz",
        "leave_type": "Annual Leave",
        "start_date": "2026-07-20",
        "end_date": "2026-07-22",
        "duration": 3,
        "status": "Pending",
        "support_document": "",
    },
    {
        "id": 2,
        "employee_name": "Nur Aisyah",
        "leave_type": "Sick Leave",
        "start_date": "2026-07-08",
        "end_date": "2026-07-08",
        "duration": 1,
        "status": "Approved",
        "support_document": "Doctor note.pdf",
    },
    {
        "id": 3,
        "employee_name": "Ravi Kumar",
        "leave_type": "Emergency Leave",
        "start_date": "2026-07-15",
        "end_date": "2026-07-15",
        "duration": 1,
        "status": "Approved",
        "support_document": "",
    },
]


TASKS = [
    {
        "id": 1,
        "title": "Prepare Monthly Report",
        "assigned_to": "Ahmad Faiz",
        "priority": "High",
        "deadline": "2026-07-25",
        "progress": 50,
        "status": "In Progress"
    }
]

AUDIT_LOGS = [
    {
        "id": 1,
        "message": "System initialized with default data.",
        "module": "System",
        "action": "INIT",
        "status": "Success",
        "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
]
# Load persisted demo data when available. The application still works with the
# defaults above on first launch, then writes changes to storage/swms_data.json.
from SWMS_LAST_FINAL.storage import load_data
load_data(globals())


# Lightweight schema migration for older persisted records.
LEAVE_DEFAULTS = {
    "Annual Leave": 18,
    "Sick Leave": 14,
    "Emergency Leave": 5,
    "Unpaid Leave": 0,
}

for _employee in EMPLOYEES:
    _annual_remaining = int(_employee.get("leave_balance", LEAVE_DEFAULTS["Annual Leave"]) or 0)
    _annual_remaining = max(0, min(LEAVE_DEFAULTS["Annual Leave"], _annual_remaining))
    _balances = _employee.setdefault("leave_balances", {})

    for _leave_name, _allocation in LEAVE_DEFAULTS.items():
        _existing = _balances.get(_leave_name, {})
        if _leave_name == "Annual Leave":
            _remaining = int(_existing.get("remaining", _annual_remaining) or 0)
        elif _leave_name == "Unpaid Leave":
            _remaining = 0
        else:
            _remaining = int(_existing.get("remaining", _allocation) or 0)

        _used = int(_existing.get("used", max(0, _allocation - _remaining)) or 0)
        _balances[_leave_name] = {
            "allocated": _allocation,
            "used": max(0, _used),
            "remaining": max(0, _remaining),
        }

    # Backward-compatible alias used by older pages and reports.
    _employee["leave_balance"] = _balances["Annual Leave"]["remaining"]

for _leave in LEAVE_REQUESTS:
    _leave.setdefault("balance_deducted", False)
