(function (root) {
    'use strict';

    root.initUserImport = function (config) {
        const el = id => document.getElementById('import-' + id);
        const form = el('upload-form'), file = el('csv-file'), muted = el('muted');
        const strings = config.strings;
        const active = new Set(['VALIDATING', 'QUEUED', 'RUNNING']);
        let batch = null, key = null, busy = false, uncertain = false, timer = null;
        const csrf = form.querySelector('[name=csrfmiddlewaretoken]').value;
        const activeTab = document.querySelector('.tabs .tab.active');
        if (activeTab) {
            const list = activeTab.closest('ul');
            if (list.scrollWidth > list.clientWidth) {
                list.scrollLeft += activeTab.getBoundingClientRect().right - list.getBoundingClientRect().right;
            }
        }

        function message(text) {
            el('error').textContent = text || '';
            el('error').hidden = !text;
        }

        function persist() {
            try {
                if (key) root.sessionStorage.setItem(config.storage_key, JSON.stringify({key, batch_id: batch && batch.id}));
                else root.sessionStorage.removeItem(config.storage_key);
            } catch (_) { message(strings.no_storage); }
        }

        function controls() {
            const retryUpload = batch && batch.validation_dispatch_pending;
            const locked = busy || uncertain || (batch && active.has(batch.status) && !retryUpload);
            file.disabled = !!locked;
            muted.disabled = !!locked || !!retryUpload;
            el('upload').disabled = !!locked;
            el('check').hidden = !key;
            el('check').disabled = busy;
            const ready = batch && batch.status === 'PREVIEW_READY' && batch.execution_available;
            const retrySubmit = batch && batch.status === 'QUEUED' && batch.execution_dispatch_pending;
            el('submit').disabled = busy || uncertain ||
                !(retrySubmit || (ready && el('confirmed').checked && muted.checked === batch.muted));
            el('previous').disabled = busy || !batch || batch.page <= 1;
            el('next').disabled = busy || !batch || batch.page >= batch.pages;
        }

        function format(text, values) {
            return text.replace(/\{(\w+)\}/g, (_, name) => String(values[name]));
        }

        function append(parent, tag, text, className) {
            const node = document.createElement(tag);
            node.textContent = text;
            if (className) node.className = className;
            parent.appendChild(node);
            return node;
        }

        function diff(value) {
            return (value.before ? strings.on : strings.off) + ' → ' + (value.after ? strings.on : strings.off);
        }

        function render(current) {
            batch = current;
            key = current.idempotency_key;
            uncertain = false;
            persist();
            root.history.replaceState(null, '', config.index_url + '?batch=' + encodeURIComponent(batch.id));
            muted.checked = batch.muted;
            const ready = batch.status === 'PREVIEW_READY';
            el('status').dataset.state = batch.status;
            el('status').textContent = batch.status === 'VALIDATING' ? strings.validating : ready ? strings.ready :
                batch.status === 'INVALID' ? strings.invalid : batch.status === 'COMPLETED' ? strings.completed :
                    batch.status === 'QUEUED' ? strings.queued : batch.status === 'RUNNING' ? strings.running : strings.closed;
            if (batch.validation_dispatch_pending) el('status').textContent = strings.retry_upload;
            if (batch.status === 'RUNNING' && !batch.progress) el('status').textContent = strings.verifying;
            if (batch.execution_dispatch_pending) el('status').textContent = strings.queue_wait;
            el('summary').textContent = batch.status === 'COMPLETED' ? format(strings.result, {
                total: batch.applied_rows, created: batch.created_users, updated: batch.updated_users,
            }) : format(strings.summary, {
                total: batch.total_rows, prepared: batch.prepared_passwords, applied: batch.applied_rows,
            });
            el('expiry').textContent = ready ? format(strings.expiry, {
                time: new Date(batch.preview_expires_at).toLocaleString(),
            }) : '';
            el('processing').hidden = !batch.progress;
            el('progress').hidden = !batch.progress;
            if (batch.progress) {
                el('processing').textContent = format(strings.progress, batch.progress);
                el('progress').max = batch.progress.total;
                el('progress').value = batch.progress.processed;
            }
            if (batch.report_expired) el('expiry').textContent = strings.report_expired;
            el('errors').replaceChildren();
            (batch.errors || []).forEach(issue => {
                append(el('errors'), 'li', (issue.line ? strings.line + ' ' + issue.line + ': ' : '') +
                    (issue.field ? issue.field + ': ' : '') + issue.message);
            });
            el('rows').replaceChildren();
            batch.rows.forEach(row => {
                const tr = document.createElement('tr');
                tr.dataset.row = row.row;
                append(tr, 'td', row.line);
                append(tr, 'td', row.username);
                append(tr, 'td', row.action === 'CREATE' ? strings.create : row.action === 'UPDATE' ?
                    strings.update : strings.unknown);
                const fields = append(tr, 'td', '');
                const preview = row.preview || {};
                ['first_name', 'last_name', 'email'].forEach(name => {
                    const change = (preview.changes || {})[name];
                    append(fields, 'p', strings[name] + ': ' + (change ?
                        (change.before || strings.empty) + ' → ' + change.after :
                        row.action === 'CREATE' ? strings.empty : strings.keep));
                });
                append(tr, 'td', row.input.password_provided ? strings.provided : strings.keep);
                append(tr, 'td', (preview.organizations_to_add || []).join(', ') || strings.none);
                append(tr, 'td', preview.mute ? diff(preview.mute) : strings.unknown);
                const policy = append(tr, 'td', preview.password_rotation ? diff(preview.password_rotation) : strings.unknown);
                if ((preview.warnings || []).includes('password_not_restored')) append(policy, 'p', strings.warning);
                const errors = append(tr, 'td', '', 'import-row-errors');
                row.errors.forEach(issue => append(errors, 'p', (issue.field ? issue.field + ': ' : '') + issue.message));
                append(tr, 'td', row.status === 'SUCCESS' ? strings.saved : row.status === 'NOT_APPLIED' ?
                    strings.not_applied : '');
                el('rows').appendChild(tr);
            });
            el('table-wrap').hidden = !batch.rows.length;
            el('pagination').hidden = batch.pages <= 1;
            el('page').textContent = format(strings.page, {page: batch.page, pages: batch.pages});
            el('confirmation').hidden = !ready && !batch.execution_dispatch_pending;
            el('review').hidden = !ready;
            el('submit').textContent = batch.execution_dispatch_pending ? strings.retry_submit : config.confirm_label;
            el('confirmed').checked = false;
            controls();
            clearTimeout(timer);
            if (active.has(batch.status)) timer = setTimeout(check, 2000);
        }

        async function check(page) {
            if (!key || busy) return;
            busy = true;
            controls();
            try {
                const url = batch ? batch.status_url + '?page=' + (page || batch.page) :
                    config.index_url + '?operation_key=' + encodeURIComponent(key);
                const response = await fetch(url, {credentials: 'same-origin', headers: {Accept: 'application/json'}, cache: 'no-store'});
                const data = await response.json();
                if (response.ok && data.batch) { message(''); render(data.batch); }
                else if (response.status === 404 && !batch) {
                    // Confirmed missing operation: retry the same key after reselecting the file.
                    uncertain = false;
                    message(strings.missing);
                } else {
                    uncertain = true;
                    message(data.error ? data.error.message : strings.unavailable);
                }
            } catch (_) {
                uncertain = true;
                message(strings.network);
            } finally { busy = false; controls(); }
        }

        form.addEventListener('submit', async event => {
            event.preventDefault();
            const retryUpload = batch && batch.validation_dispatch_pending;
            if (busy || uncertain || (batch && active.has(batch.status) && !retryUpload) || !file.files.length) return;
            if (file.files[0].size > config.max_bytes) { message(strings.too_large); return; }
            if ((batch && !retryUpload) || !key) {
                batch = null;
                key = root.crypto.randomUUID();
            }
            root.history.replaceState(null, '', config.index_url + '?operation_key=' + encodeURIComponent(key));
            persist(); // Before the first network request; retries keep this key.
            const body = new FormData();
            body.append('csv_file', file.files[0]);
            body.append('muted', muted.checked ? 'true' : 'false');
            busy = true;
            uncertain = true;
            controls();
            message('');
            try {
                const response = await fetch(config.upload_url, {method: 'POST', credentials: 'same-origin',
                    headers: {'X-CSRFToken': csrf, 'Idempotency-Key': key, Accept: 'application/json'}, body});
                const data = await response.json();
                if (data.batch) { render(data.batch); if (response.ok) file.value = ''; }
                if (!response.ok) {
                    message((data.error ? data.error.message : strings.unavailable) +
                        (data.issues || []).map(issue => '\n' + strings.line + ' ' + issue.line +
                            (issue.field ? ' (' + issue.field + ')' : '') + ': ' + issue.message).join(''));
                    if ([413, 422].includes(response.status) && !data.batch) {
                        // Definite rejection before reservation; the corrected upload is a new request.
                        key = null; uncertain = false; persist();
                    }
                }
            } catch (_) { message(strings.network); }
            finally { busy = false; controls(); }
        });
        el('check').addEventListener('click', () => check());
        el('confirmed').addEventListener('change', controls);
        muted.addEventListener('change', () => {
            if (batch && batch.status === 'PREVIEW_READY' && muted.checked !== batch.muted) message(strings.mute_changed);
            else message('');
            controls();
        });
        el('submit').addEventListener('click', async () => {
            if (busy || uncertain || !batch || el('submit').disabled) return;
            busy = true;
            uncertain = true;
            controls();
            message('');
            persist();
            try {
                const response = await fetch(config.submit_url, {method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf, 'Idempotency-Key': key},
                    body: JSON.stringify({batch_id: batch.id, version: batch.version, confirmed: true})});
                const data = await response.json();
                if (data.batch) render(data.batch);
                if (!response.ok) message(data.error ? data.error.message : strings.unavailable);
            } catch (_) { message(strings.network); }
            finally { busy = false; controls(); }
        });
        el('previous').addEventListener('click', () => check(batch.page - 1));
        el('next').addEventListener('click', () => check(batch.page + 1));
        root.addEventListener('pagehide', () => clearTimeout(timer));

        if (config.initial_batch) render(config.initial_batch);
        else {
            try {
                const saved = JSON.parse(root.sessionStorage.getItem(config.storage_key) || 'null');
                key = config.pending_key || (saved && saved.key);
            } catch (_) { key = config.pending_key || null; }
            if (key) { uncertain = true; check(); }
            controls();
        }
    };
})(window);
