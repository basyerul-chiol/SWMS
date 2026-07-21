# SWMS MySQL Migration

This edition keeps the existing UI and business workflows while making MySQL
its primary persistent store.

## Run

1. Import `swms_database_rewritten.sql` into MySQL as `swms_db`.
2. Fill the database and Twilio values in `.env`.
3. Run:

```powershell
pip install -r requirements.txt
python db.py
python app.py
```

On first launch, the current `storage/swms_data.json` data is migrated into
MySQL. Later launches load from MySQL. The JSON file remains an emergency
backup only.

## Verification queries

```sql
USE swms_db;
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM tasks;
SELECT COUNT(*) FROM leave_requests;
SELECT COUNT(*) FROM notifications;
SELECT updated_at FROM swms_state WHERE state_key='application_state';
```
