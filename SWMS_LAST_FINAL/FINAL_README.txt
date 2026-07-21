SWMS LAST FINAL
===============

This is the cleaned final project folder.

Included and preserved:
- Existing UI and routes
- MySQL primary persistence
- Employee, leave, task, performance, settings and dashboard workflows
- Leave-balance deduction
- Google Calendar invitations
- Twilio WhatsApp notifications
- Emergency JSON backup only if MySQL is unavailable

Before running:
1. Copy the .env file from your currently working SWMS folder into this folder.
2. Run START_SWMS.bat.

Do not import the SQL again if swms_db already contains the tables.
Do not run app_new.py or any backup application file; only app.py is included.
