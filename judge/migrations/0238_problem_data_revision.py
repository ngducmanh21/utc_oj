import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0237_test_data_upload')]

    operations = [
        migrations.CreateModel(
            name='ProblemDataRevision',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[(s, s) for s in ('PREPARED', 'APPLYING', 'APPLIED', 'FAILED')],
                                            db_index=True, default='PREPARED', max_length=16)),
                ('previous_data', models.JSONField(default=dict)),
                ('data', models.JSONField(default=dict)),
                ('cases', models.JSONField(default=list)),
                ('previous_init', models.TextField(null=True)),
                ('init', models.TextField()),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('applied_at', models.DateTimeField(blank=True, null=True)),
                ('problem', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='data_revisions', to='judge.problem')),
                ('session', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                                 related_name='revision', to='judge.problemdataeditsession')),
                ('upload', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                             to='judge.testdataupload')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
