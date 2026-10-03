import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const context = vm.createContext({window: {}, document: {readyState: 'complete', querySelector: () => null}});
vm.runInContext(await readFile(new URL('../../resources/registration-email.js', import.meta.url), 'utf8'), context);

function fixture(domains = 'lms.utc.edu.vn,gmail.com') {
    const events = {};
    const formEvents = {};
    const input = {
        value: '',
        dataset: {registrationEmailDomains: domains,
            registrationEmailError: 'Chỉ chấp nhận email có đuôi @lms.utc.edu.vn, @gmail.com.'},
        customError: '',
        addEventListener(name, handler) { events[name] = handler; },
        form: {addEventListener(name, handler) { formEvents[name] = handler; }},
        setCustomValidity(message) { this.customError = message; },
        setAttribute(name, value) { this[name] = value; },
        get validity() {
            const valueMissing = !this.value.trim();
            const typeMismatch = !valueMissing && !/^[^@\s]+@[^@\s]+$/.test(this.value.trim());
            return {typeMismatch, valid: !valueMissing && !typeMismatch && !this.customError};
        },
        get validationMessage() {
            return this.customError || (this.validity.valid ? '' : 'Native email error');
        },
        checkValidity() { return this.validity.valid; },
        reportValidity() { this.reported = true; },
    };
    const error = {textContent: 'Server error', hidden: false};
    context.window.RegistrationEmail.init(input, error);
    return {input, error, events, formEvents};
}

test('typing an outside domain or suffix lookalike shows the translated error', () => {
    const {input, error, events} = fixture();
    for (const domain of ['outlook.com', 'gmail.com.evil.com', 'sub.gmail.com', 'fakegmail.com', 'gmail.com.', 'utc.edu.vn', 'sub.lms.utc.edu.vn', 'lms.utc.edu.vn.evil.com',
        'fakelms.utc.edu.vn', 'lms.utc.edu.vn.']) {
        input.value = `student@${domain}`;
        events.input();
        assert.equal(input.checkValidity(), false);
        assert.equal(error.textContent, input.dataset.registrationEmailError);
        assert.equal(error.hidden, false);
        assert.equal(input['aria-invalid'], 'true');
    }
});

test('membership follows the backend whitelist including Gmail and arbitrary domains', () => {
    for (const domain of ['lms.utc.edu.vn', 'gmail.com', 'example.edu']) {
        const {input, events} = fixture(domain);
        input.value = ` Student@${domain.toUpperCase()} `;
        events.blur();
        assert.equal(input.checkValidity(), true);
        input.value = 'student@outlook.com';
        events.input();
        assert.equal(input.checkValidity(), false);
    }
});

test('empty whitelist blocks any address and submit', () => {
    const {input, events, formEvents} = fixture('');
    input.value = 'student@gmail.com';
    events.input();
    assert.equal(input.checkValidity(), false);
    let prevented = false;
    formEvents.submit({preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
});

test('correcting the domain clears errors and accepts uppercase and trimmed email', () => {
    const {input, error, events} = fixture();
    input.value = 'Student@outlook.com';
    events.input();
    input.value = ' Student@LMS.UTC.EDU.VN ';
    events.blur();
    assert.equal(input.value, 'Student@LMS.UTC.EDU.VN');
    assert.equal(input.checkValidity(), true);
    assert.equal(error.textContent, '');
    assert.equal(error.hidden, true);
    assert.equal(input['aria-invalid'], 'false');
});

test('submit validates autofilled values without input events and prevents invalid registration', () => {
    const {input, formEvents} = fixture();
    let prevented = false;
    input.value = 'student@outlook.com';
    formEvents.submit({preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(input.reported, true);
    input.value = ' student@lms.utc.edu.vn ';
    formEvents.submit({preventDefault() { assert.fail('valid email must submit'); }});
    assert.equal(input.value, 'student@lms.utc.edu.vn');
});

test('initialization preserves server errors and leaves empty and malformed emails to native validation', () => {
    const {input, error, events} = fixture();
    assert.equal(error.textContent, 'Server error');
    for (const value of ['', 'student@@lms.utc.edu.vn', 'student']) {
        input.value = value;
        events.change();
        assert.equal(input.customError, '');
        assert.equal(input.checkValidity(), false);
    }
});
