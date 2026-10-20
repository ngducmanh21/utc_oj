import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0238_problem_data_revision'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='profile', name='rotate_password_on_login',
            field=models.BooleanField(default=False, verbose_name='rotate password after login',
                                      help_text='Keep the authenticated session and replace the password after a complete login.'),
        ),
        migrations.AddField(
            model_name='profile', name='account_session_epoch',
            field=models.PositiveBigIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name='profile', name='password_rotated_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AlterModelOptions(
            name='profile',
            options={
                'permissions': (
                    ('test_site', 'Shows in-progress development stuff'),
                    ('totp', 'Edit TOTP settings'),
                    ('can_upload_image', 'Can upload image directly to server via martor'),
                    ('high_problem_timelimit', 'Can set high problem timelimit'),
                    ('long_contest_duration', 'Can set long contest duration'),
                    ('create_mass_testcases', 'Can create unlimitted number of testcases for a problem'),
                    ('ban_user', 'Ban users'),
                    ('manage_login_password_rotation', 'Manage login password rotation'),
                ),
                'verbose_name': 'user profile',
                'verbose_name_plural': 'user profiles',
            },
        ),
        migrations.CreateModel(
            name='ManagedAccountEvent',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('target_username', models.CharField(editable=False, max_length=150)),
                ('event', models.CharField(choices=[
                    ('policy_enabled', 'Policy enabled'),
                    ('policy_disabled', 'Policy disabled'),
                    ('password_rotated', 'Password rotated after login'),
                    ('credential_reissued', 'Password changed by administrator'),
                ], max_length=32)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('reason', models.CharField(blank=True, max_length=255)),
                ('previous_enabled', models.BooleanField()),
                ('enabled', models.BooleanField()),
                ('previous_epoch', models.PositiveBigIntegerField()),
                ('epoch', models.PositiveBigIntegerField()),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                            related_name='managed_account_actions', to=settings.AUTH_USER_MODEL)),
                ('target', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL,
                                             related_name='managed_account_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-pk'],
                'default_permissions': ('view',),
                'indexes': [models.Index(fields=['target', '-created_at'], name='managed_evt_target_time')],
            },
        ),
    ]
