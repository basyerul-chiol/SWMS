from datetime import datetime, date, timedelta
import calendar as calendar_module
import uuid
from pathlib import Path
import json
import os
from io import StringIO
import csv


from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    Response,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from SWMS_LAST_FINAL.storage import save_data, load_data, save_audit_entry
import data as data_store
from notification_service import send_task_assignment, send_leave_status

from data import (
    AUDIT_LOGS,
    DEPARTMENTS,
    EMPLOYEES,
    INTEGRATIONS,
    LEAVE_REQUESTS,
    NOTIFICATIONS,
    SETTINGS,
    TASKS,
    USERS,
)


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-local-development-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
PROFILE_UPLOAD_FOLDER = Path(app.root_path) / "static" / "uploads" / "profile"
PROFILE_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
ALLOWED_PROFILE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

load_data(data_store.__dict__)

# Upgrade older employee records to separate, dynamic leave balances.
_STARTUP_LEAVE_DEFAULTS = {
    "Annual Leave": 18,
    "Sick Leave": 14,
    "Emergency Leave": 5,
    "Unpaid Leave": 0,
}
_startup_leave_changed = False
for _employee in EMPLOYEES:
    _balances = _employee.setdefault("leave_balances", {})
    _legacy_annual = int(_employee.get("leave_balance", 18) or 0)
    for _leave_name, _allocation in _STARTUP_LEAVE_DEFAULTS.items():
        if _leave_name not in _balances:
            _remaining = (
                _legacy_annual if _leave_name == "Annual Leave"
                else 0 if _leave_name == "Unpaid Leave"
                else _allocation
            )
            _balances[_leave_name] = {
                "allocated": _allocation,
                "used": max(0, _allocation - _remaining),
                "remaining": max(0, _remaining),
            }
            _startup_leave_changed = True
    _employee["leave_balance"] = _balances["Annual Leave"]["remaining"]

if _startup_leave_changed:
    save_data(data_store.__dict__)
    print("✅ Employee leave balances initialized.")


# ---------------------------------------------------------------------------
# GOOGLE CALENDAR CONFIGURATION
# ---------------------------------------------------------------------------

GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]

PROJECT_ROOT = Path(__file__).resolve().parent
GOOGLE_CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
GOOGLE_TOKEN_FOLDER = Path(
    os.getenv("GOOGLE_TOKEN_FOLDER", str(PROJECT_ROOT / "google_tokens"))
)

# Local HTTP OAuth is allowed only during local development. Render uses HTTPS.
if os.getenv("RENDER") != "true":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


def google_oauth_configured():
    """Return True when OAuth credentials are available from env or local file."""
    env_ready = bool(
        os.getenv("GOOGLE_CLIENT_ID", "").strip()
        and os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    )
    return env_ready or GOOGLE_CREDENTIALS_FILE.exists()


def google_redirect_uri():
    """Use the exact production callback URI when supplied by Render."""
    configured = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
    return configured or url_for("google_calendar_callback", _external=True)


def create_google_flow(state=None, code_verifier=None):
    """Create the OAuth flow without storing Google secrets in GitHub."""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

    common = {
        "scopes": GOOGLE_CALENDAR_SCOPES,
        "state": state,
        "code_verifier": code_verifier,
        "autogenerate_code_verifier": code_verifier is None,
    }

    if client_id and client_secret:
        client_config = {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [google_redirect_uri()],
            }
        }
        flow = Flow.from_client_config(client_config, **common)
    elif GOOGLE_CREDENTIALS_FILE.exists():
        flow = Flow.from_client_secrets_file(str(GOOGLE_CREDENTIALS_FILE), **common)
    else:
        return None

    flow.redirect_uri = google_redirect_uri()
    return flow


def google_token_path(user_id):
    """Return the private OAuth token file belonging to one SWMS user."""
    GOOGLE_TOKEN_FOLDER.mkdir(parents=True, exist_ok=True)
    return GOOGLE_TOKEN_FOLDER / f"user_{user_id}.json"


def google_calendar_connected(user_id):
    if not user_id:
        return False
    return google_token_path(user_id).exists()


def save_google_credentials(user_id, credentials):
    token_file = google_token_path(user_id)
    token_file.write_text(credentials.to_json(), encoding="utf-8")


def load_google_credentials(user_id):
    token_file = google_token_path(user_id)
    if not token_file.exists():
        return None

    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_file), GOOGLE_CALENDAR_SCOPES
        )
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleAuthRequest())
            save_google_credentials(user_id, credentials)
        return credentials if credentials.valid else None
    except Exception:
        return None


def google_calendar_service(user_id):
    credentials = load_google_credentials(user_id)
    if not credentials:
        return None
    try:
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)
    except Exception:
        return None


def swms_user_by_name(name):
    """Find a SWMS login account using the employee's display name."""
    normalized_name = (name or "").strip().lower()
    return next(
        (
            user for user in USERS
            if user.get("name", "").strip().lower() == normalized_name
        ),
        None,
    )


def create_google_calendar_event(
    user_id,
    event_body,
    send_updates="none",
):
    """
    Insert an event into a connected user's primary Google Calendar.

    The connected user becomes the event organizer. When send_updates is
    "all", Google sends invitation emails to every attendee in event_body.
    Calendar failures never cancel the main SWMS workflow.
    """
    service = google_calendar_service(user_id)

    if not service:
        return {
            "success": False,
            "status": "Organizer Not Connected",
            "event_id": "",
            "event_link": "",
            "error": "The organizer has not connected Google Calendar.",
        }

    try:
        created_event = (
            service.events()
            .insert(
                calendarId="primary",
                body=event_body,
                sendUpdates=send_updates,
            )
            .execute()
        )

        return {
            "success": True,
            "status": "Invitation Sent",
            "event_id": created_event.get("id", ""),
            "event_link": created_event.get("htmlLink", ""),
            "error": "",
        }
    except HttpError as error:
        return {
            "success": False,
            "status": "Sync Failed",
            "event_id": "",
            "event_link": "",
            "error": str(error),
        }
    except Exception as error:
        return {
            "success": False,
            "status": "Sync Failed",
            "event_id": "",
            "event_link": "",
            "error": str(error),
        }


def task_calendar_event_body(task):
    """Prepare an all-day Google Calendar event for a task deadline."""
    deadline = datetime.strptime(task["deadline"], "%Y-%m-%d").date()
    exclusive_end = deadline + timedelta(days=1)

    return {
        "summary": f"SWMS Task: {task.get('title', 'Assigned Task')}",
        "description": (
            f"Assigned to: {task.get('assigned_to', '')}\n"
            f"Priority: {task.get('priority', 'Medium')}\n"
            f"Status: {task.get('status', 'Not Started')}\n\n"
            f"{task.get('description', '')}"
        ),
        "start": {
            "date": deadline.isoformat(),
        },
        "end": {
            "date": exclusive_end.isoformat(),
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 24 * 60},
                {"method": "popup", "minutes": 60},
            ],
        },
    }


def leave_calendar_event_body(leave):
    """Prepare an all-day event covering every approved leave day."""
    start_day = datetime.strptime(leave["start_date"], "%Y-%m-%d").date()
    final_leave_day = datetime.strptime(leave["end_date"], "%Y-%m-%d").date()

    # Google Calendar all-day event end dates are exclusive.
    exclusive_end = final_leave_day + timedelta(days=1)

    return {
        "summary": (
            f"SWMS Leave: {leave.get('employee_name', '')} "
            f"({leave.get('leave_type', 'Leave')})"
        ),
        "description": (
            f"Employee: {leave.get('employee_name', '')}\n"
            f"Leave type: {leave.get('leave_type', '')}\n"
            f"Duration: {leave.get('duration', '')} day(s)\n"
            f"Status: Approved"
        ),
        "start": {
            "date": start_day.isoformat(),
        },
        "end": {
            "date": exclusive_end.isoformat(),
        },
        "transparency": "transparent",
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 24 * 60},
            ],
        },
    }


def valid_google_attendee(user):
    """Return True when a SWMS user has an email usable as an attendee."""
    email = (user or {}).get("email", "").strip()
    return bool(email and "@" in email)


def unique_attendees(users, excluded_user_id=None):
    """Build a duplicate-free Google Calendar attendee list."""
    attendees = []
    seen_emails = set()

    for user in users:
        if not user:
            continue
        if excluded_user_id is not None and user.get("id") == excluded_user_id:
            continue
        if not valid_google_attendee(user):
            continue

        email = user["email"].strip().lower()
        if email in seen_emails:
            continue

        seen_emails.add(email)
        attendees.append(
            {
                "email": user["email"].strip(),
                "displayName": user.get("name", ""),
            }
        )

    return attendees


def sync_task_calendar_invitation(task, organizer):
    """
    Create the task on the connected manager/admin calendar and invite only
    the assigned employee. Google sends the employee an invitation email.
    """
    assigned_user = swms_user_by_name(task.get("assigned_to", ""))

    if not assigned_user:
        result = {
            "success": False,
            "status": "Employee Account Not Found",
            "event_id": "",
            "event_link": "",
            "error": "No SWMS user account matches the assigned employee.",
        }
    elif not valid_google_attendee(assigned_user):
        result = {
            "success": False,
            "status": "Employee Email Missing",
            "event_id": "",
            "event_link": "",
            "error": "The assigned employee does not have a valid email.",
        }
    elif not organizer:
        result = {
            "success": False,
            "status": "Organizer Not Found",
            "event_id": "",
            "event_link": "",
            "error": "The manager/admin account could not be found.",
        }
    else:
        existing = task.get("google_calendar_invitation", {})
        if existing.get("event_id"):
            return existing

        try:
            event_body = task_calendar_event_body(task)
            event_body["attendees"] = unique_attendees(
                [assigned_user],
                excluded_user_id=organizer.get("id"),
            )

            result = create_google_calendar_event(
                organizer["id"],
                event_body,
                send_updates="all",
            )
        except (KeyError, ValueError) as error:
            result = {
                "success": False,
                "status": "Invalid Deadline",
                "event_id": "",
                "event_link": "",
                "error": str(error),
            }

    task["google_calendar_invitation"] = {
        "status": result["status"],
        "organizer_user_id": organizer.get("id") if organizer else None,
        "organizer_name": organizer.get("name", "") if organizer else "",
        "attendee_user_id": assigned_user.get("id") if assigned_user else None,
        "attendee_name": assigned_user.get("name", "") if assigned_user else "",
        "attendee_email": assigned_user.get("email", "") if assigned_user else "",
        "event_id": result["event_id"],
        "event_link": result["event_link"],
        "error": result["error"],
        "synced_at": (
            datetime.now().isoformat(timespec="seconds")
            if result["success"]
            else ""
        ),
    }

    persist_data()
    return task["google_calendar_invitation"]


def approved_leave_invitation_users(leave, organizer):
    """
    Invite the employee taking leave and every manager/admin except the
    organizer. The organizer already receives the event automatically.
    """
    recipients = []
    employee_user = swms_user_by_name(leave.get("employee_name", ""))

    if employee_user:
        recipients.append(employee_user)

    for user in USERS:
        if user.get("role") in ("manager", "admin"):
            recipients.append(user)

    return unique_attendees(
        recipients,
        excluded_user_id=organizer.get("id") if organizer else None,
    )


def sync_approved_leave_calendar_invitation(leave, organizer):
    """
    The approving manager/admin creates one event and invites:
    - the employee taking leave;
    - all other managers;
    - all admins.

    This places the event on the organizer's calendar and emails invitations
    to the other recipients.
    """
    existing = leave.get("google_calendar_invitation", {})
    if existing.get("event_id"):
        return existing

    if not organizer:
        result = {
            "success": False,
            "status": "Organizer Not Found",
            "event_id": "",
            "event_link": "",
            "error": "The approving manager/admin account was not found.",
        }
        attendees = []
    else:
        attendees = approved_leave_invitation_users(leave, organizer)

        try:
            event_body = leave_calendar_event_body(leave)
            event_body["attendees"] = attendees

            result = create_google_calendar_event(
                organizer["id"],
                event_body,
                send_updates="all",
            )
        except (KeyError, ValueError) as error:
            result = {
                "success": False,
                "status": "Invalid Leave Dates",
                "event_id": "",
                "event_link": "",
                "error": str(error),
            }

    leave["google_calendar_invitation"] = {
        "status": result["status"],
        "organizer_user_id": organizer.get("id") if organizer else None,
        "organizer_name": organizer.get("name", "") if organizer else "",
        "attendee_emails": [
            attendee.get("email", "")
            for attendee in attendees
        ],
        "event_id": result["event_id"],
        "event_link": result["event_link"],
        "error": result["error"],
        "synced_at": (
            datetime.now().isoformat(timespec="seconds")
            if result["success"]
            else ""
        ),
    }

    persist_data()
    return leave["google_calendar_invitation"]


def login_required():
    return session.get("user_id") is not None


def role_required(*allowed_roles):
    return login_required() and session.get("role") in allowed_roles


def current_user():
    return next((user for user in USERS if user["id"] == session.get("user_id")), None)


def current_employee():
    user = current_user()
    if not user:
        return None
    return next(
        (employee for employee in EMPLOYEES if employee["email"].lower() == user["email"].lower()),
        None,
    )


def employee_by_name(name):
    return next((employee for employee in EMPLOYEES if employee.get("name") == name), None)


def allowed_profile_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PROFILE_EXTENSIONS


def save_profile_picture(uploaded_file, employee):
    """Validate and save a profile picture, returning its static relative path."""
    if not uploaded_file or not uploaded_file.filename:
        return employee.get("profile_picture", "")
    if not allowed_profile_file(uploaded_file.filename):
        raise ValueError("Profile picture must be PNG, JPG, JPEG, or WEBP.")
    extension = secure_filename(uploaded_file.filename).rsplit(".", 1)[1].lower()
    filename = f"employee_{employee.get('id', 'user')}_{uuid.uuid4().hex[:12]}.{extension}"
    destination = PROFILE_UPLOAD_FOLDER / filename
    uploaded_file.save(destination)
    old_relative = employee.get("profile_picture", "")
    if old_relative and old_relative.startswith("uploads/profile/"):
        old_path = Path(app.static_folder) / old_relative
        if old_path.exists() and old_path != destination:
            try:
                old_path.unlink()
            except OSError:
                pass
    return f"uploads/profile/{filename}"


def google_calendar_events_for_month(user_id, year, month):
    """Read Google Calendar events for a month. Fail safely when disconnected."""
    service = google_calendar_service(user_id)
    if not service:
        return [], False
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    try:
        response = service.events().list(
            calendarId="primary",
            timeMin=start.isoformat() + "Z",
            timeMax=end.isoformat() + "Z",
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        events = []
        for item in response.get("items", []):
            start_value = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
            if not start_value:
                continue
            day_text = start_value[:10]
            events.append({
                "title": item.get("summary", "Google Calendar Event"),
                "date": day_text,
                "time": "All day" if "T" not in start_value else start_value[11:16],
                "source": "Google",
                "link": item.get("htmlLink", ""),
            })
        return events, True
    except Exception:
        return [], True


def build_employee_calendar(user, employee_tasks, employee_leaves, year, month):
    """Combine Google events, task deadlines, and approved leave into a month view."""
    google_events, google_connected = google_calendar_events_for_month(user["id"], year, month)
    local_events = []
    month_prefix = f"{year:04d}-{month:02d}"
    for task in employee_tasks:
        deadline = task.get("deadline", "")
        if deadline.startswith(month_prefix):
            local_events.append({"title": task.get("title", "Task deadline"), "date": deadline, "time": "Deadline", "source": "Task", "link": url_for("employee_task_detail", task_id=task["id"])})
    for leave in employee_leaves:
        if leave.get("status") != "Approved":
            continue
        try:
            start_day = datetime.strptime(leave["start_date"], "%Y-%m-%d").date()
            end_day = datetime.strptime(leave["end_date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        cursor = start_day
        while cursor <= end_day:
            if cursor.year == year and cursor.month == month:
                local_events.append({"title": leave.get("leave_type", "Approved Leave"), "date": cursor.isoformat(), "time": "Approved leave", "source": "Leave", "link": url_for("employee_leave")})
            cursor += timedelta(days=1)
    all_events = google_events + local_events
    all_events.sort(key=lambda event: (event["date"], event.get("time", "")))
    events_by_day = {}
    for event in all_events:
        events_by_day.setdefault(int(event["date"][-2:]), []).append(event)
    weeks = calendar_module.Calendar(firstweekday=6).monthdatescalendar(year, month)
    return {
        "year": year,
        "month": month,
        "month_name": calendar_module.month_name[month],
        "weeks": weeks,
        "events": all_events,
        "events_by_day": events_by_day,
        "google_connected": google_connected,
        "today": date.today(),
    }


def leave_dates_overlap(employee_name, start_date, end_date, exclude_id=None):
    for leave in LEAVE_REQUESTS:
        if leave.get("employee_name") != employee_name:
            continue
        if exclude_id is not None and leave.get("id") == exclude_id:
            continue
        if leave.get("status") not in ("Pending", "Approved"):
            continue
        if start_date <= leave.get("end_date", "") and end_date >= leave.get("start_date", ""):
            return leave
    return None


def next_id(records):
    return max((item["id"] for item in records), default=0) + 1


def calculate_duration(start_date, end_date):
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        if end < start:
            return None
        return (end - start).days + 1
    except ValueError:
        return None


LEAVE_DEFAULTS = {
    "Annual Leave": 18,
    "Sick Leave": 14,
    "Emergency Leave": 5,
    "Unpaid Leave": 0,
}


def ensure_employee_leave_balances(employee):
    """Return a complete, backward-compatible leave balance structure."""
    balances = employee.setdefault("leave_balances", {})

    for leave_name, allocation in LEAVE_DEFAULTS.items():
        existing = balances.get(leave_name, {})
        if leave_name == "Annual Leave":
            legacy_remaining = int(employee.get("leave_balance", allocation) or 0)
            remaining = int(existing.get("remaining", legacy_remaining) or 0)
        elif leave_name == "Unpaid Leave":
            remaining = 0
        else:
            remaining = int(existing.get("remaining", allocation) or 0)

        used = int(existing.get("used", max(0, allocation - remaining)) or 0)
        balances[leave_name] = {
            "allocated": allocation,
            "used": max(0, used),
            "remaining": max(0, remaining),
        }

    employee["leave_balance"] = balances["Annual Leave"]["remaining"]
    return balances


def leave_balance_for(employee, leave_type):
    balances = ensure_employee_leave_balances(employee)
    normalized = "Sick Leave" if leave_type == "Medical Leave" else leave_type
    return balances.get(normalized, {"allocated": 0, "used": 0, "remaining": 0})


def persist_data():
    save_data(data_store.__dict__)


def add_notification(recipient_role, title, detail, link="", recipient_name=""):
    NOTIFICATIONS.insert(0, {
        "id": next_id(NOTIFICATIONS),
        "recipient_role": recipient_role,
        "recipient_name": recipient_name,
        "title": title,
        "detail": detail,
        "link": link,
        "read": False,
        "created_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        # Compatibility fields used by the integration/status panel.
        "channel": title,
        "status": "Unread",
    })


def add_audit_entry(message, module, action, status):
    user = current_user()
    entry = {
        "id": next_id(AUDIT_LOGS),
        "message": message,
        "actor": user["name"] if user else "System",
        "role": session.get("role", "system"),
        "module": module,
        "action": action,
        "status": status,
        "timestamp": datetime.now().strftime("%d %b %Y, %I:%M %p"),
        "ip_address": request.headers.get("X-Forwarded-For", request.remote_addr or "Local"),
    }
    AUDIT_LOGS.insert(0, entry)

    # Do not run persist_data() here. It rebuilds all normalized tables and can
    # exceed Gunicorn's request timeout when MySQL is hosted remotely.
    save_audit_entry(entry)


def user_notifications():
    role = session.get("role", "")
    name = session.get("user_name", "")
    return [note for note in NOTIFICATIONS
            if note.get("recipient_role") in (role, "all")
            and (not note.get("recipient_name") or note.get("recipient_name") == name)]


def refresh_task_deadlines():
    today = date.today().isoformat()
    changed = False
    for task in TASKS:
        if task.get("status") != "Completed" and task.get("deadline") and task["deadline"] < today:
            if task.get("status") != "Overdue":
                task["status"] = "Overdue"
                changed = True
    if changed:
        persist_data()


@app.before_request
def update_deadline_state():
    refresh_task_deadlines()


@app.context_processor
def inject_user():
    notes = user_notifications() if login_required() else []
    return {
        "current_user": current_user(),
        "current_role": session.get("role", ""),
        "user_notifications": notes,
        "unread_notification_count": sum(1 for note in notes if not note.get("read", False)),
    }


@app.route("/")
def home():
    if login_required():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if login_required():
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please enter both email and password.", "danger")
            return render_template("login.html")

        user = next((user for user in USERS if user["email"].lower() == email.lower()), None)
        if not user or not check_password_hash(user["password"], password):
            add_audit_entry(
                f"Failed login attempt for {email}.",
                "Authentication",
                "LOGIN_FAILED",
                "Failed",
            )
            flash("Invalid email or password.", "danger")
            return render_template("login.html")

        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["role"] = user["role"]

        add_audit_entry(
            f"{user['name']} logged in.",
            "Authentication",
            "LOGIN",
            "Success",
        )
        flash("Login successful.", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    user = current_user()
    if user:
        add_audit_entry(
            f"{user['name']} logged out.",
            "Authentication",
            "LOGOUT",
            "Success",
        )
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if not login_required():
        flash("Please log in.", "warning")
        return redirect(url_for("login"))

    role = session.get("role")
    if role == "employee":
        return redirect(url_for("employee_dashboard"))
    if role == "manager":
        return redirect(url_for("manager_dashboard"))
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("logout"))


@app.route("/employee-dashboard")
def employee_dashboard():
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    user = current_user()
    employee = current_employee()
    employee_tasks = [task for task in TASKS if task["assigned_to"] == user["name"]]
    employee_leaves = [leave for leave in LEAVE_REQUESTS if leave["employee_name"] == user["name"]]
    leave_balances = ensure_employee_leave_balances(employee) if employee else {}

    today = date.today()
    try:
        calendar_year = int(request.args.get("year", today.year))
        calendar_month = int(request.args.get("month", today.month))
        if calendar_month < 1 or calendar_month > 12:
            raise ValueError
    except (TypeError, ValueError):
        calendar_year, calendar_month = today.year, today.month

    employee_calendar = build_employee_calendar(
        user, employee_tasks, employee_leaves, calendar_year, calendar_month
    )
    previous_month = date(calendar_year, calendar_month, 1) - timedelta(days=1)
    next_month = (date(calendar_year, calendar_month, 28) + timedelta(days=4)).replace(day=1)
    performance = calculate_employee_performance(user["name"])
    dashboard_stats = {
        "active_tasks": sum(1 for task in employee_tasks if task.get("status") != "Completed"),
        "completed_tasks": sum(1 for task in employee_tasks if task.get("status") == "Completed"),
        "overdue_tasks": sum(1 for task in employee_tasks if task.get("status") == "Overdue"),
        "pending_leave": sum(1 for leave in employee_leaves if leave.get("status") == "Pending"),
        "approved_leave": sum(1 for leave in employee_leaves if leave.get("status") == "Approved"),
    }
    return render_template(
        "employee_dashboard.html",
        active_page="dashboard",
        user=user,
        employee=employee,
        tasks=employee_tasks,
        leave_requests=employee_leaves,
        notifications=NOTIFICATIONS,
        leave_balances=leave_balances,
        employee_calendar=employee_calendar,
        previous_month=previous_month,
        next_month=next_month,
        performance=performance,
        dashboard_stats=dashboard_stats,
    )


@app.route("/employee-leave", methods=["GET"])
def employee_leave():
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    user = current_user()
    employee = employee_by_name(user["name"])

    if not employee:
        flash("Employee profile not found.", "danger")
        return redirect(url_for("dashboard"))

    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "All").strip()

    leaves = [
        leave
        for leave in LEAVE_REQUESTS
        if leave.get("employee_name", "").strip().lower()
        == employee.get("name", "").strip().lower()
    ]

    if search:
        keyword = search.lower()

        leaves = [
            leave
            for leave in leaves
            if keyword in leave.get("leave_type", "").lower()
            or keyword in leave.get("status", "").lower()
            or keyword in leave.get("start_date", "").lower()
            or keyword in leave.get("end_date", "").lower()
        ]

    if status_filter != "All":
        leaves = [
            leave
            for leave in leaves
            if leave.get("status") == status_filter
        ]

    return render_template(
        "employee_leave.html",
        active_page="leave",
        employee=employee,
        leaves=leaves,
        search=search,
        status_filter=status_filter,
        current_employee_balance=int(
            ensure_employee_leave_balances(employee)["Annual Leave"]["remaining"]
        ),
        leave_balances=ensure_employee_leave_balances(employee),
    )


@app.route("/employee-tasks")
def employee_tasks():
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    user_name = current_user()["name"]
    search = request.args.get("search", "").strip().lower()
    status_filter = request.args.get("status", "All")

    tasks = [task for task in TASKS if task["assigned_to"] == user_name]
    if search:
        tasks = [
            task for task in tasks
            if search in task["title"].lower()
            or search in task["status"].lower()
            or search in task["priority"].lower()
        ]
    if status_filter != "All":
        if status_filter == "Overdue":
            tasks = [task for task in tasks if task["status"] == "Overdue"]
        else:
            tasks = [task for task in tasks if task["status"] == status_filter]

    return render_template(
        "employee_tasks.html",
        active_page="tasks",
        tasks=tasks,
        status_filter=status_filter,
        search=search,
    )


@app.route("/employee-tasks/<int:task_id>")
def employee_task_detail(task_id):
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    task = next((item for item in TASKS if item["id"] == task_id), None)
    if not task or task["assigned_to"] != current_user()["name"]:
        flash("Task not found.", "danger")
        return redirect(url_for("employee_tasks"))

    return render_template(
        "employee_task_detail.html",
        active_page="tasks",
        task=task,
    )


@app.route("/employee-profile", methods=["GET", "POST"])
def employee_profile():
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    user = current_user()
    employee = current_employee()
    if not employee:
        flash("Employee profile not found.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        emergency_contact = request.form.get("emergency_contact", "").strip()
        emergency_phone = request.form.get("emergency_phone", "").strip()

        if not name or not email or "@" not in email:
            flash("Please enter a valid name and email address.", "danger")
            return redirect(url_for("employee_profile"))

        duplicate = next((item for item in USERS if item["id"] != user["id"] and item.get("email", "").lower() == email.lower()), None)
        if duplicate:
            flash("That email address is already used by another account.", "danger")
            return redirect(url_for("employee_profile"))

        try:
            profile_picture = save_profile_picture(request.files.get("profile_picture"), employee)
        except ValueError as error:
            flash(str(error), "danger")
            return redirect(url_for("employee_profile"))

        old_name = employee.get("name", "")
        employee.update({
            "name": name,
            "email": email,
            "phone": phone,
            "address": address,
            "emergency_contact": emergency_contact,
            "emergency_phone": emergency_phone,
            "profile_picture": profile_picture,
        })
        user["name"] = name
        user["email"] = email
        session["user_name"] = name
        for task in TASKS:
            if task.get("assigned_to") == old_name:
                task["assigned_to"] = name
        for leave in LEAVE_REQUESTS:
            if leave.get("employee_name") == old_name:
                leave["employee_name"] = name

        add_audit_entry(f"{name} updated profile details.", "Employee Records", "UPDATE", "Success")
        flash("Your profile has been updated.", "success")
        return redirect(url_for("employee_profile"))

    return render_template("employee_profile.html", active_page="profile", employee=employee, user=user)


@app.route("/employee-settings", methods=["GET", "POST"])
def employee_settings():
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        SETTINGS["notifications"]["email"] = request.form.get("email_notifications") == "on"
        SETTINGS["notifications"]["whatsapp"] = request.form.get("whatsapp_notifications") == "on"
        SETTINGS["notifications"]["reminders"] = request.form.get("reminders") == "on"

        add_audit_entry(
            f"{current_user()['name']} updated notification preferences.",
            "Settings",
            "UPDATE",
            "Success",
        )
        flash("Notification preferences saved.", "success")
        return redirect(url_for("employee_settings"))

    return render_template(
        "employee_settings.html",
        active_page="settings",
        settings=SETTINGS["notifications"],
    )


@app.route("/leave/submit", methods=["POST"])
def submit_leave():
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    leave_type = request.form.get("leave_type", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    support_document = request.form.get("support_document", "").strip()

    if not leave_type or not start_date or not end_date:
        flash("Please complete all required leave fields.", "danger")
        return redirect(url_for("employee_leave"))

    duration = calculate_duration(start_date, end_date)
    if duration is None:
        flash("End date cannot be earlier than the start date.", "danger")
        return redirect(url_for("employee_leave"))
    if start_date < date.today().isoformat():
        flash("Leave cannot start on a past date.", "danger")
        return redirect(url_for("employee_leave"))

    employee = current_employee()
    if not employee:
        flash("Employee profile was not found.", "danger")
        return redirect(url_for("employee_leave"))

    overlap = leave_dates_overlap(employee["name"], start_date, end_date)
    if overlap:
        flash(f"This request overlaps leave request #{overlap['id']} ({overlap['start_date']} to {overlap['end_date']}).", "danger")
        return redirect(url_for("employee_leave"))

    normalized_leave_type = "Sick Leave" if leave_type == "Medical Leave" else leave_type
    if normalized_leave_type != "Unpaid Leave":
        available = leave_balance_for(employee, normalized_leave_type)["remaining"]
        if duration > available:
            flash(
                f"Insufficient {normalized_leave_type.lower()} balance. Available: {available} day(s).",
                "danger",
            )
            return redirect(url_for("employee_leave"))

    LEAVE_REQUESTS.append({
        "id": next_id(LEAVE_REQUESTS),
        "employee_name": employee["name"],
        "leave_type": leave_type,
        "start_date": start_date,
        "end_date": end_date,
        "duration": duration,
        "status": "Pending",
        "support_document": support_document,
        "balance_deducted": False,
    })
    add_notification("manager", "New leave request", f"{employee['name']} submitted {leave_type}.", url_for("manager_leave"))
    add_audit_entry(f"{employee['name']} submitted {duration}-day {leave_type} request.", "Leave Management", "SUBMIT", "Pending")
    flash("Leave request submitted for manager approval.", "success")
    return redirect(url_for("employee_leave"))


@app.route("/leave/<int:leave_id>/cancel", methods=["POST"])
def cancel_leave(leave_id):
    if not role_required("employee"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    leave = next((item for item in LEAVE_REQUESTS if item["id"] == leave_id), None)
    if not leave or leave["employee_name"] != current_user()["name"]:
        flash("Leave request not found.", "danger")
        return redirect(url_for("employee_leave"))

    if leave["status"] != "Pending":
        flash("Only pending leave requests can be canceled.", "warning")
        return redirect(url_for("employee_leave"))

    leave["status"] = "Cancelled"
    add_notification("manager", "Leave request cancelled", f"{current_user()['name']} cancelled leave request #{leave_id}.", url_for("manager_leave"))
    add_audit_entry(
        f"{current_user()['name']} canceled leave request {leave_id}.",
        "Leave Management",
        "CANCEL",
        "Success",
    )
    flash("Leave request canceled.", "success")
    return redirect(url_for("employee_leave"))


@app.route("/employee-tasks/<int:task_id>/progress", methods=["POST"])
def update_task_progress(task_id):
    if not login_required():
        flash("Please log in.", "warning")
        return redirect(url_for("login"))

    task = next((item for item in TASKS if item["id"] == task_id), None)
    if not task:
        flash("Task not found.", "danger")
        return redirect(url_for("dashboard"))

    try:
        progress = int(request.form.get("progress", task["progress"]))
    except (TypeError, ValueError):
        progress = task["progress"]

    task["progress"] = max(0, min(100, progress))
    if task["progress"] >= 100:
        task["status"] = "Completed"
    elif task["progress"] > 0:
        task["status"] = "In Progress"
    else:
        task["status"] = "Not Started"

    if task["status"] == "Completed":
        add_notification("manager", "Task completed", f"{task['assigned_to']} completed {task['title']}.", url_for("manager_tasks"))
    add_audit_entry(
        f"{current_user()['name']} updated task {task_id} progress.",
        "Task Management",
        "UPDATE",
        "Success",
    )
    flash("Task progress updated.", "success")
    return redirect(url_for("employee_task_detail", task_id=task_id))


def calculate_employee_performance(employee_name):
    """Calculate task-based performance metrics for one employee."""
    employee_tasks = [
        task for task in TASKS
        if task.get("assigned_to", "").strip().lower()
        == employee_name.strip().lower()
    ]

    total_tasks = len(employee_tasks)

    if total_tasks == 0:
        return {
            "employee_name": employee_name,
            "total_tasks": 0,
            "completed_tasks": 0,
            "overdue_tasks": 0,
            "average_progress": 0,
            "completion_rate": 0,
            "deadline_score": 0,
            "performance_score": 0,
            "rating": "No Data",
        }

    completed_tasks = sum(
        1 for task in employee_tasks
        if task.get("status") == "Completed"
        or int(task.get("progress", 0) or 0) >= 100
    )

    overdue_tasks = sum(
        1 for task in employee_tasks
        if task.get("status") == "Overdue"
    )

    average_progress = round(
        sum(int(task.get("progress", 0) or 0) for task in employee_tasks)
        / total_tasks
    )

    completion_rate = round(
        (completed_tasks / total_tasks) * 100
    )

    deadline_score = round(
        ((total_tasks - overdue_tasks) / total_tasks) * 100
    )

    performance_score = round(
        (completion_rate * 0.50)
        + (average_progress * 0.30)
        + (deadline_score * 0.20)
    )

    if performance_score >= 85:
        rating = "Excellent"
    elif performance_score >= 70:
        rating = "Good"
    elif performance_score >= 50:
        rating = "Average"
    else:
        rating = "Needs Improvement"

    return {
        "employee_name": employee_name,
        "total_tasks": total_tasks,
        "completed_tasks": completed_tasks,
        "overdue_tasks": overdue_tasks,
        "average_progress": average_progress,
        "completion_rate": completion_rate,
        "deadline_score": deadline_score,
        "performance_score": performance_score,
        "rating": rating,
    }


@app.route("/google-calendar")
def google_calendar_settings():
    if not login_required():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    user = current_user()
    connected = google_calendar_connected(user["id"]) if user else False

    return render_template(
        "google_calendar.html",
        active_page="google_calendar",
        connected=connected,
        user=user,
        credentials_file_exists=google_oauth_configured(),
    )


@app.route("/google-calendar/connect")
def google_calendar_connect():
    if not login_required():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    flow = create_google_flow()
    if flow is None:
        flash(
            "Google OAuth is not configured. Add GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET to the server environment.",
            "danger",
        )
        return redirect(url_for("google_calendar_settings"))

    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    # OAuth PKCE requires the same verifier during the callback token exchange.
    session["google_oauth_state"] = state
    session["google_oauth_user_id"] = current_user()["id"]
    session["google_code_verifier"] = flow.code_verifier

    return redirect(authorization_url)


@app.route("/google-calendar/callback")
def google_calendar_callback():
    if not login_required():
        flash("Your SWMS session expired. Please log in and connect again.", "warning")
        return redirect(url_for("login"))

    expected_state = session.get("google_oauth_state")
    oauth_user_id = session.get("google_oauth_user_id")

    if not expected_state or request.args.get("state") != expected_state:
        flash("Google authorization could not be verified. Please try again.", "danger")
        return redirect(url_for("google_calendar_settings"))

    if oauth_user_id != current_user()["id"]:
        flash("Google authorization user mismatch.", "danger")
        return redirect(url_for("google_calendar_settings"))

    try:
        code_verifier = session.get("google_code_verifier")

        if not code_verifier:
            flash(
                "Google authorization session expired. Please connect again.",
                "danger",
            )
            return redirect(url_for("google_calendar_settings"))

        flow = create_google_flow(
            state=expected_state,
            code_verifier=code_verifier,
        )
        if flow is None:
            raise RuntimeError("Google OAuth environment variables are missing.")

        flow.fetch_token(
            authorization_response=request.url,
        )

        save_google_credentials(
            current_user()["id"],
            flow.credentials,
        )

        add_audit_entry(
            f"{current_user()['name']} connected Google Calendar.",
            "Integrations",
            "GOOGLE_CALENDAR_CONNECT",
            "Success",
        )

        flash("Google Calendar connected successfully.", "success")
    except Exception as error:
        flash(
            f"Google Calendar connection failed: {error}",
            "danger",
        )
    finally:
        session.pop("google_oauth_state", None)
        session.pop("google_oauth_user_id", None)
        session.pop("google_code_verifier", None)

    return redirect(url_for("google_calendar_settings"))


@app.route("/google-calendar/disconnect", methods=["POST"])
def google_calendar_disconnect():
    if not login_required():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    token_file = google_token_path(current_user()["id"])

    if token_file.exists():
        token_file.unlink()

        add_audit_entry(
            f"{current_user()['name']} disconnected Google Calendar.",
            "Integrations",
            "GOOGLE_CALENDAR_DISCONNECT",
            "Success",
        )

        flash("Google Calendar disconnected.", "success")
    else:
        flash("Google Calendar is already disconnected.", "warning")

    return redirect(url_for("google_calendar_settings"))


@app.route("/google-calendar/test", methods=["POST"])
def google_calendar_test():
    if not login_required():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    tomorrow = date.today() + timedelta(days=1)
    result = create_google_calendar_event(
        current_user()["id"],
        {
            "summary": "SWMS Google Calendar Test",
            "description": "This test event confirms that SWMS can access your calendar.",
            "start": {"date": tomorrow.isoformat()},
            "end": {"date": (tomorrow + timedelta(days=1)).isoformat()},
        },
    )

    if result["success"]:
        flash("Test event was added to your Google Calendar.", "success")
    else:
        flash(
            f"Calendar test failed: {result['error'] or result['status']}",
            "danger",
        )

    return redirect(url_for("google_calendar_settings"))


@app.route("/manager-dashboard")
def manager_dashboard():
    if not role_required("manager"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    search = request.args.get("search", "").strip().lower()
    leave_type = request.args.get("leave_type", "All")
    status = request.args.get("status", "All")
    date_range = request.args.get("date_range", "").strip()

    leaves = LEAVE_REQUESTS
    if search:
        leaves = [
            leave for leave in leaves
            if search in leave["employee_name"].lower()
            or search in leave["leave_type"].lower()
        ]
    if leave_type != "All":
        leaves = [leave for leave in leaves if leave["leave_type"] == leave_type]
    if status != "All":
        leaves = [leave for leave in leaves if leave["status"] == status]
    if date_range:
        range_parts = [part.strip() for part in date_range.replace('–', '-').replace('to', '-').split('-') if part.strip()]
        if len(range_parts) == 2:
            start_filter = range_parts[0]
            end_filter = range_parts[1]
            leaves = [
                leave for leave in leaves
                if leave["start_date"] >= start_filter and leave["end_date"] <= end_filter
            ]

    pending_approvals = sum(1 for leave in LEAVE_REQUESTS if leave["status"] == "Pending")
    overdue_tasks = sum(1 for task in TASKS if task["status"] == "Overdue")
    completion_rate = int(sum(task["progress"] for task in TASKS) / max(1, len(TASKS)))

    task_chart_labels = [task["title"] for task in TASKS]
    task_chart_values = [task["progress"] for task in TASKS]
    month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_counts = [0] * 12
    for leave in LEAVE_REQUESTS:
        try:
            monthly_counts[datetime.strptime(leave["start_date"], "%Y-%m-%d").month - 1] += 1
        except (KeyError, ValueError):
            pass
    leave_trend_labels = month_labels
    leave_trend_values = monthly_counts
    team_members = [employee for employee in EMPLOYEES if employee["status"] == "Active"]

    return render_template(
        "manager_dashboard.html",
        active_page="dashboard",
        user=current_user(),
        leave_requests=leaves,
        tasks=TASKS,
        pending_approvals=pending_approvals,
        overdue_tasks=overdue_tasks,
        completion_rate=completion_rate,
        task_chart_labels=task_chart_labels,
        task_chart_values=task_chart_values,
        leave_trend_labels=leave_trend_labels,
        leave_trend_values=leave_trend_values,
        employees=team_members,
        search=search,
        selected_leave_type=leave_type,
        selected_status=status,
        selected_date_range=date_range,
    )


@app.route("/manager-performance")
def manager_performance():
    if not role_required("manager", "admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    employee_records = [
        employee for employee in EMPLOYEES
        if employee.get("role", "").strip().lower() == "employee"
        and employee.get("status", "Active") != "Inactive"
    ]

    performance_data = [
        calculate_employee_performance(employee["name"])
        for employee in employee_records
    ]

    performance_data.sort(
        key=lambda item: (
            item["total_tasks"] > 0,
            item["performance_score"],
            item["average_progress"],
        ),
        reverse=True,
    )

    rank = 1
    for result in performance_data:
        if result["total_tasks"] > 0:
            result["rank"] = rank
            rank += 1
        else:
            result["rank"] = "-"

    scored_employees = [
        result for result in performance_data
        if result["total_tasks"] > 0
    ]

    average_score = (
        round(
            sum(result["performance_score"] for result in scored_employees)
            / len(scored_employees)
        )
        if scored_employees
        else 0
    )

    top_performer = scored_employees[0] if scored_employees else None

    needs_improvement = sum(
        1 for result in scored_employees
        if result["performance_score"] < 50
    )

    return render_template(
        "manager_performance.html",
        active_page="performance",
        performance_data=performance_data,
        total_employees=len(employee_records),
        employees_analysed=len(scored_employees),
        average_score=average_score,
        top_performer=top_performer,
        needs_improvement=needs_improvement,
        chart_labels=[
            result["employee_name"]
            for result in scored_employees
        ],
        chart_values=[
            result["performance_score"]
            for result in scored_employees
        ],
    )


@app.route("/manager-leave")
def manager_leave():
    if not role_required("manager"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    return render_template(
        "manager_leave.html",
        active_page="leave",
        leave_requests=LEAVE_REQUESTS,
    )


@app.route("/manager-tasks")
def manager_tasks():
    if not role_required("manager"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    return render_template(
        "manager_tasks.html",
        active_page="tasks",
        tasks=TASKS,
    )


@app.route("/manager-calendar")
def manager_calendar():
    if not role_required("manager"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    return render_template(
        "manager_calendar.html",
        active_page="calendar",
        tasks=TASKS,
    )


@app.route("/manager-profile")
def manager_profile():
    if not role_required("manager"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    return render_template(
        "manager_profile.html",
        active_page="profile",
        user=current_user(),
    )


@app.route("/manager-settings", methods=["GET", "POST"])
def manager_settings():
    if not role_required("manager"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        SETTINGS["notifications"]["email"] = request.form.get("email_notifications") == "on"
        SETTINGS["notifications"]["whatsapp"] = request.form.get("whatsapp_notifications") == "on"
        SETTINGS["notifications"]["reminders"] = request.form.get("reminders") == "on"
        SETTINGS["system"]["timezone"] = request.form.get("timezone", SETTINGS["system"]["timezone"])
        SETTINGS["system"]["workweek"] = request.form.get("workweek", SETTINGS["system"]["workweek"])
        add_audit_entry(
            f"{current_user()['name']} updated manager settings.",
            "Settings",
            "UPDATE",
            "Success",
        )
        flash("Manager settings saved.", "success")
        return redirect(url_for("manager_settings"))

    return render_template(
        "manager_settings.html",
        active_page="settings",
        settings=SETTINGS,
    )


@app.route("/task/add", methods=["POST"])
@app.route("/manager/tasks/create", methods=["POST"])
def create_task():
    if not role_required("manager", "admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    assigned_to = request.form.get("assigned_to", "").strip()
    priority = request.form.get("priority", "Medium").strip()
    deadline = request.form.get("deadline", "").strip()

    if not title or not assigned_to or not priority or not deadline:
        flash("Please complete all task wizard fields.", "danger")
        return redirect(url_for("manager_dashboard"))

    if not any(employee["name"] == assigned_to for employee in EMPLOYEES):
        flash("Assigned employee not found.", "danger")
        return redirect(url_for("manager_dashboard"))

    TASKS.append(
        {
            "id": next_id(TASKS),
            "title": title,
            "description": description,
            "assigned_to": assigned_to,
            "priority": priority,
            "deadline": deadline,
            "progress": 0,
            "status": "Not Started",
        }
    )

    # The connected manager/admin is the organizer. The assigned employee
    # receives a Google Calendar invitation email.
    calendar_result = sync_task_calendar_invitation(
        TASKS[-1],
        current_user(),
    )

    assigned_employee = employee_by_name(assigned_to)
    whatsapp_result = send_task_assignment(assigned_employee or {}, TASKS[-1])
    TASKS[-1]["whatsapp_notification"] = whatsapp_result

    add_notification(
        "employee",
        "New task assigned",
        f"{title} is due on {deadline}.",
        url_for("employee_tasks"),
        assigned_to,
    )
    add_audit_entry(
        f"{current_user()['name']} created task '{title}'.",
        "Task Management",
        "CREATE",
        "Success",
    )
    delivery_notes = []
    delivery_category = "success"

    if calendar_result.get("event_id"):
        delivery_notes.append("Google Calendar invitation sent")
    elif calendar_result.get("status") == "Organizer Not Connected":
        delivery_notes.append("Calendar not sent because the organizer is not connected")
        delivery_category = "warning"
    else:
        delivery_notes.append(
            "Calendar failed: "
            f"{calendar_result.get('error') or calendar_result.get('status')}"
        )
        delivery_category = "warning"

    if whatsapp_result.get("success"):
        delivery_notes.append("WhatsApp notification sent")
    else:
        delivery_notes.append(
            "WhatsApp not sent: "
            f"{whatsapp_result.get('error') or whatsapp_result.get('status')}"
        )
        delivery_category = "warning"

    persist_data()
    flash("Task created. " + "; ".join(delivery_notes) + ".", delivery_category)

    return redirect(url_for("manager_dashboard"))


@app.route("/leave/<int:leave_id>/<string:action>", methods=["POST"])
def manage_leave(leave_id, action):
    if not role_required("manager", "admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    leave = next((item for item in LEAVE_REQUESTS if item["id"] == leave_id), None)
    if not leave:
        flash("Leave request not found.", "danger")
        return redirect(url_for("manager_dashboard"))
    if leave.get("status") != "Pending":
        flash("Only pending leave requests can be approved or rejected.", "warning")
        return redirect(url_for("manager_dashboard"))

    employee = employee_by_name(leave["employee_name"])
    calendar_result = None
    if action == "approve":
        if not employee:
            flash("Employee profile was not found.", "danger")
            return redirect(url_for("manager_dashboard"))

        # Calculate the duration again from the stored dates. This also supports
        # older JSON records that were created before the duration field existed.
        required = calculate_duration(leave.get("start_date", ""), leave.get("end_date", ""))
        if required is None:
            flash("Cannot approve: the leave dates are invalid.", "danger")
            return redirect(url_for("manager_dashboard"))
        leave["duration"] = required

        leave_type = leave.get("leave_type", "Annual Leave").strip()
        normalized_leave_type = "Sick Leave" if leave_type == "Medical Leave" else leave_type
        balances = ensure_employee_leave_balances(employee)
        balance = balances.get(normalized_leave_type)

        if not leave.get("balance_deducted", False):
            if normalized_leave_type == "Unpaid Leave":
                previous_used = int(balance.get("used", 0) or 0)
                balance["used"] = previous_used + required
                leave["balance_before_approval"] = previous_used
                leave["balance_after_approval"] = balance["used"]
            else:
                previous_balance = int(balance.get("remaining", 0) or 0)
                if required > previous_balance:
                    flash(
                        f"Cannot approve: employee has only {previous_balance} {normalized_leave_type.lower()} day(s), "
                        f"but this request requires {required} day(s).",
                        "danger",
                    )
                    return redirect(url_for("manager_dashboard"))
                balance["remaining"] = previous_balance - required
                balance["used"] = int(balance.get("used", 0) or 0) + required
                leave["balance_before_approval"] = previous_balance
                leave["balance_after_approval"] = balance["remaining"]

            employee["leave_balance"] = balances["Annual Leave"]["remaining"]
            leave["balance_deducted"] = True
            leave["deducted_days"] = required
            leave["deducted_leave_type"] = normalized_leave_type

        leave["status"] = "Approved"
        status = "Success"

        # Persist immediately instead of relying on a later audit-log write.
        persist_data()

        # The approving manager/admin organizes one event. Employee, other
        # managers and admins receive Google invitation emails.
        calendar_result = sync_approved_leave_calendar_invitation(
            leave,
            current_user(),
        )
    elif action == "reject":
        leave["status"] = "Rejected"
        rejection_reason = request.form.get("rejection_reason", "").strip()
        if rejection_reason:
            leave["rejection_reason"] = rejection_reason
        status = "Success"
    else:
        flash("Invalid action.", "danger")
        return redirect(url_for("manager_dashboard"))

    whatsapp_result = send_leave_status(employee or {}, leave)
    leave["whatsapp_notification"] = whatsapp_result

    add_notification("employee", f"Leave {leave['status'].lower()}", f"Your {leave['leave_type']} request was {leave['status'].lower()}.", url_for("employee_leave"), leave["employee_name"])
    if action == "approve" and leave.get("balance_deducted"):
        audit_message = (
            f"{current_user()['name']} approved leave request {leave_id} for {leave['employee_name']} "
            f"and deducted {leave.get('deducted_days', leave.get('duration', 0))} day(s) "
            f"from {leave.get('deducted_leave_type', leave.get('leave_type', 'leave'))} "
            f"({leave.get('balance_before_approval')} → {leave.get('balance_after_approval')})."
        )
        success_message = (
            f"Leave approved. {leave.get('deducted_days', leave.get('duration', 0))} day(s) deducted; "
            f"updated balance: {leave.get('balance_after_approval')} day(s)."
        )

        if calendar_result and calendar_result.get("event_id"):
            success_message += " Google Calendar invitations were sent."
        elif calendar_result:
            success_message += (
                " Calendar invitation was not sent: "
                f"{calendar_result.get('error') or calendar_result.get('status')}."
            )
    else:
        audit_message = f"{current_user()['name']} {action}d leave request {leave_id} for {leave['employee_name']}."
        success_message = "Leave request updated successfully."

    if whatsapp_result.get("success"):
        success_message += " WhatsApp notification was sent."
    else:
        success_message += (
            " WhatsApp notification was not sent: "
            f"{whatsapp_result.get('error') or whatsapp_result.get('status')}."
        )

    add_audit_entry(audit_message, "Leave Management", action.upper(), status)
    persist_data()
    flash(success_message, "success" if whatsapp_result.get("success") else "warning")
    return redirect(url_for("manager_dashboard"))


@app.route("/admin-dashboard")
def admin_dashboard():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    section = request.args.get("section", "employees")
    search = request.args.get("search", "").strip().lower()
    department = request.args.get("department", "All")
    role_filter = request.args.get("role", "All")
    status = request.args.get("status", "All")

    filtered_employees = EMPLOYEES
    if search:
        filtered_employees = [
            emp for emp in filtered_employees
            if search in emp["name"].lower()
            or search in emp["employee_id"].lower()
            or search in emp["email"].lower()
        ]
    if department != "All":
        filtered_employees = [emp for emp in filtered_employees if emp["department"] == department]
    if role_filter != "All":
        filtered_employees = [emp for emp in filtered_employees if emp.get("role", "Employee") == role_filter]
    if status != "All":
        filtered_employees = [emp for emp in filtered_employees if emp["status"] == status]

    total_employees = len(EMPLOYEES)
    department_count = len(DEPARTMENTS)
    task_completion = int(sum(task["progress"] for task in TASKS) / max(1, len(TASKS)))
    active_alerts = sum(1 for note in NOTIFICATIONS if note["status"] not in ["Delivered", "Connected"])

    chart_labels = [dept["name"] for dept in DEPARTMENTS]
    chart_values = [sum(1 for emp in EMPLOYEES if emp["department"] == dept["name"]) for dept in DEPARTMENTS]

    department_metrics = []
    for dept in DEPARTMENTS:
        members = [emp for emp in EMPLOYEES if emp.get("department") == dept["name"]]
        member_names = {emp["name"] for emp in members}
        dept_tasks = [task for task in TASKS if task.get("assigned_to") in member_names]
        completed = sum(1 for task in dept_tasks if task.get("status") == "Completed")
        overdue = sum(1 for task in dept_tasks if task.get("status") == "Overdue")
        completion = int((completed / len(dept_tasks)) * 100) if dept_tasks else 0
        leave_count = sum(1 for leave in LEAVE_REQUESTS if leave.get("employee_name") in member_names and leave.get("status") == "Approved")
        department_metrics.append({
            "id": dept["id"], "name": dept["name"], "head": dept.get("head", "Unassigned"),
            "employees": len(members), "tasks": len(dept_tasks), "completed": completed,
            "overdue": overdue, "completion_rate": completion, "approved_leave": leave_count,
        })
    performance_labels = [metric["name"] for metric in department_metrics]
    performance_values = [metric["completion_rate"] for metric in department_metrics]

    return render_template(
        "admin_dashboard.html",
        active_page="employees",
        section=section,
        employees=filtered_employees,
        leave_requests=LEAVE_REQUESTS,
        tasks=TASKS,
        audit_logs=AUDIT_LOGS,
        integrations=INTEGRATIONS,
        settings=SETTINGS,
        departments=DEPARTMENTS,
        total_employees=total_employees,
        department_count=department_count,
        task_completion=task_completion,
        active_alerts=active_alerts,
        chart_labels=chart_labels,
        chart_values=chart_values,
        department_metrics=department_metrics,
        performance_labels=performance_labels,
        performance_values=performance_values,
        search=search,
        selected_department=department,
        selected_role=role_filter,
        selected_status=status,
    )



@app.route("/admin/departments/add", methods=["POST"])
def admin_add_department():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    name = request.form.get("name", "").strip()
    head = request.form.get("head", "").strip() or "Unassigned"
    if not name:
        flash("Department name is required.", "danger")
    elif any(dept["name"].lower() == name.lower() for dept in DEPARTMENTS):
        flash("A department with that name already exists.", "danger")
    else:
        DEPARTMENTS.append({"id": next_id(DEPARTMENTS), "name": name, "head": head})
        add_audit_entry(f"{current_user()['name']} created department {name}.", "Departments", "CREATE", "Success")
        flash("Department created.", "success")
    return redirect(url_for("admin_dashboard", section="departments"))


@app.route("/admin/departments/<int:department_id>/edit", methods=["POST"])
def admin_edit_department(department_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    department = next((dept for dept in DEPARTMENTS if dept["id"] == department_id), None)
    if not department:
        flash("Department not found.", "danger")
        return redirect(url_for("admin_dashboard", section="departments"))
    old_name = department["name"]
    new_name = request.form.get("name", old_name).strip()
    head = request.form.get("head", department.get("head", "Unassigned")).strip() or "Unassigned"
    duplicate = any(dept["id"] != department_id and dept["name"].lower() == new_name.lower() for dept in DEPARTMENTS)
    if not new_name or duplicate:
        flash("Enter a unique department name.", "danger")
        return redirect(url_for("admin_dashboard", section="departments"))
    department.update({"name": new_name, "head": head})
    for employee in EMPLOYEES:
        if employee.get("department") == old_name:
            employee["department"] = new_name
    add_audit_entry(f"{current_user()['name']} updated department {old_name} to {new_name}.", "Departments", "UPDATE", "Success")
    flash("Department updated.", "success")
    return redirect(url_for("admin_dashboard", section="departments"))


@app.route("/admin/departments/<int:department_id>/delete", methods=["POST"])
def admin_delete_department(department_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))
    department = next((dept for dept in DEPARTMENTS if dept["id"] == department_id), None)
    if not department:
        flash("Department not found.", "danger")
    elif any(emp.get("department") == department["name"] for emp in EMPLOYEES):
        flash("Cannot delete a department that still has employees. Reassign them first.", "danger")
    else:
        DEPARTMENTS.remove(department)
        add_audit_entry(f"{current_user()['name']} deleted department {department['name']}.", "Departments", "DELETE", "Success")
        flash("Department deleted.", "success")
    return redirect(url_for("admin_dashboard", section="departments"))


@app.route("/admin/settings/update", methods=["POST"])
def admin_update_settings():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    SETTINGS["notifications"]["email"] = request.form.get("email_notifications") == "on"
    SETTINGS["notifications"]["whatsapp"] = request.form.get("whatsapp_notifications") == "on"
    SETTINGS["notifications"]["reminders"] = request.form.get("reminders") == "on"
    SETTINGS["system"]["timezone"] = request.form.get("timezone", SETTINGS["system"]["timezone"])
    SETTINGS["system"]["workweek"] = request.form.get("workweek", SETTINGS["system"]["workweek"])

    add_audit_entry(
        f"{current_user()['name']} updated system settings.",
        "Settings",
        "UPDATE",
        "Success",
    )
    flash("System settings saved.", "success")
    return redirect(url_for("admin_dashboard", section="settings"))


@app.route("/admin/employees/add", methods=["GET", "POST"])
def admin_add_employee():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        department = request.form.get("department", "").strip()
        position = request.form.get("position", "").strip()
        phone = request.form.get("phone", "").strip()
        role = request.form.get("role", "Employee").strip()

        if not name or not email or not department or not position:
            flash("Please complete all required employee fields.", "danger")
            return redirect(url_for("admin_add_employee"))

        new_employee = {
            "id": next_id(EMPLOYEES),
            "employee_id": f"EMP-{1000 + next_id(EMPLOYEES)}",
            "name": name,
            "email": email,
            "department": department,
            "position": position,
            "phone": phone,
            "status": "Active",
            "role": role,
            "joined": date.today().strftime("%d %b %Y"),
            "leave_balance": LEAVE_DEFAULTS["Annual Leave"],
            "leave_balances": {
                leave_name: {
                    "allocated": allocation,
                    "used": 0,
                    "remaining": allocation if leave_name != "Unpaid Leave" else 0,
                }
                for leave_name, allocation in LEAVE_DEFAULTS.items()
            },
        }
        EMPLOYEES.append(new_employee)
        USERS.append(
            {
                "id": next_id(USERS),
                "name": name,
                "email": email,
                "password": generate_password_hash("1234"),
                "role": role.lower(),
            }
        )
        add_audit_entry(
            f"{current_user()['name']} created employee {name}.",
            "Employee Records",
            "CREATE",
            "Success",
        )
        flash("Employee created successfully.", "success")
        return redirect(url_for("admin_dashboard", section="employees"))

    return render_template(
        "admin_employee_form.html",
        active_page="employees",
        form_action=url_for("admin_add_employee"),
        employee=None,
        departments=DEPARTMENTS,
    )


@app.route("/admin/employees/<int:employee_id>/edit", methods=["GET", "POST"])
def admin_edit_employee(employee_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    employee = next((emp for emp in EMPLOYEES if emp["id"] == employee_id), None)
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("admin_dashboard", section="employees"))

    if request.method == "POST":
        employee["name"] = request.form.get("name", employee["name"]).strip()
        employee["email"] = request.form.get("email", employee["email"]).strip()
        employee["department"] = request.form.get("department", employee["department"]).strip()
        employee["position"] = request.form.get("position", employee["position"]).strip()
        employee["phone"] = request.form.get("phone", employee.get("phone", "")).strip()
        employee["status"] = request.form.get("status", employee["status"]).strip()
        add_audit_entry(
            f"{current_user()['name']} updated employee {employee['name']}.",
            "Employee Records",
            "UPDATE",
            "Success",
        )
        flash("Employee updated successfully.", "success")
        return redirect(url_for("admin_dashboard", section="employees"))

    return render_template(
        "admin_employee_form.html",
        active_page="employees",
        form_action=url_for("admin_edit_employee", employee_id=employee_id),
        employee=employee,
        departments=DEPARTMENTS,
    )


@app.route("/admin/employees/<int:employee_id>/view")
def admin_view_employee(employee_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    employee = next((emp for emp in EMPLOYEES if emp["id"] == employee_id), None)
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("admin_dashboard", section="employees"))

    return render_template(
        "admin_employee_view.html",
        active_page="employees",
        employee=employee,
    )


@app.route("/admin/employees/<int:employee_id>/deactivate", methods=["POST"])
def admin_deactivate_employee(employee_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    employee = next((emp for emp in EMPLOYEES if emp["id"] == employee_id), None)
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("admin_dashboard", section="employees"))

    employee["status"] = "Inactive"
    add_audit_entry(
        f"{current_user()['name']} deactivated {employee['name']}.",
        "Employee Records",
        "UPDATE",
        "Success",
    )
    flash("Employee deactivated.", "success")
    return redirect(url_for("admin_dashboard", section="employees"))


@app.route("/admin/employees/<int:employee_id>/delete", methods=["POST"])
def admin_delete_employee(employee_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    employee = next((emp for emp in EMPLOYEES if emp["id"] == employee_id), None)
    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("admin_dashboard", section="employees"))

    EMPLOYEES.remove(employee)
    user = next((user for user in USERS if user["email"].lower() == employee["email"].lower()), None)
    if user:
        USERS.remove(user)
    add_audit_entry(
        f"{current_user()['name']} deleted employee {employee['name']}.",
        "Employee Records",
        "DELETE",
        "Success",
    )
    flash("Employee deleted.", "success")
    return redirect(url_for("admin_dashboard", section="employees"))


@app.route("/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):
    if not login_required():
        return redirect(url_for("login"))
    note = next((item for item in user_notifications() if item["id"] == notification_id), None)
    if note:
        note["read"] = True
        note["status"] = "Read"
        persist_data()
        target = note.get("link") or url_for("dashboard")
        return redirect(target)
    return redirect(url_for("dashboard"))


@app.route("/notifications/read-all", methods=["POST"])
def mark_all_notifications_read():
    if not login_required():
        return redirect(url_for("login"))
    for note in user_notifications():
        note["read"] = True
        note["status"] = "Read"
    persist_data()
    flash("All notifications marked as read.", "success")
    return redirect(request.referrer or url_for("dashboard"))


def csv_download(filename, headers, rows):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/export/tasks")
def export_tasks():
    if not login_required():
        return redirect(url_for("login"))
    rows = TASKS
    if session.get("role") == "employee":
        rows = [task for task in TASKS if task.get("assigned_to") == session.get("user_name")]
    return csv_download("tasks.csv", ["ID", "Title", "Assigned To", "Priority", "Deadline", "Progress", "Status"],
                        [[t.get("id"), t.get("title"), t.get("assigned_to"), t.get("priority"), t.get("deadline"), t.get("progress"), t.get("status")] for t in rows])


@app.route("/export/employees")
def export_employees():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    output = StringIO()
    output.write("Employee ID,Name,Email,Department,Position,Status,Joined\n")
    for employee in EMPLOYEES:
        output.write(
            f"{employee['employee_id']},{employee['name']},{employee['email']},{employee['department']},{employee['position']},{employee['status']},{employee.get('joined','')}\n"
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=employees.csv"},
    )


@app.route("/export/audit-logs")
def export_audit_logs():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    output = StringIO()
    output.write("Timestamp,Module,Action,Status,Message\n")
    for entry in AUDIT_LOGS:
        output.write(
            f"{entry['timestamp']},{entry['module']},{entry['action']},{entry['status']},{entry['message']}\n"
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=audit_logs.csv"},
    )


@app.route("/export/leave-records")
def export_leave_records():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    output = StringIO()
    output.write("ID,Employee,Type,Start Date,End Date,Duration,Status\n")
    for leave in LEAVE_REQUESTS:
        output.write(
            f"{leave['id']},{leave['employee_name']},{leave['leave_type']},{leave['start_date']},{leave['end_date']},{leave.get('duration','')},{leave['status']}\n"
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=leave_records.csv"},
    )


@app.route("/integrations/toggle", methods=["POST"])
def toggle_integration():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    integration_id = int(request.form.get("integration_id", 0))
    integration = next((item for item in INTEGRATIONS if item["id"] == integration_id), None)
    if not integration:
        flash("Integration not found.", "danger")
        return redirect(url_for("admin_dashboard", section="integrations"))

    integration["enabled"] = not integration["enabled"]
    status = "Enabled" if integration["enabled"] else "Disabled"
    add_audit_entry(
        f"{current_user()['name']} {status.lower()} integration {integration['name']}.",
        "Integrations",
        "UPDATE",
        "Success",
    )
    flash(f"Integration {status}.", "success")
    return redirect(url_for("admin_dashboard", section="integrations"))


@app.route("/settings/update", methods=["POST"])
def update_settings():
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    SETTINGS["system"]["timezone"] = request.form.get("timezone", SETTINGS["system"]["timezone"]).strip()
    SETTINGS["system"]["workweek"] = request.form.get("workweek", SETTINGS["system"]["workweek"]).strip()
    add_audit_entry(
        f"{current_user()['name']} updated system settings.",
        "Settings",
        "UPDATE",
        "Success",
    )
    flash("System settings saved.", "success")
    return redirect(url_for("admin_dashboard", section="settings"))




@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if not login_required():
        flash("Please log in first.", "warning")
        return redirect(url_for("login"))

    user = current_user()
    if not user:
        session.clear()
        flash("Your user account could not be found.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_password or not new_password or not confirm_password:
            flash("Please complete all password fields.", "danger")
            return redirect(url_for("change_password"))

        if not check_password_hash(user["password"], current_password):
            add_audit_entry(
                f"{user['name']} entered an incorrect current password.",
                "Authentication",
                "CHANGE_PASSWORD_FAILED",
                "Failed",
            )
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("change_password"))

        if len(new_password) < 8:
            flash("New password must contain at least 8 characters.", "danger")
            return redirect(url_for("change_password"))

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return redirect(url_for("change_password"))

        if check_password_hash(user["password"], new_password):
            flash("New password cannot be the same as your current password.", "danger")
            return redirect(url_for("change_password"))

        user["password"] = generate_password_hash(new_password)

        add_audit_entry(
            f"{user['name']} changed their account password.",
            "Authentication",
            "CHANGE_PASSWORD",
            "Success",
        )

        session.clear()
        flash("Password changed successfully. Please log in again.", "success")
        return redirect(url_for("login"))

    return render_template(
        "change_password.html",
        active_page="change_password",
        user=user,
    )


@app.route("/admin/employees/<int:employee_id>/reset-password", methods=["GET", "POST"])
def admin_reset_employee_password(employee_id):
    if not role_required("admin"):
        flash("Access denied.", "danger")
        return redirect(url_for("dashboard"))

    employee = next(
        (item for item in EMPLOYEES if item.get("id") == employee_id),
        None,
    )

    if not employee:
        flash("Employee not found.", "danger")
        return redirect(url_for("admin_dashboard", section="employees"))

    user = next(
        (
            item for item in USERS
            if item.get("email", "").strip().lower()
            == employee.get("email", "").strip().lower()
        ),
        None,
    )

    if not user:
        flash("The employee login account was not found.", "danger")
        return redirect(url_for("admin_dashboard", section="employees"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not new_password or not confirm_password:
            flash("Please complete both password fields.", "danger")
            return redirect(
                url_for(
                    "admin_reset_employee_password",
                    employee_id=employee_id,
                )
            )

        if len(new_password) < 8:
            flash("New password must contain at least 8 characters.", "danger")
            return redirect(
                url_for(
                    "admin_reset_employee_password",
                    employee_id=employee_id,
                )
            )

        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "danger")
            return redirect(
                url_for(
                    "admin_reset_employee_password",
                    employee_id=employee_id,
                )
            )

        user["password"] = generate_password_hash(new_password)

        add_notification(
            user.get("role", "employee"),
            "Password reset",
            "Your password was reset by the system administrator.",
            url_for("login"),
            user.get("name", ""),
        )

        add_audit_entry(
            f"{current_user()['name']} reset the password for {employee['name']}.",
            "Authentication",
            "ADMIN_RESET_PASSWORD",
            "Success",
        )

        flash(
            f"Password for {employee['name']} was reset successfully.",
            "success",
        )
        return redirect(url_for("admin_dashboard", section="employees"))

    return render_template(
        "admin_reset_password.html",
        active_page="employees",
        employee=employee,
    )

@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", code=404, title="Page not found", message="The page you requested does not exist."), 404


@app.errorhandler(500)
def server_error(error):
    return render_template("error.html", code=500, title="Something went wrong", message="The system encountered an unexpected error."), 500


if __name__ == "__main__":
    app.run(debug=True)