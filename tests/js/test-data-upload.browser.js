// Optional browser integration check. Start Chrome with remote debugging, then:
// CHROME_DEBUG_URL=http://127.0.0.1:9237 node tests/js/test-data-upload.browser.js
// Uses the real tus browser client with a controlled HTTP protocol fixture.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {once} from 'node:events';
import WebSocket from 'ws';

const debugUrl = process.env.CHROME_DEBUG_URL || 'http://127.0.0.1:9237';
const root = new URL('../../', import.meta.url);
let offset = 0, complete = false, requestSizes = [], patches = 0, heartbeatCalls = 0, heads = 0;
let storedFingerprint = '', totalSize = 0;
let rejectHeartbeats = false, loseAdmission = false, conflict = false;
let conflictOwner = 'another author';
let rejectAdmission = null;
let rejectTus = null, uploadCreated = false, canceledUploads = 0, endedSessions = 0;
let heldHeartbeat = null, holdHeartbeat = false;
let heldStatus = null, holdStatus = false;
let sessionNumber = 1;
const errors = [];
const upload = () => ({
    id: 'upload-1', token: 'upload-secret', status: complete ? 'READY' : uploadCreated ? 'UPLOADING' : 'CREATED',
    size: totalSize, fingerprint: storedFingerprint, filename: 'tests.zip',
    tus_url: offset || patches ? '/uploads/file-1' : null, entries: complete ? ['1.in', '1.out'] : [],
});
let session = {id: 'session-1', token: 'edit-secret', owner: 'tester'};
const html = `<!doctype html><html><body>
<form id="test-data-form"><input name="csrfmiddlewaretoken" value="csrf">
<section id="test-data-upload"><h3>Test upload</h3><p id="test-upload-status"></p>
<progress id="test-upload-progress"></progress><p id="test-upload-detail"></p>
<p id="test-upload-confirmed"></p><p id="test-upload-limit"></p>
<button id="test-upload-begin" type="button">Edit</button><button id="test-upload-end" type="button">End</button>
<button id="test-upload-retry" type="button">Retry</button><button id="test-upload-cancel" type="button">Cancel</button>
<button id="test-upload-revoke" type="button">Revoke</button></section>
<input id="id_edit_session_id" name="edit_session_id"><input id="id_edit_token" name="edit_token"><input id="id_upload_id" name="upload_id">
<fieldset id="test-data-fields" disabled inert><input id="id_problem-data-zipfile" name="zipfile" type="file">
<input id="fixture-form-value" name="checker_args" value="original">
<input id="zip-save-btn" type="submit" value="Save"></fieldset></form>
<script src="/tus.js"></script><script src="/upload.js"></script><script>
window.readyFiles = null;
TestDataUpload.init({base_url:'/api/',storage_key:'upload-fixture',max_size:1073741824,
chunk_size:16777216,tus_endpoint:'/uploads/',heartbeat_seconds:30,current_user:'tester'},
(files, autofill) => { window.readyFiles={files,autofill}; });
</script></body></html>`;
const server = createServer(async (req, res) => {
    try {
        const path = req.url;
        if (path === '/') { res.setHeader('Content-Type', 'text/html'); res.end(html); return; }
        if (path === '/tus.js' || path === '/upload.js') {
            res.setHeader('Content-Type', 'application/javascript');
            res.end(await readFile(new URL(path === '/tus.js' ? 'resources/vendor/tus/tus.min.js' : 'resources/test-data-upload.js', root)));
            return;
        }
        if (path.startsWith('/api/')) {
            const chunks = [];
            for await (const chunk of req) chunks.push(chunk);
            const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
            res.setHeader('Content-Type', 'application/json');
            if (path === '/api/session/') {
                if (conflict) {
                    res.writeHead(409);
                    res.end(JSON.stringify({error: {code:'locked',message:'Test data is being edited by another author.'},
                        session:{id:'other-session',owner:conflictOwner},can_revoke:true})); return;
                }
                session = {id:`session-${sessionNumber++}`,token:'edit-secret',owner:'tester'};
                res.end(JSON.stringify({session, uploads: []})); return;
            }
            if (path.includes('/revoke/')) { conflict = false; res.end('{}'); return; }
            if (path.includes('/end/')) {
                endedSessions++;
                res.end(JSON.stringify({session:{...session,status:'ENDED'}})); return;
            }
            if (path.includes('/heartbeat/')) {
                heartbeatCalls++;
                assert.equal(body.edit_token, session.token);
                if (holdHeartbeat) {
                    holdHeartbeat = false;
                    const response = JSON.stringify({session, uploads: totalSize ? [upload()] : []});
                    heldHeartbeat = () => res.end(response);
                    return;
                }
                if (rejectHeartbeats) {
                    res.writeHead(403);
                    res.end(JSON.stringify({error:{code:'session_revoked',message:'The editing session was revoked.'}}));
                    return;
                }
                res.end(JSON.stringify({session, uploads: totalSize ? [upload()] : []})); return;
            }
            if (path === '/api/files/') {
                assert.equal(body.edit_token, session.token);
                if (rejectAdmission) {
                    res.writeHead(rejectAdmission.status);
                    res.end(JSON.stringify({error: {code: rejectAdmission.code, message: rejectAdmission.message}}));
                    return;
                }
                storedFingerprint = body.fingerprint;
                totalSize = body.size;
                if (loseAdmission) {
                    loseAdmission = false;
                    // The upstream persisted admission, but the proxy could not
                    // return its successful response to the browser.
                    res.writeHead(503); res.end('{}'); return;
                }
                res.end(JSON.stringify({upload: upload()})); return;
            }
            if (path === '/api/files/upload-1/cancel/') {
                assert.equal(body.edit_token, session.token);
                canceledUploads++;
                const previous = upload();
                offset = 0; complete = false; totalSize = 0; patches = 0; storedFingerprint = ''; uploadCreated = false;
                res.end(JSON.stringify({upload:{...previous,status:'CANCELED'}})); return;
            }
            if (path.startsWith('/api/files/upload-1/')) {
                assert.equal(req.headers['x-edit-token'], session.token);
                assert.equal(req.headers['x-edit-session'], session.id);
                if (holdStatus) {
                    holdStatus = false;
                    heldStatus = () => {
                        res.writeHead(403);
                        res.end(JSON.stringify({error:{code:'session_revoked',message:'A stale upload request failed.'}}));
                    };
                    return;
                }
                res.end(JSON.stringify({upload: upload(), session})); return;
            }
        }
        if (path.startsWith('/uploads/')) {
            assert.equal(req.headers['x-test-upload-token'], 'upload-secret');
            res.setHeader('Tus-Resumable', '1.0.0');
            if (rejectTus) { res.writeHead(rejectTus); res.end('Upload request rejected'); return; }
            if (req.method === 'POST') {
                assert.equal(Number(req.headers['upload-length']), totalSize);
                uploadCreated = true;
                res.writeHead(201, {'Location': `http://${req.headers.host}/uploads/file-1`}); res.end(); return;
            }
            if (req.method === 'HEAD') {
                heads++;
                res.writeHead(200, {'Upload-Offset': String(offset), 'Upload-Length': String(totalSize)}); res.end(); return;
            }
            if (req.method === 'PATCH') {
                if (Number(req.headers['upload-offset']) !== offset) {
                    // A transport may resend a request after losing the reply.
                    // tus rejects stale offsets; HEAD recovers accepted bytes.
                    res.writeHead(409, {'Upload-Offset':String(offset)}); res.end(); return;
                }
                patches++;
                const currentPatch = patches;
                let count = 0;
                for await (const chunk of req) count += chunk.length;
                requestSizes.push(count);
                offset += count;
                complete = offset === totalSize;
                // Lose the first PATCH response after persisting the chunk. The
                // reloaded browser must ask HEAD instead of sending it again.
                await new Promise(resolve => setTimeout(resolve, currentPatch === 1 ? 3000 : 500));
                if (currentPatch === 1) { res.destroy(); return; }
                res.writeHead(204, {'Upload-Offset': String(offset)}); res.end(); return;
            }
        }
        res.writeHead(404); res.end();
    } catch (error) { errors.push(error); res.writeHead(500); res.end(); }
});
server.listen(0, '127.0.0.1');
await once(server, 'listening');
const address = `http://127.0.0.1:${server.address().port}/`;
const targets = await (await fetch(`${debugUrl}/json/list`)).json();
const target = targets.find(item => item.type === 'page');
assert.ok(target, 'Open a Chrome tab with remote debugging enabled.');
const ws = new WebSocket(target.webSocketDebuggerUrl);
await once(ws, 'open');
let seq = 0;
const pending = new Map();
ws.on('message', raw => {
    const data = JSON.parse(raw);
    if (data.id && pending.has(data.id)) {
        const [resolve, reject] = pending.get(data.id); pending.delete(data.id);
        if (data.error) reject(new Error(JSON.stringify(data.error))); else resolve(data.result);
    }
    if (data.method === 'Runtime.exceptionThrown') errors.push(new Error(data.params.exceptionDetails.text));
});
function send(method, params = {}) {
    return new Promise((resolve, reject) => {
        const id = ++seq; pending.set(id, [resolve, reject]); ws.send(JSON.stringify({id, method, params}));
    });
}
async function evaluate(expression) {
    const result = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
}
async function until(expression) {
    for (let i = 0; i < 150; i++) {
        if (await evaluate(expression)) return;
        await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(`Timed out: ${expression}`);
}
try {
    await send('Runtime.enable');
    await send('Page.navigate', {url: address});
    await until('window.TestDataUpload && document.getElementById("test-upload-begin")');
    assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), true);
    await evaluate('document.getElementById("test-upload-begin").click()');
    await until('!document.getElementById("test-data-fields").disabled');
    // A definite admission rejection must preserve the active editor. A chosen
    // but rejected ZIP must be canceled before saving the published test data.
    for (const rejection of [
        {status: 503, code: 'not_configured', message: 'Upload staging is not configured.'},
        {status: 507, code: 'insufficient_storage', message: 'There is not enough free disk space for this upload.'},
    ]) {
        rejectAdmission = rejection;
        await evaluate(`(() => {
            const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(512)], 'tests.zip'));
            const input = document.getElementById('id_problem-data-zipfile');
            input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
        })()`);
        await until('document.getElementById("test-data-upload").dataset.state === "upload_error"');
        assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), false);
        assert.equal(await evaluate('document.getElementById("test-data-fields").inert'), false);
        assert.equal(await evaluate('document.getElementById("id_problem-data-zipfile").disabled'), false);
        assert.equal(await evaluate('document.getElementById("test-upload-cancel").hidden'), false);
        assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), true);
        await evaluate('document.getElementById("fixture-form-value").value = "preserved after rejection"');
        await evaluate('document.getElementById("test-upload-cancel").click()');
        await until('document.getElementById("test-data-upload").dataset.state === "editing"');
        assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), false);
        assert.equal(await evaluate('document.getElementById("fixture-form-value").value'), 'preserved after rejection');
        assert.equal(await evaluate('document.getElementById("id_upload_id").value'), '');
    }
    rejectAdmission = null;
    // A tus/proxy error does not prove the Django editing lease expired. Keep
    // its token until the application's API actually rejects that lease.
    for (const code of [401, 403, 404, 410]) {
        rejectTus = code;
        const originalSession = await evaluate('document.getElementById("id_edit_session_id").value');
        const cancellations = canceledUploads;
        const ended = endedSessions;
        await evaluate(`(() => {
            const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(512)], 'tests.zip'));
            const input = document.getElementById('id_problem-data-zipfile');
            input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
        })()`);
        await until('document.getElementById("test-data-upload").dataset.state === "upload_error"');
        assert.equal(await evaluate('document.getElementById("id_edit_session_id").value'), originalSession);
        assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), false);
        assert.equal(await evaluate('document.getElementById("test-upload-begin").hidden'), true);
        assert.equal(await evaluate('document.getElementById("test-upload-revoke").hidden'), true);
        assert.equal(await evaluate('document.getElementById("test-upload-end").hidden'), false);
        await evaluate('document.getElementById("test-upload-cancel").click()');
        await until('document.getElementById("test-data-upload").dataset.state === "editing"');
        assert.equal(canceledUploads, cancellations + 1);
        assert.equal(endedSessions, ended);
        assert.equal(await evaluate('document.getElementById("id_edit_session_id").value'), originalSession);
        assert.equal(await evaluate('document.getElementById("test-upload-begin").hidden'), true);
        assert.equal(await evaluate('document.getElementById("test-upload-revoke").hidden'), true);
        assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), false);
    }
    rejectTus = null;
    await evaluate(`(() => {
        const dt = new DataTransfer();
        dt.items.add(new File([new Uint8Array(17 * 1024 * 1024 + 12)], 'tests.zip'));
        const input = document.getElementById('id_problem-data-zipfile');
        input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('document.getElementById("test-upload-progress").value > 0');
    assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), true);
    for (let i = 0; i < 150 && offset < 16 * 1024 * 1024; i++) {
        await new Promise(resolve => setTimeout(resolve, 20));
    }
    assert.equal(offset, 16 * 1024 * 1024);
    assert.equal(await evaluate('document.getElementById("test-upload-confirmed").textContent'), 'Confirmed by server: 0 B');
    await send('Page.reload');
    await until('document.getElementById("test-data-upload").dataset.state === "resume"');
    // Same length/name but changed bytes must never append to the previous file.
    await evaluate(`(() => {
        const bytes = new Uint8Array(17 * 1024 * 1024 + 12); bytes[12345] = 1;
        const dt = new DataTransfer(); dt.items.add(new File([bytes], 'tests.zip'));
        const input = document.getElementById('id_problem-data-zipfile');
        input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('document.getElementById("test-upload-status").textContent.includes("different file")');
    assert.equal(patches, 1);
    await evaluate(`(() => {
        const dt = new DataTransfer();
        dt.items.add(new File([new Uint8Array(17 * 1024 * 1024 + 12)], 'tests.zip'));
        const input = document.getElementById('id_problem-data-zipfile');
        input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('document.getElementById("id_upload_id").value === "upload-1"');
    assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), false);
    assert.equal(await evaluate('document.getElementById("id_problem-data-zipfile").hasAttribute("name")'), false);
    assert.deepEqual(await evaluate('window.readyFiles'), {files: ['1.in', '1.out'], autofill: true});
    assert.equal(await evaluate('document.getElementById("test-data-upload").dataset.state'), 'ready');
    assert.deepEqual(requestSizes, [16 * 1024 * 1024, 1024 * 1024 + 12]);
    assert.ok(heads >= 1);
    assert.ok(heartbeatCalls >= 1);

    // Canceling a ready ZIP preserves the authored form and restores the current
    // published file choices. Save must no longer carry the canceled upload ID.
    await evaluate('document.getElementById("fixture-form-value").value = "edited"');
    holdStatus = true;
    await evaluate('document.getElementById("test-upload-retry").click()');
    for (let i = 0; i < 150 && !heldStatus; i++) await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(heldStatus, 'The fixture should hold an upload status response.');
    await evaluate('document.getElementById("test-upload-cancel").click()');
    await until('document.getElementById("test-data-upload").dataset.state === "editing"');
    heldStatus(); heldStatus = null;
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), false);
    assert.equal(await evaluate('document.getElementById("id_upload_id").value'), '');
    assert.equal(await evaluate('document.getElementById("fixture-form-value").value'), 'edited');
    assert.deepEqual(await evaluate('window.readyFiles'), {files:[],autofill:false});

    // A lost Django admission response must be recoverable without reloading or
    // creating a second upload. The server's heartbeat returns the admitted ID.
    loseAdmission = true;
    await evaluate(`(() => {
        const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(512)], 'tests.zip'));
        const input = document.getElementById('id_problem-data-zipfile');
        input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('document.getElementById("test-data-upload").dataset.state === "connection_error"');
    assert.equal(await evaluate('document.getElementById("test-upload-retry").hidden'), false);
    await evaluate('document.getElementById("test-upload-retry").click()');
    await until('document.getElementById("id_upload_id").value === "upload-1"');
    assert.equal(await evaluate('document.getElementById("fixture-form-value").value'), 'edited');
    await evaluate('document.getElementById("test-upload-cancel").click()');
    await until('document.getElementById("test-data-upload").dataset.state === "editing"');

    // Cancel also reconciles a lost admission response before dropping the
    // local selection, so a later heartbeat cannot resurrect an orphan upload.
    loseAdmission = true;
    await evaluate(`(() => {
        const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(512)], 'tests.zip'));
        const input = document.getElementById('id_problem-data-zipfile');
        input.files = dt.files; input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('document.getElementById("test-data-upload").dataset.state === "connection_error"');
    await evaluate('document.getElementById("test-upload-cancel").click()');
    await until('document.getElementById("test-data-upload").dataset.state === "editing"');
    assert.equal(totalSize, 0);
    assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), false);
    await evaluate('window.dispatchEvent(new Event("online"))');
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(await evaluate('document.getElementById("id_upload_id").value'), '');

    // End wins over an older heartbeat still in flight. The late response must
    // not resurrect credentials, unlock Save, or reactivate the closed session.
    await evaluate('window.confirm = () => true');
    holdHeartbeat = true;
    await evaluate('window.dispatchEvent(new Event("online"))');
    for (let i = 0; i < 150 && !heldHeartbeat; i++) await new Promise(resolve => setTimeout(resolve, 20));
    assert.ok(heldHeartbeat, 'The fixture should hold a heartbeat response.');
    await evaluate('document.getElementById("test-upload-end").click()');
    await until('document.getElementById("test-data-upload").dataset.state === "view"');
    heldHeartbeat(); heldHeartbeat = null;
    await new Promise(resolve => setTimeout(resolve, 250));
    assert.equal(await evaluate('document.getElementById("id_edit_session_id").value'), '');
    assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), true);

    // An admin may revoke another editor, and an editor whose own lease is
    // revoked must lose upload/Save controls without losing authored form text.
    conflict = true;
    await evaluate('document.getElementById("test-upload-begin").click()');
    await until('!document.getElementById("test-upload-revoke").hidden');
    assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), true);
    await evaluate('document.getElementById("test-upload-revoke").click()');
    await until('!document.getElementById("test-data-fields").disabled');
    rejectHeartbeats = true;
    await evaluate('window.dispatchEvent(new Event("online"))');
    await until('document.getElementById("test-data-upload").dataset.state === "locked"');
    assert.equal(await evaluate('document.getElementById("id_edit_token").value'), '');
    assert.equal(await evaluate('document.getElementById("zip-save-btn").disabled'), true);
    assert.equal(await evaluate('document.getElementById("fixture-form-value").value'), 'edited');
    assert.equal(await evaluate('document.getElementById("test-upload-cancel").hidden'), true);

    // An orphan of the current administrator is labeled as their previous
    // session. Ending it creates a fresh lease; normal End then releases it.
    rejectHeartbeats = false;
    conflict = true;
    conflictOwner = 'tester';
    await evaluate('document.getElementById("test-upload-begin").click()');
    await until('!document.getElementById("test-upload-revoke").hidden');
    assert.equal(await evaluate('document.getElementById("test-upload-revoke").textContent'), 'End previous edit session');
    await evaluate('document.getElementById("test-upload-revoke").click()');
    await until('!document.getElementById("test-data-fields").disabled');
    await evaluate('document.getElementById("test-upload-end").click()');
    await until('document.getElementById("test-data-upload").dataset.state === "view"');
    assert.equal(await evaluate('document.getElementById("id_edit_session_id").value'), '');
    assert.equal(await evaluate('document.getElementById("test-data-fields").disabled'), true);
    assert.equal(errors.length, 0, String(errors));
    console.log('PASS: Chrome checks tus errors preserve the edit lease, Cancel versus End, own-session recovery, admission rejection, chunk/progress/resume, lost-response recovery, file identity, stale heartbeat, admin revoke, and preservation of form edits.');
} finally {
    ws.close(); server.closeAllConnections(); server.close();
}
