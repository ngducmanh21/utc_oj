import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0236_judge_storages'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ProblemDataEditSession',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('token', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('status', models.CharField(db_index=True, default='ACTIVE', max_length=12)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_activity', models.DateTimeField(default=django.utils.timezone.now)),
                ('expires_at', models.DateTimeField()),
                ('problem', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='data_edit_sessions', to='judge.problem')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='TestDataUpload',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('filename', models.CharField(max_length=255)),
                ('fingerprint', models.CharField(blank=True, max_length=128)),
                ('size', models.PositiveBigIntegerField()),
                ('actual_size', models.PositiveBigIntegerField(default=0)),
                ('reserved_bytes', models.PositiveBigIntegerField(default=0)),
                ('status', models.CharField(db_index=True, default='CREATED', max_length=12)),
                ('entries', models.JSONField(blank=True, default=list)),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_activity', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='uploads', to='judge.problemdataeditsession')),
            ],
        ),
    ]
