# Dynamic Leave Balance Fix

This final update changes employee leave balances from static dashboard values to per-employee dynamic balances.

Defaults for a newly created employee:
- Annual Leave: 18 days
- Sick Leave: 14 days
- Emergency Leave: 5 days
- Unpaid Leave Taken: 0 days

Behaviour:
- New and existing employees are initialized automatically when the application starts.
- Dashboard values and progress bars are dynamic.
- Approval deducts only the selected leave type.
- Unpaid leave is recorded as days taken and does not reduce paid leave.
- MySQL `leave_types` and `leave_balances` are synchronized automatically.
- Existing Google Calendar, email invitation, Twilio WhatsApp, task, audit and UI workflows are preserved.

Run normally:
1. Ensure `.env` contains the working MySQL and Twilio credentials.
2. Run `python app.py`.
3. Log in as the newly created employee and refresh the dashboard.

Expected new employee values: 18 Annual, 14 Sick, 5 Emergency and 0 Unpaid.
