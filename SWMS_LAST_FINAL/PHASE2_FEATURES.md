# SWMS Phase 2 (No MySQL)

## Added
- Role-aware notification centre with unread badge, dropdown, mark-read, and mark-all-read.
- Workflow notifications for leave submission/decision/cancellation and task assignment/completion.
- Dynamic manager leave trend and task deadline/overdue calculation.
- Expanded audit entries with actor, role, module, action, status, and timestamp.
- CSV export for task records while preserving the existing employee, leave, and audit exports.
- JSON persistence in `storage/swms_data.json`; it is created automatically after the first data-changing action.
- Confirmation prompts for delete/deactivate actions, submit loading feedback, and polished 404/500 pages.
- Existing filters, chart sizing, dashboard UI, and redesigned settings retained.

## Demo accounts
- Employee: `basyerul@gmail.com` / `1234`
- Manager: `manager@swms.com` / `1234`
- Admin: `admin@swms.com` / `1234`

## Run
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

## Reset demo data
Stop Flask and delete `storage/swms_data.json`. The next launch uses the default data in `data.py`.

## MySQL later
`storage.py` is isolated from the routes. It can later be replaced by a MySQL repository layer without redesigning the templates.
