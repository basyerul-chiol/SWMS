# Leave Balance Deduction Fix

- Recalculates duration from stored start/end dates during approval.
- Deducts paid leave types from the shared leave balance; `Unpaid Leave` does not deduct.
- Prevents duplicate deductions through `balance_deducted`.
- Persists the new balance immediately to `storage/swms_data.json`.
- Stores before/after balance values and shows the remaining balance after approval.
