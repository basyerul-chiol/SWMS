# SWMS Core Feature Upgrade (No MySQL)

Implemented:

1. Leave balance and approval logic
   - Annual leave balance on employee records
   - Balance displayed on My Leave
   - Balance deducted only after approval
   - Duplicate approval prevented
   - Insufficient balance prevents submission/approval

2. Leave validation
   - Required fields
   - Valid date order
   - Past-date prevention
   - Overlapping pending/approved leave prevention

3. Department CRUD and analytics
   - Add, edit and delete departments
   - Department names cascade to employee records when renamed
   - Deletion blocked while employees remain assigned
   - Department employee/task/completion/overdue/leave metrics
   - Department completion chart

4. Authentication audit history
   - Successful login
   - Failed login
   - Logout
   - Actor, role, module, action, timestamp, status and local IP
   - Audit filtering and CSV export remain available

All changes continue to use JSON persistence and do not require MySQL.
