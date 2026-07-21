"""External notification providers for SWMS.

Credentials are loaded from environment variables or a local .env file.
This module never raises provider errors into the main SWMS workflow; every
send returns a structured result that can be logged and shown to the user.
"""

import os
import re
from datetime import datetime

from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

load_dotenv()


def normalize_whatsapp_number(phone):
    """Convert a Malaysian/international number to Twilio's WhatsApp format."""
    raw = str(phone or "").strip()
    if not raw:
        return ""

    if raw.startswith("whatsapp:"):
        raw = raw.split(":", 1)[1]

    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""

    # Local Malaysian format: 01xxxxxxxx -> +601xxxxxxxx
    if digits.startswith("0"):
        digits = "60" + digits[1:]
    elif not digits.startswith("60") and raw.startswith("+"):
        # Keep other explicit international numbers.
        pass
    elif not digits.startswith("60") and len(digits) in (9, 10):
        # Convenient fallback for numbers entered without 0 or +60.
        digits = "60" + digits

    return f"whatsapp:+{digits}"


def twilio_configuration():
    return {
        "account_sid": os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
        "auth_token": os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
        "from_number": normalize_whatsapp_number(
            os.getenv("TWILIO_WHATSAPP_FROM", "+14155238886")
        ),
        "default_recipient": normalize_whatsapp_number(
            os.getenv("TWILIO_DEFAULT_RECIPIENT", "")
        ),
    }


def send_whatsapp(phone, body):
    """Send one WhatsApp message and return a safe status dictionary."""
    config = twilio_configuration()
    recipient = normalize_whatsapp_number(phone) or config["default_recipient"]

    if not config["account_sid"] or not config["auth_token"]:
        return {
            "success": False,
            "status": "Not Configured",
            "message_sid": "",
            "recipient": recipient,
            "error": "TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is missing.",
            "sent_at": "",
        }

    if not recipient:
        return {
            "success": False,
            "status": "Phone Missing",
            "message_sid": "",
            "recipient": "",
            "error": "No employee WhatsApp number is available.",
            "sent_at": "",
        }

    try:
        client = Client(config["account_sid"], config["auth_token"])
        message = client.messages.create(
            from_=config["from_number"],
            to=recipient,
            body=body,
        )
        return {
            "success": True,
            "status": "Sent",
            "message_sid": message.sid,
            "recipient": recipient,
            "error": "",
            "sent_at": datetime.now().isoformat(timespec="seconds"),
        }
    except TwilioRestException as error:
        return {
            "success": False,
            "status": "Send Failed",
            "message_sid": "",
            "recipient": recipient,
            "error": f"Twilio {error.code}: {error.msg}",
            "sent_at": "",
        }
    except Exception as error:
        return {
            "success": False,
            "status": "Send Failed",
            "message_sid": "",
            "recipient": recipient,
            "error": str(error),
            "sent_at": "",
        }


def send_task_assignment(employee, task):
    name = employee.get("name", "Employee")
    body = (
        "📌 *SWMS — New Task Assigned*\n\n"
        f"Hello {name},\n"
        "You have been assigned a new task.\n\n"
        f"*Task:* {task.get('title', '-')}\n"
        f"*Priority:* {task.get('priority', '-')}\n"
        f"*Deadline:* {task.get('deadline', '-')}\n\n"
        "Please log in to SWMS for full details."
    )
    return send_whatsapp(employee.get("phone", ""), body)


def send_leave_status(employee, leave):
    status = leave.get("status", "Updated")
    icon = "✅" if status == "Approved" else "❌" if status == "Rejected" else "ℹ️"
    name = employee.get("name", leave.get("employee_name", "Employee"))

    lines = [
        f"{icon} *SWMS — Leave {status}*",
        "",
        f"Hello {name},",
        f"Your {leave.get('leave_type', 'leave')} application has been *{status.lower()}*.",
        "",
        f"*Start:* {leave.get('start_date', '-')}",
        f"*End:* {leave.get('end_date', '-')}",
        f"*Duration:* {leave.get('duration', '-')} day(s)",
    ]

    if status == "Rejected" and leave.get("rejection_reason"):
        lines.extend(["", f"*Reason:* {leave['rejection_reason']}"])

    lines.extend(["", "Please log in to SWMS for more details."])
    return send_whatsapp(employee.get("phone", ""), "\n".join(lines))
