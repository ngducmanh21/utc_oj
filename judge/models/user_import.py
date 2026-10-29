"""Import metadata and temporary encrypted credentials; no account mutations."""

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


def validate_import_metadata(value):
    """Keep credential material out of metadata exposed to future status views."""
    forbidden = {'password', 'password_hash', 'password_ciphertext', 'prepared_password_ciphertext',
                 'ciphertext', 'api_token', 'totp_key', 'scratch_codes', 'raw_csv'}
    if isinstance(value, dict):
        if any(not isinstance(key, str) or key.lower() in forbidden for key in value):
            raise ValidationError('Import metadata must not contain credentials.', code='unsafe_metadata')
        for child in value.values():
            validate_import_metadata(child)
    elif isinstance(value, list):
        for child in value:
            validate_import_metadata(child)


class ImportMetadataModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        # Also protect normal ORM creates, which don't call full_clean(). Services
        # must not bypass this with bulk_create/update of metadata JSON fields.
        for field in self._meta.fields:
            if isinstance(field, models.JSONField):
                validate_import_metadata(getattr(self, field.attname))
        return super().save(*args, **kwargs)


class UserImportBatch(ImportMetadataModel):
    class Status(models.TextChoices):
        VALIDATING = 'VALIDATING'
        INVALID = 'INVALID'
        PREVIEW_READY = 'PREVIEW_READY'
        QUEUED = 'QUEUED'
        RUNNING = 'RUNNING'
        COMPLETED = 'COMPLETED'
        CONFLICT = 'CONFLICT'
        FAILED = 'FAILED'
        EXPIRED = 'EXPIRED'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='user_import_batches')
    idempotency_key = models.UUIDField(editable=False)
    schema_version = models.PositiveSmallIntegerField(default=1)
    version = models.PositiveIntegerField(default=1)
    snapshot_digest = models.CharField(max_length=96, blank=True, editable=False)
    muted = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.VALIDATING)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)
    preview_expires_at = models.DateTimeField()
    secret_expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    validation_attempts = models.PositiveIntegerField(default=0)
    execution_attempts = models.PositiveIntegerField(default=0)
    attempt_token = models.UUIDField(null=True, blank=True, editable=False)
    task_id = models.CharField(max_length=255, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    total_rows = models.PositiveIntegerField(default=0)
    applied_rows = models.PositiveIntegerField(default=0)
    created_users = models.PositiveIntegerField(default=0)
    updated_users = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True, validators=[validate_import_metadata])

    class Meta:
        default_permissions = ('view',)
        constraints = [
            models.UniqueConstraint(fields=['actor', 'idempotency_key'], name='user_import_batch_key'),
            models.CheckConstraint(check=models.Q(version__gte=1), name='user_import_batch_version'),
            models.CheckConstraint(check=(~models.Q(status='COMPLETED') & models.Q(applied_rows=0)) |
                                   models.Q(status='COMPLETED', applied_rows=models.F('total_rows')),
                                   name='user_import_batch_atomic'),
        ]
        indexes = [models.Index(fields=['actor', 'status'], name='user_import_actor_state')]


class UserImportRow(ImportMetadataModel):
    class Action(models.TextChoices):
        UNKNOWN = 'UNKNOWN'
        CREATE = 'CREATE'
        UPDATE = 'UPDATE'

    class Status(models.TextChoices):
        PENDING = 'PENDING'
        VALID = 'VALID'
        INVALID = 'INVALID'
        SUCCESS = 'SUCCESS'
        NOT_APPLIED = 'NOT_APPLIED'

    batch = models.ForeignKey(UserImportBatch, on_delete=models.CASCADE, related_name='rows')
    row_number = models.PositiveIntegerField()
    line_number = models.PositiveIntegerField()
    username = models.CharField(max_length=150)
    action = models.CharField(max_length=8, choices=Action.choices, default=Action.UNKNOWN)
    target = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='user_import_rows')
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    input_data = models.JSONField(default=dict, validators=[validate_import_metadata])
    baseline = models.JSONField(default=dict, blank=True, validators=[validate_import_metadata])
    preview = models.JSONField(default=dict, blank=True, validators=[validate_import_metadata])
    errors = models.JSONField(default=list, blank=True, validators=[validate_import_metadata])
    result = models.JSONField(default=dict, blank=True, validators=[validate_import_metadata])
    password_ciphertext = models.TextField(blank=True, editable=False)
    password_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    prepared_password_ciphertext = models.TextField(blank=True, editable=False)
    prepared_password_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ('view',)
        ordering = ['row_number']
        constraints = [
            models.UniqueConstraint(fields=['batch', 'row_number'], name='user_import_row_number'),
            models.CheckConstraint(check=models.Q(row_number__gte=1, line_number__gte=2),
                                   name='user_import_row_source'),
            models.CheckConstraint(check=models.Q(password_ciphertext='') |
                                   models.Q(prepared_password_ciphertext=''), name='user_import_one_secret'),
        ]


class UserImportOperation(models.Model):
    class Action(models.TextChoices):
        UPLOAD = 'upload'
        SUBMIT = 'submit'

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              related_name='user_import_operations')
    action = models.CharField(max_length=8, choices=Action.choices)
    idempotency_key = models.UUIDField(editable=False)
    request_digest = models.CharField(max_length=96, editable=False)
    batch = models.ForeignKey(UserImportBatch, on_delete=models.PROTECT, null=True, editable=False,
                              related_name='operations')
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        default_permissions = ('view',)
        constraints = [models.UniqueConstraint(fields=['actor', 'action', 'idempotency_key'],
                                               name='user_import_operation_key')]
