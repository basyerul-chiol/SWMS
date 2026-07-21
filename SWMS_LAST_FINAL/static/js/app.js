document.addEventListener('DOMContentLoaded', function () {
    const normalize = (value) => String(value || '').trim().toLowerCase();

    function createToast(message) {
        let toastContainer = document.querySelector('.toast-container');
        if (!toastContainer) {
            toastContainer = document.createElement('div');
            toastContainer.className = 'toast-container';
            document.body.appendChild(toastContainer);
        }
        const toast = document.createElement('div');
        toast.className = 'toast-message';
        toast.textContent = message;
        toastContainer.appendChild(toast);
        window.setTimeout(() => toast.remove(), 3000);
    }

    document.querySelectorAll('button:not([type])').forEach((button) => {
        button.type = 'button';
    });

    const togglePasswordButton = document.querySelector('.password-toggle');
    const passwordInput = document.querySelector('#passwordInput');
    if (togglePasswordButton && passwordInput) {
        togglePasswordButton.addEventListener('click', function (event) {
            event.preventDefault();
            passwordInput.type = passwordInput.type === 'password' ? 'text' : 'password';
            togglePasswordButton.textContent = passwordInput.type === 'password' ? 'Show' : 'Hide';
        });
    }

    // Prevent legacy placeholder anchors from jumping to the top.
    document.querySelectorAll('a[href="#"], a[href=""]').forEach((link) => {
        link.addEventListener('click', function (event) {
            event.preventDefault();
            createToast('This control is not connected yet.');
        });
    });

    // Employee leave modal controls.
    const applyLeaveButton = document.querySelector('.topbar-btn[data-open-leave], .topbar-btn');
    const leaveModal = document.querySelector('.apply-modal');
    if (applyLeaveButton && leaveModal) {
        leaveModal.classList.add('modal-hidden');
        applyLeaveButton.addEventListener('click', function (event) {
            if (applyLeaveButton.tagName === 'A' && applyLeaveButton.getAttribute('href')) return;
            event.preventDefault();
            leaveModal.classList.remove('modal-hidden');
        });
        leaveModal.querySelectorAll('[data-close-modal], .btn-secondary').forEach((button) => {
            button.type = 'button';
            button.addEventListener('click', function (event) {
                event.preventDefault();
                leaveModal.classList.add('modal-hidden');
            });
        });
    }

    function setEmptyState(tbody, id, colspan, visibleCount, message) {
        let empty = document.getElementById(id);
        if (!empty) {
            empty = document.createElement('tr');
            empty.id = id;
            empty.innerHTML = `<td colspan="${colspan}" class="empty-filter-state">${message}</td>`;
            tbody.appendChild(empty);
        }
        empty.hidden = visibleCount !== 0;
    }

    // Manager leave filtering.
    const managerForm = document.getElementById('managerFilterForm');
    const managerTableBody = document.querySelector('.approval-table tbody');
    if (managerForm && managerTableBody) {
        const search = managerForm.querySelector('[name="search"]');
        const type = managerForm.querySelector('[name="leave_type"]');
        const status = managerForm.querySelector('[name="status"]');
        const date = managerForm.querySelector('[name="date_range"]');
        const apply = document.getElementById('managerFilterApply');
        const rows = Array.from(managerTableBody.querySelectorAll('tr.leave-row'));

        const filter = function (event) {
            if (event) event.preventDefault();
            const q = normalize(search?.value);
            const selectedType = normalize(type?.value);
            const selectedStatus = normalize(status?.value);
            const dateValue = normalize(date?.value);
            let visible = 0;

            rows.forEach((row) => {
                const employee = normalize(row.dataset.employee);
                const leaveType = normalize(row.dataset.leaveType);
                const leaveStatus = normalize(row.dataset.status);
                const start = normalize(row.dataset.startDate);
                const end = normalize(row.dataset.endDate);
                const matchesSearch = !q || employee.includes(q) || leaveType.includes(q);
                const matchesType = !selectedType || selectedType === 'all' || leaveType === selectedType;
                const matchesStatus = !selectedStatus || selectedStatus === 'all' || leaveStatus === selectedStatus;
                const matchesDate = !dateValue || start.includes(dateValue) || end.includes(dateValue) || `${start} ${end}`.includes(dateValue);
                const show = matchesSearch && matchesType && matchesStatus && matchesDate;
                row.hidden = !show;
                if (show) visible += 1;
            });
            setEmptyState(managerTableBody, 'managerLeaveEmptyState', 6, visible, 'No matching leave requests found.');
            const count = document.getElementById('managerLeaveCount');
            if (count) count.textContent = `${visible} request${visible === 1 ? '' : 's'}`;
        };

        managerForm.addEventListener('submit', filter);
        search?.addEventListener('input', filter);
        type?.addEventListener('change', filter);
        status?.addEventListener('change', filter);
        date?.addEventListener('input', filter);
        apply?.addEventListener('click', filter);
        filter();
    }

    // Admin employee filtering and sorting.
    const adminForm = document.getElementById('adminDirectoryFilterForm');
    const adminBody = document.querySelector('.directory-table tbody');
    if (adminForm && adminBody) {
        const search = adminForm.querySelector('[name="search"]');
        const department = adminForm.querySelector('[name="department"]');
        const role = adminForm.querySelector('[name="role"]');
        const status = adminForm.querySelector('[name="status"]');
        const apply = document.getElementById('adminFilterApply');
        const sort = document.getElementById('adminSortButton');
        let ascending = true;
        let rows = Array.from(adminBody.querySelectorAll('tr.employee-row'));

        const filter = function (event) {
            if (event) event.preventDefault();
            const q = normalize(search?.value);
            const selectedDepartment = normalize(department?.value);
            const selectedRole = normalize(role?.value);
            const selectedStatus = normalize(status?.value);
            let visible = 0;
            rows.forEach((row) => {
                const matchesSearch = !q || normalize(row.dataset.name).includes(q) || normalize(row.dataset.employeeId).includes(q);
                const matchesDepartment = !selectedDepartment || selectedDepartment === 'all' || normalize(row.dataset.department) === selectedDepartment;
                const matchesRole = !selectedRole || selectedRole === 'all' || normalize(row.dataset.role) === selectedRole;
                const matchesStatus = !selectedStatus || selectedStatus === 'all' || normalize(row.dataset.status) === selectedStatus;
                const show = matchesSearch && matchesDepartment && matchesRole && matchesStatus;
                row.hidden = !show;
                if (show) visible += 1;
            });
            setEmptyState(adminBody, 'adminEmployeeEmptyState', 6, visible, 'No matching employees found.');
            const count = document.getElementById('adminEmployeeCount');
            if (count) count.textContent = `Showing ${visible} of ${rows.length}`;
        };

        adminForm.addEventListener('submit', filter);
        search?.addEventListener('input', filter);
        department?.addEventListener('change', filter);
        role?.addEventListener('change', filter);
        status?.addEventListener('change', filter);
        apply?.addEventListener('click', filter);
        sort?.addEventListener('click', function (event) {
            event.preventDefault();
            rows.sort((a, b) => normalize(a.dataset.name).localeCompare(normalize(b.dataset.name)) * (ascending ? 1 : -1));
            rows.forEach((row) => adminBody.appendChild(row));
            ascending = !ascending;
            sort.textContent = ascending ? 'Sort A–Z' : 'Sort Z–A';
            filter();
        });
        filter();
    }

    // Generic audit filters.
    const auditRows = Array.from(document.querySelectorAll('.audit-entry'));
    const auditControls = document.querySelector('.audit-filter-row');
    if (auditRows.length && auditControls) {
        const search = auditControls.querySelector('input[type="search"]');
        const selects = auditControls.querySelectorAll('select');
        const action = selects[0];
        const module = selects[1];
        const filter = function () {
            const q = normalize(search?.value);
            const a = normalize(action?.value).replace('action type:', '').trim();
            const m = normalize(module?.value).replace('module:', '').trim();
            auditRows.forEach((row) => {
                const show = (!q || normalize(row.textContent).includes(q)) && (!a || a === 'all' || normalize(row.dataset.action) === a) && (!m || m === 'all' || normalize(row.dataset.module) === m);
                row.hidden = !show;
            });
        };
        search?.addEventListener('input', filter);
        action?.addEventListener('change', filter);
        module?.addEventListener('change', filter);
    }

    // Task tabs remain on-page and never submit a form.
    const taskTabs = document.querySelectorAll('.task-tabs .tab');
    const taskItems = document.querySelectorAll('[data-task-status]');
    taskTabs.forEach((tab) => {
        tab.type = 'button';
        tab.addEventListener('click', function (event) {
            event.preventDefault();
            taskTabs.forEach((item) => item.classList.remove('tab--active'));
            tab.classList.add('tab--active');
            const wanted = normalize(tab.dataset.status || tab.textContent);
            taskItems.forEach((item) => {
                const current = normalize(item.dataset.taskStatus);
                item.hidden = !(wanted === 'all' || current === wanted);
            });
        });
    });
});

// Phase 2 notification centre and safer destructive actions.
document.addEventListener('DOMContentLoaded', function () {
    const centre = document.querySelector('[data-notification-centre]');
    const toggle = document.querySelector('[data-notification-toggle]');
    const menu = document.querySelector('[data-notification-menu]');
    if (centre && toggle && menu) {
        toggle.addEventListener('click', function (event) {
            event.stopPropagation();
            menu.hidden = !menu.hidden;
            toggle.setAttribute('aria-expanded', String(!menu.hidden));
        });
        document.addEventListener('click', function (event) {
            if (!centre.contains(event.target)) {
                menu.hidden = true;
                toggle.setAttribute('aria-expanded', 'false');
            }
        });
    }

    document.querySelectorAll('form[data-confirm], form[action*="/delete"], form[action*="/deactivate"]').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            const message = form.dataset.confirm || 'Are you sure you want to continue?';
            if (!window.confirm(message)) event.preventDefault();
        });
    });

    document.querySelectorAll('form').forEach(function (form) {
        form.addEventListener('submit', function () {
            const submit = form.querySelector('button[type="submit"], input[type="submit"]');
            if (submit) {
                submit.classList.add('is-loading');
                submit.setAttribute('aria-busy', 'true');
            }
        });
    });
});
