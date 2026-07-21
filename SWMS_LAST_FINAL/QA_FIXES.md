# SWMS QA Fixes

## Fixed
- Manager dashboard leave search/type/status/date filters now work client-side without page reload.
- Admin employee search/department/role/status filters work together.
- Admin employee sort toggles A-Z / Z-A and updates the visible result count.
- Empty-state messages are shown when filters return no records.
- Manager and Admin charts use a fixed responsive wrapper (280px desktop, 240px mobile).
- Existing Chart.js instances are destroyed before re-creation to avoid chart growth/duplication.
- Legacy empty/hash links no longer jump the page to the top.
- Buttons without a type are normalized to `type="button"` to prevent accidental form submission.
- Admin sidebar now links to Overview, Employee Records, Departments, Leave Records, Task Records, Audit Logs, Global Analytics, Integrations, Settings, and Logout.
- Missing Admin sections are rendered inside the existing Admin dashboard shell.
- Audit search/action/module filtering is connected.

## Validation performed
- Python syntax check passed for `app.py`.
- JavaScript syntax check passed for `static/js/app.js`.
- All 20 Jinja templates parsed successfully.
- Employee, Manager, and Admin routes returned HTTP 200 in Flask test-client checks.

## Run
1. Create/activate your virtual environment.
2. `pip install -r requirements.txt`
3. `python app.py`
4. Open `http://127.0.0.1:5000`
5. Hard refresh with `Ctrl + Shift + R`.
