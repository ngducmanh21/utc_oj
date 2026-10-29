"""Bounded CSV parsing, read-only account validation and idempotency primitives."""

import csv
import hashlib
import hmac
import io
import json
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import Language, Organization, UserImportBatch, UserImportOperation
from judge.utils.user_import_config import get_user_import_config

CSV_FIELDS = ('username', 'password', 'name', 'school', 'email', 'organizations', 'password_rotation')
SCHEMA_VERSION = 1


class UserImportError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = str(message)
        super().__init__(self.message)


@dataclass(frozen=True)
class ImportIssue:
    code: str
    message: str
    row: int = 0
    line: int = 0
    field: str = ''

    def as_dict(self):
        return {**asdict(self), 'message': str(self.message)}


@dataclass(frozen=True)
class ImportRow:
    row_number: int
    line_number: int
    username: str
    password: str = field(repr=False)
    name: str
    school: str
    email: str
    organizations: tuple
    password_rotation: object
    target_id: object = None
    resolved: bool = False

    def metadata(self):
        return {'username': self.username, 'name': self.name, 'school': self.school, 'email': self.email,
                'organizations': list(self.organizations), 'password_rotation': self.password_rotation,
                'password_provided': bool(self.password)}


@dataclass(frozen=True)
class ParsedImport:
    rows: tuple = ()
    issues: tuple = ()

    def require_valid(self):
        if self.issues:
            raise UserImportError('invalid_csv', _('Correct the CSV errors before importing.'))


def require_user_import_enabled():
    config = get_user_import_config()
    if not config.enabled:
        raise UserImportError('disabled', _('User import is disabled.'))
    return config


def read_import_csv(upload):
    """Read at most MAX_BYTES+1, even when upload.size is absent or untrusted."""
    limit = get_user_import_config().max_bytes
    if not isinstance(upload, bytes) and not callable(getattr(upload, 'read', None)):
        raise UserImportError('invalid_file', _('Upload a UTF-8 CSV file.'))
    data = upload if isinstance(upload, bytes) else upload.read(limit + 1)
    if not isinstance(data, bytes):
        raise UserImportError('invalid_file', _('Upload a UTF-8 CSV file.'))
    if len(data) > limit:
        raise UserImportError('file_too_large', _('The CSV exceeds the upload size limit.'))
    return data


@sensitive_variables()
def parse_user_csv(upload):
    config = get_user_import_config()
    try:
        data = read_import_csv(upload)
        text = data.decode('utf-8-sig')
        if '\x00' in text:
            raise UnicodeError()
    except UserImportError as exc:
        return ParsedImport(issues=(ImportIssue(exc.code, exc.message),))
    except UnicodeError:
        return ParsedImport(issues=(ImportIssue('invalid_encoding', _('Use UTF-8 text without NUL characters.')),))
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    try:
        headers = [name.strip().lower() for name in next(reader)]
    except (StopIteration, csv.Error):
        return ParsedImport(issues=(ImportIssue('invalid_header', _('The CSV header is missing or malformed.')),))
    if (len(headers) != len(set(headers)) or set(headers) - set(CSV_FIELDS) or
            not {'username', 'password_rotation'} <= set(headers) or headers[-1] != 'password_rotation'):
        message = _('Use known, unique columns and put password_rotation last.')
        return ParsedImport(issues=(ImportIssue('invalid_header', message, line=1),))

    rows, issues = [], []
    seen = defaultdict(list)
    record_number = 0
    try:
        while True:
            line = reader.line_num + 1
            try:
                values = next(reader)
            except StopIteration:
                break
            if not values or all(value == '' for value in values):
                continue
            record_number += 1
            if record_number > config.max_rows:
                issues.append(ImportIssue('too_many_rows', _('The CSV exceeds the row limit.'),
                                          record_number, line))
                break

            def error(code, message, name=''):
                issues.append(ImportIssue(code, message, record_number, line, name))

            if len(values) != len(headers):
                error('invalid_width', _('The row has a different number of cells than the header.'))
                continue
            item = dict(zip(headers, values))
            item = {name: item.get(name, '') for name in CSV_FIELDS}
            for name in CSV_FIELDS:
                if name != 'password':
                    item[name] = item[name].strip()
            username = item['username']
            if not re.fullmatch(r'\w+', username) or len(username) > User._meta.get_field('username').max_length:
                error('invalid_username', _('Use a nonempty username containing word characters within the limit.'),
                      'username')
            elif username:
                seen[username].append((record_number, line))
            for name, model_field in (('name', 'first_name'), ('school', 'last_name'), ('email', 'email')):
                if len(item[name]) > User._meta.get_field(model_field).max_length:
                    error('field_too_long', _('The value exceeds the field length limit.'), name)
            if item['email']:
                try:
                    validate_email(item['email'])
                except ValidationError:
                    error('invalid_email', _('Enter a valid email address.'), 'email')
            password = item['password']
            if password and (not password.strip() or len(password) > config.max_password_length):
                error('invalid_password', _('The password is blank or exceeds the password length limit.'), 'password')
            rotation = {'true': True, 'false': False}.get(item['password_rotation'])
            if rotation is None:
                error('invalid_policy', _('password_rotation must be true or false.'), 'password_rotation')
            orgs = tuple(dict.fromkeys(slug.strip() for slug in item['organizations'].split('&')))
            if not item['organizations']:
                orgs = ()
            if any(not slug or len(slug) > Organization._meta.get_field('slug').max_length for slug in orgs):
                error('invalid_organization', _('Use nonempty organization slugs separated by &.'), 'organizations')
            rows.append(ImportRow(record_number, line, username, password, item['name'], item['school'],
                                  item['email'], orgs, rotation))
    except csv.Error:
        issues.append(ImportIssue('malformed_csv', _('The CSV quoting or field size is invalid.'),
                                  record_number + 1, reader.line_num))
    for occurrences in seen.values():
        if len(occurrences) > 1:
            issues.extend(ImportIssue('duplicate_username', _('This username appears more than once in the CSV.'),
                                      row, line, 'username') for row, line in occurrences)
    if record_number == 0 and not issues:
        issues.append(ImportIssue('empty_csv', _('The CSV contains no account rows.')))
    return ParsedImport(tuple(rows), tuple(issues))


def _resolve_database_usernames(rows):
    """Use the username column's actual collation for both duplicates and lookup."""
    if not rows:
        return {}
    if connection.vendor != 'mysql':
        raise UserImportError('unsupported_database', _('User import requires MySQL or MariaDB.'))
    table = User._meta.db_table
    column = User._meta.get_field('username').column
    with connection.cursor() as cursor:
        cursor.execute('SELECT CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS '
                       'WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s', [table, column])
        charset, collation = cursor.fetchone()
        if not all(re.fullmatch(r'[a-zA-Z0-9_]+', name or '') for name in (charset, collation)):
            raise UserImportError('unsupported_collation', _('Unable to check the username collation.'))
        parts = ['SELECT CONVERT(%s USING ' + charset + ') COLLATE ' + collation +
                 ' AS import_username, %s AS import_row' for row in rows]
        parameters = [value for row in rows for value in (row.username, row.row_number)]
        quote = connection.ops.quote_name
        sql = ('SELECT source.import_row, COUNT(*) OVER (PARTITION BY source.import_username), target.id FROM (' +
               ' UNION ALL '.join(parts) + ') source LEFT JOIN ' + quote(table) +
               ' target ON target.' + quote(column) + ' = source.import_username')
        cursor.execute(sql, parameters)
        return {number: (count, target) for number, count, target in cursor.fetchall()}


def validate_import_accounts(parsed):
    """Read-only validation. Password validators are run separately before preview."""
    issues = list(parsed.issues)
    invalid_names = {issue.row for issue in issues if issue.code == 'invalid_username'}
    candidates = [row for row in parsed.rows if row.row_number not in invalid_names]
    matches = _resolve_database_usernames(candidates)
    users = User.objects.select_related('profile').in_bulk(
        target for count, target in matches.values() if target is not None)
    known_orgs = {}
    rows = []
    language_exists = None
    for row in parsed.rows:
        if row.row_number not in matches:
            rows.append(row)
            continue
        count, target_id = matches[row.row_number]
        target = users.get(target_id)

        def error(code, message, name='username'):
            issues.append(ImportIssue(code, message, row.row_number, row.line_number, name))

        if count > 1 and not any(i.row == row.row_number and i.code == 'duplicate_username' for i in issues):
            error('duplicate_username', _('The database treats multiple CSV usernames as the same account.'))
        if target:
            if target.username != row.username:
                error('username_conflict', _('Use the exact spelling of the existing username.'))
            profile = getattr(target, 'profile', None)
            if target.is_staff or target.is_superuser or not target.is_active or profile is None or profile.is_banned:
                error('ineligible_account', _('Only active ordinary accounts with a profile can be imported.'))
        else:
            if not row.password:
                error('password_required', _('New accounts require a password.'), 'password')
            if language_exists is None:
                language_exists = Language.objects.filter(key=settings.DEFAULT_USER_LANGUAGE).exists()
            if not language_exists:
                error('missing_default_language', _('The configured default language does not exist.'), '')
        for slug in row.organizations:
            if slug not in known_orgs:
                known_orgs[slug] = Organization.objects.filter(slug=slug).exists()
            if not known_orgs[slug]:
                error('unknown_organization', _('An organization slug in this row does not exist.'), 'organizations')
        rows.append(replace(row, target_id=target_id, resolved=True))
    return ParsedImport(tuple(rows), tuple(issues))


def parse_idempotency_key(value):
    try:
        if not isinstance(value, (str, uuid.UUID)):
            raise ValueError()
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise UserImportError('invalid_key', _('Use a UUID idempotency key.')) from None


@sensitive_variables()
def import_request_digest(action, payload):
    config = require_user_import_enabled()
    if action not in UserImportOperation.Action.values:
        raise UserImportError('invalid_action', _('Unknown import operation.'))
    try:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise UserImportError('invalid_payload', _('Use a valid import request object.')) from None
    return hmac.new(config.digest_key, ('utcoj.user-import.v1:' + action + ':' + body).encode(),
                    hashlib.sha256).hexdigest()


@sensitive_variables()
def upload_request_digest(data, muted):
    config = require_user_import_enabled()
    if type(muted) is not bool:
        raise UserImportError('invalid_mute', _('The mute option must be a boolean.'))
    data = read_import_csv(data)
    digest = hmac.new(config.digest_key, b'utcoj.user-import.upload.v1:' + bytes([muted]), hashlib.sha256)
    digest.update(data)
    return digest.hexdigest()


@transaction.atomic
def get_or_create_import_operation(actor, action, key, request_digest, create_batch):
    """Bind one request to one batch, including concurrent deliveries and rollback.

    create_batch only reserves metadata. Account mutations belong to the later
    batch executor, never to the upload/submit request callback.
    """
    require_user_import_enabled()
    if (not getattr(actor, 'is_authenticated', False) or not actor.is_active or
            not actor.is_staff or not actor.is_superuser or
            getattr(actor, 'is_impersonate', False)):
        raise PermissionDenied()
    key = parse_idempotency_key(key)
    if (action not in UserImportOperation.Action.values or not isinstance(request_digest, str) or
            not re.fullmatch(r'[a-f0-9]{64}', request_digest)):
        raise UserImportError('invalid_operation', _('Use a valid import operation and digest.'))
    operation, created = UserImportOperation.objects.select_for_update().get_or_create(
        actor=actor, action=action, idempotency_key=key, defaults={'request_digest': request_digest},
    )
    if not constant_time_compare(operation.request_digest, request_digest):
        raise UserImportError('idempotency_conflict', _('This idempotency key belongs to a different request.'))
    if created:
        batch = create_batch()
        if not isinstance(batch, UserImportBatch) or batch.actor_id != actor.pk or batch.idempotency_key != key:
            raise UserImportError('invalid_batch', _('The batch does not match the request owner or key.'))
        operation.batch = batch
        operation.save(update_fields=['batch'])
    elif operation.batch_id is None:
        raise UserImportError('unavailable', _('The import request has no recoverable batch.'))
    return operation, created
