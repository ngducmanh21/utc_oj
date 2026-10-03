/* global tus */
(function (root) {
    'use strict';

    const gettext = text => typeof root.gettext === 'function' ? root.gettext(text) : text;
    const HASH_BLOCK_SIZE = 4 * 1024 * 1024;

    // Hash every byte without keeping the ZIP in memory. The final SHA-256 covers
    // ordered, fixed-size block hashes and the exact size (not names/mtime).
    async function fingerprint(file, progress, signal) {
        if (!root.crypto || !root.crypto.subtle) {
            throw new Error(gettext('Secure file verification requires HTTPS or localhost.'));
        }
        const count = Math.ceil(file.size / HASH_BLOCK_SIZE);
        const hashes = new Uint8Array(count * 32);
        for (let offset = 0, index = 0; offset < file.size; offset += HASH_BLOCK_SIZE, index++) {
            if (signal && signal.aborted) throw new DOMException('Canceled', 'AbortError');
            const bytes = await file.slice(offset, offset + HASH_BLOCK_SIZE).arrayBuffer();
            hashes.set(new Uint8Array(await root.crypto.subtle.digest('SHA-256', bytes)), index * 32);
            if (progress) progress(Math.min(offset + HASH_BLOCK_SIZE, file.size), file.size);
        }
        if (signal && signal.aborted) throw new DOMException('Canceled', 'AbortError');
        const digest = new Uint8Array(await root.crypto.subtle.digest('SHA-256', hashes));
        return `sha256-blocks-v1:${file.size}:` + Array.from(digest, b => b.toString(16).padStart(2, '0')).join('');
    }

    function formatBytes(bytes) {
        const units = ['B', 'KiB', 'MiB', 'GiB'];
        let unit = 0;
        while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit++; }
        return `${bytes.toFixed(unit ? 1 : 0)} ${units[unit]}`;
    }

    function init(config, onFilesReady) {
        const panel = document.getElementById('test-data-upload');
        const form = document.getElementById('test-data-form');
        const fields = document.getElementById('test-data-fields');
        const fileInput = document.getElementById('id_problem-data-zipfile');
        if (!panel || !form || !fileInput) return;
        const savedZip = document.getElementById('test-data-zip-current');
        const pendingZip = document.getElementById('test-data-zip-pending');
        const el = id => document.getElementById(`test-upload-${id}`);
        const status = el('status'), progress = el('progress'), detail = el('detail');
        const begin = el('begin'), end = el('end'), retry = el('retry'), cancel = el('cancel');
        const revoke = el('revoke');
        let conflictingSession = null;
        const sessionField = document.getElementById('id_edit_session_id');
        const tokenField = document.getElementById('id_edit_token');
        const uploadField = document.getElementById('id_upload_id');
        const storageKey = config.storage_key;
        let session = null, record = null, transfer = null, selectedFile = null;
        let state = 'VIEW', confirmed = 0, busy = false, connected = true;
        let heartbeatTimer, pollTimer, hashing, action = 0, saving = false;
        let lastSample = null, speed = 0, lastReady = config.upload && config.upload.id;
        let awaitingValidation = false;
        let releaseTabLock = null;
        const initialFiles = (config.initial_files || []).slice();
        if (lastReady) uploadField.value = lastReady;
        const csrf = form.querySelector('[name=csrfmiddlewaretoken]').value;

        // The selected ZIP is sent by tus; normal Save only submits its upload ID.
        fileInput.removeAttribute('name');
        fileInput.accept = '.zip,application/zip';

        function persist() {
            try {
                if (session) root.sessionStorage.setItem(storageKey, JSON.stringify({session, upload: record}));
                else root.sessionStorage.removeItem(storageKey);
            } catch (_) { /* Upload still works when browser storage is unavailable. */ }
        }

        function writeSession() {
            sessionField.value = session ? session.id : '';
            tokenField.value = session ? session.token : '';
            persist();
        }

        function controls() {
            const locked = !session || !connected;
            fields.disabled = locked;
            fields.inert = locked;
            const ready = record ? record.status === 'READY' : !selectedFile;
            form.querySelectorAll('[type=submit]').forEach(button => {
                button.disabled = locked || busy || !ready || saving;
            });
            begin.hidden = !!session;
            begin.disabled = busy;
            if (revoke) {
                revoke.hidden = !conflictingSession || !!session;
                revoke.disabled = busy;
                revoke.textContent = conflictingSession && conflictingSession.owner === config.current_user ?
                    gettext('End previous edit session') : gettext('Revoke edit session');
            }
            end.hidden = !session;
            end.disabled = busy || saving;
            cancel.hidden = !record && !hashing && !selectedFile;
            cancel.disabled = saving || (busy && !hashing);
            retry.hidden = !session || !['PAUSED', 'RESUME', 'CONNECTION_ERROR', 'UPLOAD_ERROR'].includes(state);
            fileInput.disabled = locked || busy || ['CREATING', 'UPLOADING', 'VALIDATING', 'UPLOADED'].includes(state);
            const clear = document.getElementById('problem-data-zipfile-clear_id');
            if (clear) { clear.disabled = locked || !!record; if (record) clear.checked = false; }
            const builder = document.getElementById('btn-build-test-data');
            if (builder) builder.hidden = !!record || busy;
            const verifyingFile = state === 'VERIFYING' && (fileInput.files[0] || selectedFile);
            const filename = verifyingFile ? verifyingFile.name :
                record && record.filename || selectedFile && selectedFile.name;
            const applied = record && record.status === 'APPLIED';
            if (savedZip) savedZip.hidden = !!filename;
            if (pendingZip) {
                pendingZip.hidden = !filename;
                pendingZip.textContent = filename ?
                    `${applied ? gettext('Saved ZIP') : gettext('New ZIP')}: ${filename}` +
                    (applied ? '' : ` (${gettext('Not saved yet')})`) : '';
            }
        }

        function show(next, message, indeterminate = false) {
            state = next;
            panel.dataset.state = next.toLowerCase();
            if (status.textContent !== message) status.textContent = message;
            progress.hidden = !indeterminate && !['UPLOADING', 'PAUSED', 'READY'].includes(next);
            if (indeterminate) progress.removeAttribute('value');
            controls();
        }

        function updateProgress(sent, total) {
            sent = Math.max(0, Math.min(sent, total));
            const now = performance.now();
            if (lastSample && now - lastSample.time >= 500) {
                const current = Math.max(0, sent - lastSample.bytes) * 1000 / (now - lastSample.time);
                speed = speed ? speed * 0.65 + current * 0.35 : current;
                lastSample = {time: now, bytes: sent};
            } else if (!lastSample || sent < lastSample.bytes) {
                lastSample = {time: now, bytes: sent};
                speed = 0;
            }
            progress.max = total || 1;
            progress.value = sent;
            const percent = total ? (100 * sent / total).toFixed(1) : '0.0';
            let text = `${percent}% · ${formatBytes(sent)} / ${formatBytes(total)}`;
            if (speed > 0 && sent < total) {
                const seconds = Math.ceil((total - sent) / speed);
                const remaining = `${Math.floor(seconds / 60)} ${gettext('min')} ${seconds % 60} ${gettext('sec')}`;
                text += ` · ${formatBytes(speed)}/s · ${remaining} ${gettext('remaining (estimated)')}`;
            }
            detail.textContent = text;
            el('confirmed').textContent = `${gettext('Confirmed by server')}: ${formatBytes(confirmed)}`;
        }

        async function request(path, body, method = 'POST') {
            const headers = {'Accept': 'application/json', 'X-CSRFToken': csrf};
            if (session) {
                headers['X-Edit-Session'] = session.id;
                headers['X-Edit-Token'] = session.token;
            }
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            const abort = new AbortController();
            const timeout = setTimeout(() => abort.abort(), 20000);
            let response;
            try {
                response = await fetch(config.base_url + path, {
                    method, credentials: 'same-origin', cache: 'no-store', headers,
                    body: body === undefined ? undefined : JSON.stringify(body), signal: abort.signal,
                });
            } catch (_) {
                throw new Error(gettext('Cannot reach the server. Retry to continue; your form is preserved.'));
            } finally { clearTimeout(timeout); }
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                const error = new Error(data.error && data.error.message || gettext('The request failed. Please try again.'));
                error.status = response.status;
                error.code = data.error && data.error.code;
                error.data = data;
                throw error;
            }
            return data;
        }

        function credentials(extra) {
            return Object.assign({edit_session_id: session.id, edit_token: session.token}, extra || {});
        }

        async function stopTransfer() {
            if (hashing) hashing.abort();
            hashing = null;
            if (transfer) await transfer.abort(); // Keep the offset for retry/resume.
            transfer = null;
            clearTimeout(pollTimer);
        }

        async function fail(error) {
            busy = false;
            if (error.code === 'locked' && error.data && error.data.can_revoke) {
                conflictingSession = error.data.session;
            }
            if ([401, 403, 404, 410].includes(error.status) ||
                ['session_expired', 'session_revoked', 'invalid_session', 'invalid_token', 'session_inactive'].includes(error.code)) {
                action++;
                await stopTransfer();
                clearInterval(heartbeatTimer);
                session = null;
                record = null;
                selectedFile = null;
                awaitingValidation = false;
                uploadField.value = '';
                fileInput.value = '';
                if (releaseTabLock) { releaseTabLock(); releaseTabLock = null; }
                writeSession();
                show('LOCKED', error.message);
            } else if (['not_configured', 'disabled', 'insufficient_storage', 'upload_capacity', 'invalid_file'].includes(error.code)) {
                // A rejected upload does not invalidate a confirmed edit lease.
                // Keep testcase controls available; Cancel restores saving the
                // current data without accidentally submitting the rejected ZIP.
                show('UPLOAD_ERROR', error.message + ' ' + gettext('You can still edit testcases. Cancel the upload to use the current test data, or retry.'));
            } else if (!error.status || error.status >= 500 || (error.code === 'upload_exists' && !record)) {
                connected = false;
                show('CONNECTION_ERROR', error.message || gettext('Cannot reach the server. Retry to continue; your form is preserved.'));
            } else {
                show('ERROR', error.message);
            }
        }

        async function heartbeat() {
            if (!session || saving) return;
            const sessionId = session.id;
            try {
                const data = await request(`session/${sessionId}/heartbeat/`, credentials());
                // End/revoke or a new session may finish while this request is
                // in flight. An old heartbeat must never unlock the old editor.
                if (!session || session.id !== sessionId || saving) return;
                session = data.session;
                connected = true;
                writeSession();
                if (!record) {
                    const pending = (data.uploads || []).find(item =>
                        !['CANCELED', 'EXPIRED', 'FAILED', 'APPLIED'].includes(item.status));
                    if (pending) {
                        // Recover an upload admission whose successful response
                        // was lost. Do not create a second upload on retry.
                        record = pending;
                        await inspectRecord(false);
                        return;
                    }
                }
                if (state === 'CONNECTION_ERROR') {
                    show(record ? 'RESUME' : 'EDITING', record ? gettext('Connection restored. Continue the upload.') : gettext('Editing test data.'));
                }
                controls();
            } catch (error) {
                if (!session || session.id !== sessionId || saving) return;
                if (error.status && error.status < 500) return fail(error);
                connected = false;
                show('CONNECTION_ERROR', gettext('Cannot confirm the edit session. Reconnecting; your form is preserved.'));
            }
        }

        async function activate(data) {
            session = data.session;
            conflictingSession = null;
            // sessionStorage is copied by Duplicate Tab. A browser lock prevents
            // the copied token from silently granting a second editor.
            if (!releaseTabLock && navigator.locks) {
                const acquired = await new Promise(resolve => {
                    navigator.locks.request(`test-data-edit:${session.id}`, {ifAvailable: true}, lock => {
                        if (!lock) { resolve(false); return; }
                        resolve(true);
                        return new Promise(release => { releaseTabLock = release; });
                    });
                });
                if (!acquired) {
                    session = null;
                    writeSession();
                    throw new Error(gettext('This edit session is already open in another tab. Continue in that tab.'));
                }
            }
            Object.assign(config, data.config || {});
            connected = true;
            writeSession();
            clearInterval(heartbeatTimer);
            heartbeatTimer = setInterval(heartbeat, (config.heartbeat_seconds || 30) * 1000);
            show('EDITING', gettext('Editing test data. Changes take effect only after Save.'));
        }

        async function inspectRecord(autofill = false) {
            if (!record || !session) return;
            clearTimeout(pollTimer);
            const currentAction = action;
            const currentId = record.id;
            try {
                const data = await request(`files/${record.id}/`, undefined, 'GET');
                if (currentAction !== action || !record || currentId !== record.id) return;
                record = data.upload;
                persist();
                busy = false;
                if (record.status === 'READY') {
                    if (lastReady !== record.id && !record.entries) {
                        const listing = await request(`files/${record.id}/?entries=1`, undefined, 'GET');
                        if (currentAction !== action || !record || record.id !== currentId) return;
                        record = listing.upload;
                    }
                    awaitingValidation = false;
                    uploadField.value = record.id;
                    confirmed = record.size;
                    updateProgress(record.size, record.size);
                    show('READY', gettext('ZIP checked. Ready to save; the test data has not been applied yet.'));
                    if (lastReady !== record.id) {
                        onFilesReady(record.entries || [], autofill);
                        lastReady = record.id;
                    }
                } else if (record.status === 'CREATING') {
                    show('CREATING', gettext('Waiting for the upload to start…'), true);
                    pollTimer = setTimeout(() => inspectRecord(autofill), 2000);
                } else if (['UPLOADED', 'VALIDATING'].includes(record.status) ||
                    (awaitingValidation && ['CREATED', 'UPLOADING'].includes(record.status))) {
                    show('VALIDATING', gettext('Upload complete. Checking ZIP on the server…'), true);
                    pollTimer = setTimeout(() => inspectRecord(autofill), 2000);
                } else if (record.status === 'APPLYING') {
                    show('APPLYING', gettext('Applying test data…'), true);
                    pollTimer = setTimeout(() => inspectRecord(false), 2000);
                } else if (record.status === 'APPLIED') {
                    clearInterval(heartbeatTimer);
                    session = null;
                    if (releaseTabLock) { releaseTabLock(); releaseTabLock = null; }
                    uploadField.value = '';
                    writeSession();
                    show('APPLIED', gettext('Test data saved. Reload the page to see the current data.'));
                } else if (['FAILED', 'CANCELED', 'EXPIRED'].includes(record.status)) {
                    awaitingValidation = false;
                    show('ERROR', record.error || gettext('This upload cannot be used. Cancel it and select the ZIP again.'));
                } else {
                    show('RESUME', gettext('Select the same ZIP to continue the unfinished upload.'));
                }
            } catch (error) {
                if (currentAction !== action || !record || currentId !== record.id || !session) return;
                if (!error.status || error.status >= 500) {
                    show('CONNECTION_ERROR', gettext('Waiting for the server. Your upload and form are preserved.'));
                    pollTimer = setTimeout(() => inspectRecord(autofill), 5000);
                } else await fail(error);
            }
        }

        function startTransfer(file) {
            lastSample = null;
            speed = 0;
            busy = false;
            const currentId = record.id;
            const currentAction = action;
            transfer = new tus.Upload(file, {
                endpoint: config.tus_endpoint,
                uploadUrl: record.tus_url || undefined,
                chunkSize: config.chunk_size,
                retryDelays: [0, 1000, 3000, 5000, 10000, 20000],
                storeFingerprintForResuming: false,
                removeFingerprintOnSuccess: true,
                headers: {'X-Test-Upload-Token': record.token},
                metadata: {upload_id: record.id, filename: file.name, filetype: 'application/zip'},
                onBeforeRequest(req) {
                    if (req.getUnderlyingObject()) req.getUnderlyingObject().withCredentials = true;
                },
                onAfterResponse(req, res) {
                    if (currentAction !== action || !record || record.id !== currentId) return;
                    const offset = res.getHeader('Upload-Offset');
                    if (offset !== null && Number.isFinite(Number(offset))) confirmed = Number(offset);
                    if (req.getMethod() === 'HEAD') updateProgress(confirmed, file.size);
                    if (transfer && transfer.url) { record.tus_url = transfer.url; persist(); }
                },
                onProgress(sent, total) {
                    if (currentAction !== action) return;
                    show('UPLOADING', gettext('Uploading ZIP…'));
                    updateProgress(sent, total);
                },
                onChunkComplete(_chunk, accepted, total) {
                    if (currentAction !== action) return;
                    confirmed = accepted;
                    updateProgress(accepted, total);
                    if (transfer && transfer.url) { record.tus_url = transfer.url; persist(); }
                },
                onShouldRetry(error, attempt) {
                    if (currentAction !== action) return false;
                    const statusCode = error.originalResponse && error.originalResponse.getStatus();
                    if ([401, 403, 404, 410, 413].includes(statusCode)) return false;
                    show('PAUSED', navigator.onLine ? gettext('Connection interrupted. Retrying upload…') : gettext('Offline. Waiting to continue upload…'));
                    updateProgress(confirmed, file.size);
                    return attempt < 6;
                },
                async onError(error) {
                    if (currentAction !== action) return;
                    const statusCode = error.originalResponse && error.originalResponse.getStatus();
                    if ([401, 403, 404, 410].includes(statusCode)) {
                        // tus/proxy errors do not prove the editing lease ended.
                        // The application API is authoritative; retain its valid
                        // session and token so Cancel and End can still reach it.
                        try {
                            const data = await request(`files/${currentId}/`, undefined, 'GET');
                            if (currentAction !== action) return;
                            record = data.upload;
                            session = data.session || session;
                            connected = true;
                            writeSession();
                            if (!['CREATED', 'UPLOADING'].includes(record.status)) {
                                await inspectRecord(true);
                                return;
                            }
                        } catch (lookupError) {
                            if (currentAction === action) await fail(lookupError);
                            return;
                        }
                        show('UPLOAD_ERROR', gettext('The upload service rejected the request. Your edit session is still active. Retry or cancel the ZIP upload.'));
                        updateProgress(confirmed, file.size);
                        return;
                    }
                    show('PAUSED', statusCode === 413 ? gettext('An upload request exceeded the server limit. Contact an administrator.') : gettext('Upload interrupted. Continue to retry from the confirmed position.'));
                    updateProgress(confirmed, file.size);
                },
                onSuccess() {
                    if (currentAction !== action) return;
                    confirmed = file.size;
                    record.tus_url = transfer.url;
                    transfer = null;
                    awaitingValidation = true;
                    persist();
                    updateProgress(file.size, file.size);
                    show('VALIDATING', gettext('Upload complete. Checking ZIP on the server…'), true);
                    inspectRecord(true);
                },
            });
            show('UPLOADING', gettext('Uploading ZIP…'));
            updateProgress(confirmed, file.size);
            transfer.start();
        }

        async function chooseFile(file) {
            if (!file || !session || busy) return;
            if (!file.size || file.size > config.max_size) {
                show('ERROR', `${gettext('ZIP size must be between 1 byte and')} ${formatBytes(config.max_size)}.`);
                fileInput.value = '';
                return;
            }
            const currentAction = ++action;
            busy = true;
            show('VERIFYING', gettext('Verifying the selected file before upload…'), true);
            hashing = new AbortController();
            controls();
            try {
                const hash = await fingerprint(file, (done, total) => {
                    detail.textContent = `${gettext('File verification')}: ${formatBytes(done)} / ${formatBytes(total)}`;
                }, hashing.signal);
                hashing = null;
                if (currentAction !== action) return;
                controls();
                selectedFile = file;
                if (record && ['CREATED', 'CREATING', 'UPLOADING'].includes(record.status)) {
                    if (record.fingerprint !== hash || record.size !== file.size) {
                        busy = false;
                        show('RESUME', gettext('This is a different file. Select the original ZIP, or cancel the unfinished upload first.'));
                        fileInput.value = '';
                        return;
                    }
                    // Recover the URL even if the original POST response was lost.
                    const data = await request(`files/${record.id}/`, undefined, 'GET');
                    record = data.upload;
                } else {
                    if (record) {
                        await request(`files/${record.id}/cancel/`, credentials());
                        record = null;
                        persist();
                    }
                    uploadField.value = '';
                    const data = await request('files/', credentials({filename: file.name, size: file.size, fingerprint: hash}));
                    record = data.upload;
                    config.tus_endpoint = data.tus_endpoint || config.tus_endpoint;
                    config.chunk_size = data.chunk_size || config.chunk_size;
                    confirmed = 0;
                }
                if (currentAction !== action) return;
                persist();
                if (['CREATING', 'READY', 'UPLOADED', 'VALIDATING', 'APPLIED'].includes(record.status)) {
                    busy = false;
                    await inspectRecord(true);
                } else startTransfer(file);
            } catch (error) {
                hashing = null;
                if (currentAction === action && error.name !== 'AbortError') await fail(error);
            } finally {
                if (currentAction === action) { busy = false; controls(); }
            }
        }

        begin.addEventListener('click', async () => {
            busy = true;
            show('STARTING', gettext('Starting edit session…'), true);
            try { await activate(await request('session/', {})); }
            catch (error) { await fail(error); }
            finally { busy = false; controls(); }
        });

        if (revoke) revoke.addEventListener('click', async () => {
            if (!conflictingSession) return;
            const message = conflictingSession.owner === config.current_user ?
                gettext('End your previous edit session? Its unapplied uploads will be canceled.') :
                gettext('Revoke this edit session? Its unapplied uploads will be canceled.');
            if (!root.confirm(message)) return;
            busy = true;
            controls();
            try {
                await request(`session/${conflictingSession.id}/revoke/`, {});
                conflictingSession = null;
                await activate(await request('session/', {}));
            } catch (error) { await fail(error); }
            finally { busy = false; controls(); }
        });

        end.addEventListener('click', async () => {
            if (!session || !root.confirm(gettext('End this edit session? Unapplied uploads will be canceled.'))) return;
            action++;
            await stopTransfer();
            busy = true;
            controls();
            try {
                await request(`session/${session.id}/end/`, credentials());
                clearInterval(heartbeatTimer);
                session = null;
                if (releaseTabLock) { releaseTabLock(); releaseTabLock = null; }
                record = null;
                awaitingValidation = false;
                selectedFile = null;
                uploadField.value = '';
                fileInput.value = '';
                writeSession();
                show('VIEW', gettext('Edit session ended. Changes have not been saved.'));
            } catch (error) { await fail(error); }
            finally { busy = false; controls(); }
        });

        cancel.addEventListener('click', async () => {
            action++;
            await stopTransfer();
            busy = true;
            controls();
            try {
                if (!record && selectedFile && !connected) {
                    // A lost admission response may have created a server-side
                    // upload. Find it before canceling instead of dropping only
                    // the browser's file and leaving that upload alive.
                    const data = await request(`session/${session.id}/heartbeat/`, credentials());
                    record = (data.uploads || []).find(item =>
                        !['CANCELED', 'EXPIRED', 'FAILED', 'APPLIED'].includes(item.status)) || null;
                    connected = true;
                }
                if (record) await request(`files/${record.id}/cancel/`, credentials());
                record = null;
                awaitingValidation = false;
                selectedFile = null;
                confirmed = 0;
                uploadField.value = '';
                fileInput.value = '';
                persist();
                onFilesReady(initialFiles, false);
                lastReady = null;
                detail.textContent = '';
                el('confirmed').textContent = '';
                show('EDITING', gettext('Upload canceled. Your form is preserved; select another ZIP if needed.'));
            } catch (error) { await fail(error); }
            finally { busy = false; controls(); }
        });

        retry.addEventListener('click', async () => {
            await heartbeat();
            if (!session || !connected) return;
            if (!record) return selectedFile ? chooseFile(selectedFile) : controls();
            if (['UPLOADED', 'VALIDATING', 'READY'].includes(record.status)) return inspectRecord(false);
            if (selectedFile) {
                await stopTransfer();
                chooseFile(selectedFile);
            } else fileInput.click();
        });

        fileInput.addEventListener('change', () => chooseFile(fileInput.files[0]));
        // jQuery-triggered changes from the optional local ZIP builder.
        if (root.jQuery) root.jQuery(fileInput).on('change.tusUpload', event => {
            if (!event.originalEvent) chooseFile(fileInput.files[0]);
        });

        form.addEventListener('submit', event => {
            if (!session || !connected || busy || saving || (record ? record.status !== 'READY' : selectedFile)) {
                event.preventDefault();
                event.stopImmediatePropagation();
                status.textContent = gettext('Start an edit session and wait until the ZIP is ready before saving.');
                return;
            }
            saving = true;
            show('APPLYING', gettext('Saving test data… Please wait for confirmation.'), true);
        }, true);

        root.addEventListener('online', () => { if (session) heartbeat(); });
        root.addEventListener('pageshow', event => {
            if (event.persisted) { saving = false; heartbeat(); }
        });

        async function restore() {
            const saved = !config.session && new URLSearchParams(root.location.search).get('test-data-saved') === '1';
            show('VIEW', saved ? gettext('Test data saved. Start a new edit session to make further changes.') : gettext('Start an edit session to change test data.'));
            el('limit').textContent = `${gettext('Maximum ZIP size')}: ${formatBytes(config.max_size)}.`;
            let previous = config.session ? {session: config.session, upload: config.upload} : null;
            if (!previous && !saved) {
                try { previous = JSON.parse(root.sessionStorage.getItem(storageKey)); }
                catch (_) { /* No saved session. */ }
            }
            if (saved) {
                try { root.sessionStorage.removeItem(storageKey); } catch (_) { /* Storage may be disabled. */ }
                return;
            }
            if (!previous || !previous.session) return;
            session = previous.session;
            busy = true;
            show('RESTORING', gettext('Checking your previous edit session…'), true);
            try {
                const data = await request(`session/${session.id}/heartbeat/`, credentials());
                await activate(data);
                record = (data.uploads || []).find(item => item.id === (previous.upload && previous.upload.id)) ||
                    (data.uploads || []).find(item => !['CANCELED', 'EXPIRED', 'FAILED'].includes(item.status)) || null;
                if (record) await inspectRecord(false);
            } catch (error) { await fail(error); }
            finally { busy = false; controls(); }
        }
        restore();
    }

    root.TestDataUpload = {init, fingerprint, formatBytes};
})(globalThis);
