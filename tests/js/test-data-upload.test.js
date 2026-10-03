import assert from 'node:assert/strict';
import test from 'node:test';
import {createHash, webcrypto} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const context = vm.createContext({crypto: webcrypto, DOMException, Uint8Array});
vm.runInContext(await readFile(new URL('../../resources/test-data-upload.js', import.meta.url), 'utf8'), context);
const {fingerprint, formatBytes} = context.TestDataUpload;
const block = 4 * 1024 * 1024;

test('full-content fingerprint matches independent block hash and reads bounded chunks', async () => {
    const data = new Uint8Array(block * 2 + 17);
    data[0] = 7;
    data[block + 9] = 18;
    data[data.length - 1] = 250;
    const blob = new Blob([data]);
    const reads = [];
    const progress = [];
    const file = {
        size: blob.size,
        slice(start, end) {
            reads.push([start, end]);
            assert.ok(end - start <= block);
            return blob.slice(start, end);
        },
    };
    const digests = [];
    for (let start = 0; start < data.length; start += block) {
        digests.push(createHash('sha256').update(data.subarray(start, start + block)).digest());
    }
    const expected = createHash('sha256').update(Buffer.concat(digests)).digest('hex');
    assert.equal(await fingerprint(file, (done) => progress.push(done)), `sha256-blocks-v1:${data.length}:${expected}`);
    assert.equal(reads.length, 3);
    assert.equal(progress.at(-1), data.length);
});

test('same filename and length with a change in the middle cannot resume as the same file', async () => {
    const original = new Uint8Array(block * 3 + 1);
    const changed = original.slice();
    changed[block + 123456] = 1;
    const a = new File([original], 'tests.zip', {lastModified: 1});
    const b = new File([changed], 'tests.zip', {lastModified: 1});
    assert.notEqual(await fingerprint(a), await fingerprint(b));
    assert.equal(await fingerprint(a), await fingerprint(new File([original], 'renamed.zip')));
});

test('canceling verification stops before another block is read', async () => {
    const controller = new AbortController();
    let slices = 0;
    const data = new Blob([new Uint8Array(block * 3)]);
    await assert.rejects(fingerprint({size: data.size, slice(start, end) {
        slices++;
        return data.slice(start, end);
    }}, () => controller.abort(), controller.signal), {name: 'AbortError'});
    assert.equal(slices, 1);
});

test('progress formatting handles the approved file limit without decimal-unit ambiguity', () => {
    assert.equal(formatBytes(0), '0 B');
    assert.equal(formatBytes(16 * 1024 * 1024), '16.0 MiB');
    assert.equal(formatBytes(1024 * 1024 * 1024), '1.0 GiB');
});
