"""Snapshot and atomic account application for the CSV importer.

The validation worker will call capture -> prepare_import_password -> finalize.
The execution worker must claim RUNNING with a fresh attempt token before apply.
This module has no HTTP, queue dispatch, retry loop or account registration mail.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_variables

from judge.models import EFFECTIVE_MATH_ENGINES, Language, Organization, Profile, UserImportBatch, UserImportRow
from judge.utils.managed_login import on_admin_password_changed, set_login_policy
from judge.utils.user_import import ImportIssue, SCHEMA_VERSION, UserImportError, _resolve_database_usernames, \
    import_request_digest, parse_idempotency_key, require_user_import_enabled
from judge.utils.user_import_storage import get_prepared_import_password

USER_FIELDS = {'name': 'first_name', 'school': 'last_name', 'email': 'email'}
INPUT_FIELDS = {'username', 'name', 'school', 'email', 'organizations', 'password_rotation', 'password_provided'}
logger = logging.getLogger(__name__)


class ImportBatchConflict(UserImportError):
    def __init__(self, code='stale_snapshot', rows=()):
        super().__init__(code, _('The batch changed or an account changed. Upload and preview the CSV again.'))
        self.issues = tuple(ImportIssue(code, self.message, row.row_number, row.line_number) for row in rows)


@dataclass(frozen=True)
class ImportBatchResult:
    batch_id: object
    version: int
    total: int
    created: int
    updated: int
    replayed: bool = False


def _actor_guard(actor):
    if (not getattr(actor, 'is_authenticated', False) or not actor.is_active or
            not actor.is_staff or not actor.is_superuser or getattr(actor, 'is_impersonate', False)):
        raise PermissionDenied()


def _batch(actor, batch_id, version):
    _actor_guard(actor)
    if type(version) is not int or version < 1:
        raise UserImportError('invalid_version', _('Use a positive integer preview version.'))
    batch = UserImportBatch.objects.select_for_update().get(pk=batch_id)
    if batch.actor_id != actor.pk:
        raise PermissionDenied()
    if batch.version != version or batch.schema_version != SCHEMA_VERSION:
        raise ImportBatchConflict('stale_version')
    return batch


def _rows(batch):
    rows = list(batch.rows.select_for_update().order_by('row_number'))
    if batch.errors or not rows or len(rows) != batch.total_rows or len(rows) > require_user_import_enabled().max_rows:
        raise ImportBatchConflict('invalid_rows')
    for row in rows:
        data = row.input_data
        valid = isinstance(data, dict) and set(data) == INPUT_FIELDS
        if valid:
            valid = (isinstance(data['username'], str) and data['username'] == row.username and
                     re.fullmatch(r'\w+', row.username) and
                     len(row.username) <= User._meta.get_field('username').max_length and
                     type(data['password_provided']) is bool and type(data['password_rotation']) is bool and
                     isinstance(data['organizations'], list))
        if valid:
            valid = all(isinstance(data[name], str) and len(data[name]) <= User._meta.get_field(field).max_length
                        for name, field in USER_FIELDS.items())
        if valid:
            slugs = data['organizations']
            valid = (all(isinstance(slug, str) and 0 < len(slug) <= Organization._meta.get_field('slug').max_length
                         for slug in slugs) and
                     len(slugs) == len(set(slugs)))
        if valid and data['email']:
            try:
                validate_email(data['email'])
            except ValidationError:
                valid = False
        if not valid or row.errors or row.status == UserImportRow.Status.INVALID:
            raise ImportBatchConflict('invalid_row', [row])
        if row.action not in (UserImportRow.Action.CREATE, UserImportRow.Action.UPDATE):
            raise ImportBatchConflict('invalid_action', [row])
        if (row.action == UserImportRow.Action.UPDATE) != (row.target_id is not None):
            raise ImportBatchConflict('missing_target', [row])
        if row.action == UserImportRow.Action.CREATE and not data['password_provided']:
            raise ImportBatchConflict('password_required', [row])
    return rows


@sensitive_variables()
def _security_fingerprint(user, profile):
    state = [user.pk, user.password, user.is_active, user.is_staff, user.is_superuser, profile.pk,
             profile.rotate_password_on_login, profile.account_session_epoch, profile.api_token,
             profile.password_rotated_at.isoformat() if profile.password_rotated_at else None,
             profile.ban_reason, profile.is_totp_enabled, profile.totp_key, profile.scratch_codes,
             profile.is_webauthn_enabled,
             list(profile.webauthn_credentials.order_by('pk').values_list('pk', 'cred_id', 'public_key'))]
    return import_request_digest('submit', {'domain': 'account-baseline', 'state': state})


@sensitive_variables()
def _digest(batch, rows, actor, profile, phase):
    payload = {'domain': 'batch-snapshot', 'phase': phase, 'id': str(batch.pk), 'schema': batch.schema_version,
               'version': batch.version, 'key': str(batch.idempotency_key), 'muted': batch.muted,
               'actor': _security_fingerprint(actor, profile),
               'created': batch.created_at.isoformat(), 'expiry': batch.secret_expires_at.isoformat(),
               'validators': settings.AUTH_PASSWORD_VALIDATORS, 'hashers': settings.PASSWORD_HASHERS, 'rows': []}
    for row in rows:
        item = {'id': row.pk, 'number': row.row_number, 'line': row.line_number, 'username': row.username,
                'target': row.target_id, 'action': row.action, 'input': row.input_data,
                'baseline': row.baseline, 'preview': row.preview}
        if phase == 'preview':
            item['prepared'] = hashlib.sha256(row.prepared_password_ciphertext.encode()).hexdigest()
            item['expiry'] = (row.prepared_password_expires_at.isoformat()
                              if row.prepared_password_expires_at else None)
        payload['rows'].append(item)
    return import_request_digest('submit', payload)


def _locked_accounts(batch, rows, actor, frozen):
    # Acquire every existing User before any Profile/Organization, across the
    # whole batch rather than per CSV row. Managed-login helpers reuse these locks.
    user_ids = {batch.actor_id} | {row.target_id for row in rows if row.target_id is not None}
    users = {user.pk: user for user in User.objects.select_for_update().filter(pk__in=user_ids).order_by('pk')}
    current_actor = users.get(actor.pk)
    _actor_guard(current_actor)
    profiles = {profile.user_id: profile for profile in
                Profile.objects.select_for_update().filter(user_id__in=user_ids).order_by('pk')}
    if current_actor.pk not in profiles:
        raise PermissionDenied()
    matches = _resolve_database_usernames(rows)
    for row in rows:
        count, user_id = matches[row.row_number]
        if count != 1 or user_id != row.target_id:
            raise ImportBatchConflict('username_conflict', [row])
        if user_id is not None:
            user = users.get(user_id)
            profile = profiles.get(user_id)
            if (user is None or profile is None or user.username != row.username or
                    not user.is_active or user.is_staff or user.is_superuser or profile.is_banned):
                raise ImportBatchConflict('ineligible_account', [row])

    plans, resolved_orgs = {}, {}
    for row in rows:
        if frozen:
            plans[row.pk] = row.baseline.get('organizations', [])
        else:
            plan = []
            for slug in row.input_data['organizations']:
                if slug not in resolved_orgs:
                    resolved_orgs[slug] = Organization.objects.filter(slug=slug).first()
                org = resolved_orgs[slug]
                if org is None:
                    raise ImportBatchConflict('unknown_organization', [row])
                plan.append({'id': org.pk, 'slug': org.slug, 'requested_slug': slug})
            plans[row.pk] = plan
    org_ids = {org['id'] for plan in plans.values() for org in plan}
    orgs = {org.pk: org for org in Organization.objects.select_for_update().filter(pk__in=org_ids).order_by('pk')}
    profile_ids = [profile.pk for profile in profiles.values()]
    memberships = set(Profile.organizations.through.objects.select_for_update()
                      .filter(profile_id__in=profile_ids, organization_id__in=org_ids)
                      .order_by('profile_id', 'organization_id').values_list('profile_id', 'organization_id'))
    for row in rows:
        if len(plans[row.pk]) != len(row.input_data['organizations']):
            raise ImportBatchConflict('organization_conflict', [row])
        for slug, plan in zip(row.input_data['organizations'], plans[row.pk]):
            if (plan.get('requested_slug') != slug or plan['id'] not in orgs or
                    orgs[plan['id']].slug != plan['slug']):
                raise ImportBatchConflict('organization_conflict', [row])
    return current_actor, users, profiles, plans, memberships


def _baseline(row, users, profiles, plans, memberships):
    user = users.get(row.target_id)
    profile = profiles.get(row.target_id)
    data = row.input_data
    fields = {field: getattr(user, field) if user else '' for name, field in USER_FIELDS.items()
              if data[name] or data['password_provided']}
    fields['mute'] = profile.mute if profile else False
    organizations = [{**{key: item[key] for key in ('id', 'slug', 'requested_slug')},
                      'member': profile is not None and (profile.pk, item['id']) in memberships}
                     for item in plans[row.pk]]
    baseline = {'profile_id': profile.pk if profile else None, 'fields': fields, 'organizations': organizations,
                'credential_fingerprint': _security_fingerprint(user, profile) if user else ''}
    if user is None:
        language = Language.objects.filter(key=settings.DEFAULT_USER_LANGUAGE).first()
        if language is None:
            raise ImportBatchConflict('missing_default_language', [row])
        baseline.update(default_language_id=language.pk, default_timezone=settings.DEFAULT_USER_TIME_ZONE)
    return baseline


def _preview(row, users, profiles, muted):
    user, profile = users.get(row.target_id), profiles.get(row.target_id)
    changes = {}
    for name, field in USER_FIELDS.items():
        if row.input_data[name]:
            changes[field] = {'before': getattr(user, field) if user else '', 'after': row.input_data[name]}
    enabled = profile.rotate_password_on_login if profile else False
    return {'changes': changes, 'password_provided': row.input_data['password_provided'],
            'mute': {'before': profile.mute if profile else False, 'after': muted},
            'password_rotation': {'before': enabled, 'after': row.input_data['password_rotation']},
            'organizations_to_add': [org['slug'] for org in row.baseline['organizations'] if not org['member']],
            'warnings': ['password_not_restored'] if enabled and not row.input_data['password_rotation'] and
            not row.input_data['password_provided'] else []}


def _check_baselines(rows, users, profiles, plans, memberships):
    stale = [row for row in rows if row.baseline != _baseline(row, users, profiles, plans, memberships)]
    if stale:
        raise ImportBatchConflict(rows=stale)


def _candidate(row, users):
    user = users.get(row.target_id)
    values = {field: row.input_data[name] or (getattr(user, field) if user else '')
              for name, field in USER_FIELDS.items()}
    return User(pk=row.target_id, username=row.username, **values)


@transaction.atomic
def capture_import_snapshot(actor, batch_id, expected_version):
    """Capture account baseline before expensive password validation; no User writes.

    Returns (row, unsaved candidate User) pairs for prepare_import_password().
    Repeated calls verify/reuse the original baseline rather than replacing it.
    """
    require_user_import_enabled()
    batch = _batch(actor, batch_id, expected_version)
    if batch.status != UserImportBatch.Status.VALIDATING or timezone.now() >= batch.secret_expires_at:
        raise UserImportError('invalid_batch_state', _('The batch is not available for validation.'))
    rows = _rows(batch)
    frozen = bool(batch.snapshot_digest)
    current_actor, users, profiles, plans, memberships = _locked_accounts(batch, rows, actor, frozen)
    if frozen:
        expected = _digest(batch, rows, current_actor, profiles[actor.pk], 'baseline')
        if not constant_time_compare(batch.snapshot_digest, expected):
            raise ImportBatchConflict()
        _check_baselines(rows, users, profiles, plans, memberships)
    else:
        if any(row.baseline for row in rows):
            raise ImportBatchConflict('invalid_snapshot')
        for row in rows:
            row.baseline = _baseline(row, users, profiles, plans, memberships)
            row.preview = _preview(row, users, profiles, batch.muted)
            row.save(update_fields=['baseline', 'preview'])
        batch.snapshot_digest = _digest(batch, rows, current_actor, profiles[actor.pk], 'baseline')
        batch.save(update_fields=['snapshot_digest', 'updated_at'])
    return [(row, _candidate(row, users)) for row in rows]


@sensitive_variables()
def _prepared_passwords(rows):
    passwords = {}
    for row in rows:
        provided = row.input_data['password_provided']
        if row.password_ciphertext or row.password_expires_at is not None:
            raise ImportBatchConflict('password_not_validated', [row])
        if not provided:
            if row.prepared_password_ciphertext or row.prepared_password_expires_at is not None:
                raise ImportBatchConflict('unexpected_password', [row])
            passwords[row.pk] = None
            continue
        encoded = get_prepared_import_password(row)
        if not encoded or len(encoded) > User._meta.get_field('password').max_length:
            raise ImportBatchConflict('missing_prepared_password', [row])
        passwords[row.pk] = encoded
    return passwords


@transaction.atomic
def finalize_import_preview(actor, batch_id, expected_version):
    """Seal the preview after all validators finish, rechecking their account input."""
    config = require_user_import_enabled()
    batch = _batch(actor, batch_id, expected_version)
    if batch.status not in (UserImportBatch.Status.VALIDATING, UserImportBatch.Status.PREVIEW_READY):
        raise UserImportError('invalid_batch_state', _('The batch cannot be made ready for preview.'))
    if timezone.now() >= batch.secret_expires_at or not batch.snapshot_digest:
        raise ImportBatchConflict('expired_snapshot')
    rows = _rows(batch)
    current_actor, users, profiles, plans, memberships = _locked_accounts(batch, rows, actor, True)
    phase = 'preview' if batch.status == UserImportBatch.Status.PREVIEW_READY else 'baseline'
    if not constant_time_compare(batch.snapshot_digest, _digest(batch, rows, current_actor, profiles[actor.pk], phase)):
        raise ImportBatchConflict()
    _check_baselines(rows, users, profiles, plans, memberships)
    _prepared_passwords(rows)
    if batch.status == UserImportBatch.Status.PREVIEW_READY:
        return batch
    for row in rows:
        row.status = UserImportRow.Status.VALID
        row.save(update_fields=['status'])
    batch.status = UserImportBatch.Status.PREVIEW_READY
    batch.preview_expires_at = min(timezone.now() + timedelta(seconds=config.preview_seconds), batch.secret_expires_at)
    batch.snapshot_digest = _digest(batch, rows, current_actor, profiles[actor.pk], 'preview')
    batch.save(update_fields=['status', 'preview_expires_at', 'snapshot_digest', 'updated_at'])
    return batch


@transaction.atomic
def verify_import_submission(actor, batch_id, expected_version):
    """Check the immutable preview and current actor, without locking targets.

    The executor checks every live target baseline under the full account lock.
    Submit only binds the already reviewed snapshot; it never refreshes a diff.
    """
    require_user_import_enabled()
    batch = _batch(actor, batch_id, expected_version)
    if (batch.status != UserImportBatch.Status.PREVIEW_READY or
            timezone.now() >= min(batch.preview_expires_at, batch.secret_expires_at)):
        raise ImportBatchConflict('expired_snapshot')
    rows = _rows(batch)
    current_actor = User.objects.select_for_update().get(pk=actor.pk)
    _actor_guard(current_actor)
    profile = Profile.objects.select_for_update().get(user=current_actor)
    if not constant_time_compare(batch.snapshot_digest, _digest(batch, rows, current_actor, profile, 'preview')):
        raise ImportBatchConflict()
    if any(row.status != UserImportRow.Status.VALID for row in rows):
        raise ImportBatchConflict('invalid_rows')
    _prepared_passwords(rows)
    return batch


def _result(batch, replayed=False):
    return ImportBatchResult(batch.pk, batch.version, batch.applied_rows, batch.created_users,
                             batch.updated_users, replayed)


@sensitive_variables()
def _apply_row(actor, batch, row, users, profiles, encoded):
    fields = {field: row.input_data[name] for name, field in USER_FIELDS.items() if row.input_data[name]}
    created = row.action == UserImportRow.Action.CREATE
    if created:
        try:
            user = User.objects.create(username=row.username, password=encoded, is_active=True,
                                       is_staff=False, is_superuser=False, **fields)
        except IntegrityError as error:
            # A concurrent creator may commit after our nonlocking name lookup.
            # Abort the entire outer transaction; never reinterpret CREATE as UPDATE.
            if error.args and error.args[0] == 1062:
                raise ImportBatchConflict('username_conflict', [row]) from None
            raise
        profile = Profile.objects.create(user=user, language_id=row.baseline['default_language_id'],
                                         timezone=row.baseline['default_timezone'], mute=batch.muted)
    else:
        user, profile = users[row.target_id], profiles[row.target_id]
        for name, value in fields.items():
            setattr(user, name, value)
        if encoded is not None:
            user.password = encoded
            fields['password'] = encoded
        if fields:
            user.save(update_fields=list(fields))
        if encoded is not None:
            on_admin_password_changed(actor, user)
            profile.refresh_from_db()
        if profile.mute != batch.muted:
            profile.mute = batch.muted
            profile.save(update_fields=['mute'])
    enabled = row.input_data['password_rotation']
    if profile.rotate_password_on_login != enabled:
        set_login_policy(actor, user.pk, enabled, profile.account_session_epoch,
                         reason='CSV import %s row %s' % (batch.pk, row.row_number))
    missing = [org['id'] for org in row.baseline['organizations'] if not org['member']]
    if missing:
        profile.organizations.add(*missing)
    row.target = user
    row.status = UserImportRow.Status.SUCCESS
    row.finished_at = timezone.now()
    row.result = {'action': row.action, 'user_id': user.pk, 'credential_issued': encoded is not None,
                  'added_organizations': missing, 'changed_fields': list(fields)}
    row.password_ciphertext = row.prepared_password_ciphertext = ''
    row.password_expires_at = row.prepared_password_expires_at = None
    row.save(update_fields=['target', 'status', 'finished_at', 'result', 'password_ciphertext', 'password_expires_at',
                            'prepared_password_ciphertext', 'prepared_password_expires_at'])
    return created


@sensitive_variables()
def execute_import_batch(actor, batch_id, expected_version, attempt_token, *, progress=None):
    """Apply a claimed batch once. Failure propagates after the outer rollback.

    The queue/recovery layer owns RUNNING, attempt budget and sanitized failure
    records. It must reconcile a lost commit response before scheduling another
    attempt. A completion marker is committed with all account changes here.
    """
    config = require_user_import_enabled()
    token = parse_idempotency_key(attempt_token)
    with transaction.atomic(durable=True):
        batch = _batch(actor, batch_id, expected_version)
        # Always recheck current actor rights, including for a completed replay.
        current_actor = User.objects.get(pk=batch.actor_id)
        _actor_guard(current_actor)
        if batch.status == UserImportBatch.Status.COMPLETED:
            return _result(batch, replayed=True)
        rows = _rows(batch)
        now = timezone.now()
        if (batch.status != UserImportBatch.Status.RUNNING or batch.attempt_token != token or
                not 1 <= batch.execution_attempts <= config.max_attempts or
                batch.lease_expires_at is None or batch.lease_expires_at <= now):
            raise UserImportError('stale_attempt', _('This worker no longer owns the batch.'))
        # An accepted submission uses the secret/execution deadline. The preview
        # deadline limits confirmation, not a job already waiting in the queue.
        submitted = batch.operations.filter(action='submit').exists()
        if (batch.errors or now >= batch.secret_expires_at or
                (not submitted and now >= batch.preview_expires_at)):
            raise ImportBatchConflict('expired_snapshot')
        if any(row.status != UserImportRow.Status.VALID for row in rows):
            raise ImportBatchConflict('invalid_rows')
        current_actor, users, profiles, plans, memberships = _locked_accounts(batch, rows, actor, True)
        expected = _digest(batch, rows, current_actor, profiles[actor.pk], 'preview')
        if not constant_time_compare(batch.snapshot_digest, expected):
            raise ImportBatchConflict()
        _check_baselines(rows, users, profiles, plans, memberships)
        passwords = _prepared_passwords(rows)
        created = 0
        for number, row in enumerate(rows, 1):
            try:
                created += _apply_row(current_actor, batch, row, users, profiles, passwords[row.pk])
            except Exception as error:
                error.import_row = (row.row_number, row.line_number)
                raise
            if progress is not None and (number == len(rows) or number % max(1, len(rows) // 100) == 0):
                try:
                    progress(number, len(rows))
                except Exception:
                    logger.warning('User import progress callback failed for batch %s', batch.pk)
        if timezone.now() >= batch.secret_expires_at:
            raise ImportBatchConflict('expired_secret')
        batch.status = UserImportBatch.Status.COMPLETED
        batch.applied_rows = len(rows)
        batch.created_users = created
        batch.updated_users = len(rows) - created
        batch.finished_at = timezone.now()
        batch.lease_expires_at = None
        # Persist repair intent with the account commit, so a worker lost before
        # ACK/cache callbacks cannot lose the independent cache repair work.
        batch.errors = [ImportIssue('cache_repair_pending',
                                    _('Accounts were saved. Display caches are waiting for repair.')).as_dict()]
        batch.save(update_fields=['status', 'applied_rows', 'created_users', 'updated_users',
                                  'finished_at', 'lease_expires_at', 'errors', 'updated_at'])
        return _result(batch)


def invalidate_import_caches(batch_id):
    """Operational cache repair after commit; never reissue account credentials."""
    batch = UserImportBatch.objects.get(pk=batch_id, status=UserImportBatch.Status.COMPLETED)
    rows = list(batch.rows.all())
    profiles = dict(Profile.objects.filter(user_id__in=[row.target_id for row in rows if row.target_id])
                    .values_list('user_id', 'pk'))
    keys = []
    for row in rows:
        if row.target_id in profiles:
            keys.extend(make_template_fragment_key('user_about', (profiles[row.target_id], engine))
                        for engine in EFFECTIVE_MATH_ENGINES)
        for org in row.baseline['organizations']:
            keys.extend(make_template_fragment_key('organization_html', (org['id'], engine))
                        for engine in EFFECTIVE_MATH_ENGINES)
    cache.delete_many(list(set(keys)))
