SWMS FIXED VERSION
==================

This version fixes the duplicate role migration error:
  Duplicate entry 'employee' for key 'roles.uq_roles_key'

Do NOT delete or re-import the database again.

Run:
1. Open this SWMS folder in VS Code.
2. Confirm .env has DB_HOST, DB_PORT, DB_USER, DB_PASSWORD and DB_NAME.
3. Double-click START_SWMS.bat, or run:
     python -m pip install -r requirements.txt
     python db.py
     python app.py

Expected first successful start:
  Existing SWMS data migrated to MySQL.

The existing UI, Google Calendar and Twilio WhatsApp code are preserved.
JSON remains only as an emergency backup; MySQL stores the application state.
