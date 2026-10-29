import csv
import io
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from judge.models import Language, Organization, Profile
from judge.utils.user_import import CSV_FIELDS, parse_user_csv, read_import_csv, validate_import_accounts


def csv_bytes(rows=(), headers=CSV_FIELDS):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode()


class UserImportCSVTests(SimpleTestCase):
    def test_sample_header_does_not_create_a_description_account(self):
        parsed = parse_user_csv(csv_bytes())
        self.assertEqual(parsed.rows, ())
        self.assertEqual([issue.code for issue in parsed.issues], ['empty_csv'])

    def test_bom_quotes_unicode_and_multiline_cells(self):
        data = csv_bytes([['Nguyễn_An', '  Secret,\n"password"  ', 'An, Nguyễn', 'UTC\nSchool',
                           'an@example.org', 'a&a& b ', 'true']])
        parsed = parse_user_csv(b'\xef\xbb\xbf' + data)
        self.assertFalse(parsed.issues)
        row = parsed.rows[0]
        self.assertEqual(row.username, 'Nguyễn_An')
        self.assertEqual(row.password, '  Secret,\n"password"  ')
        self.assertEqual(row.name, 'An, Nguyễn')
        self.assertEqual(row.school, 'UTC\nSchool')
        self.assertEqual(row.organizations, ('a', 'b'))
        self.assertIs(row.password_rotation, True)
        self.assertEqual(row.line_number, 2)
        self.assertNotIn(row.password, repr(parsed))
        self.assertNotIn('password', row.metadata())

    def test_optional_columns_can_be_omitted_or_reordered(self):
        parsed = parse_user_csv(csv_bytes([['An', 'student', 'false']],
                                          [' NAME ', ' USERNAME ', ' PASSWORD_ROTATION ']))
        self.assertFalse(parsed.issues)
        self.assertEqual(parsed.rows[0].name, 'An')
        self.assertEqual(parsed.rows[0].password, '')
        self.assertIs(parsed.rows[0].password_rotation, False)

    def test_rejects_unknown_duplicate_missing_and_misplaced_headers(self):
        headers = [[], ['username'], ['password_rotation'], ['username', 'username', 'password_rotation'],
                   ['username', 'fullname', 'password_rotation'], ['password_rotation', 'username']]
        for header in headers:
            with self.subTest(header=header):
                parsed = parse_user_csv(csv_bytes(headers=header))
                self.assertEqual(parsed.issues[0].code, 'invalid_header')

    def test_boolean_values_are_strict_and_errors_are_aggregated(self):
        values = ['', 'TRUE', 'False', '1', '0', 'yes', 'no']
        parsed = parse_user_csv(csv_bytes([[f'user_{i}', value] for i, value in enumerate(values)],
                                          ['username', 'password_rotation']))
        self.assertEqual(len(parsed.issues), len(values))
        self.assertEqual({issue.code for issue in parsed.issues}, {'invalid_policy'})
        self.assertEqual([issue.line for issue in parsed.issues], list(range(2, 2 + len(values))))

    def test_boolean_outer_whitespace_and_empty_lines(self):
        data = csv_bytes([[], ['', ''], ['a', ' true '], [], ['b', 'false']],
                         ['username', 'password_rotation'])
        parsed = parse_user_csv(data)
        self.assertFalse(parsed.issues)
        self.assertEqual([row.username for row in parsed.rows], ['a', 'b'])
        self.assertEqual([row.row_number for row in parsed.rows], [1, 2])

    def test_field_validation_does_not_echo_passwords(self):
        data = csv_bytes([['bad name', ' ' * 5, 'x' * 151, 's' * 151, 'bad-email', 'org&&', 'yes'],
                          ['', 'SUPER-SECRET' * 500, '', '', '', '', 'false']])
        parsed = parse_user_csv(data)
        codes = {issue.code for issue in parsed.issues}
        self.assertTrue({'invalid_username', 'invalid_password', 'field_too_long', 'invalid_email',
                         'invalid_organization', 'invalid_policy'} <= codes)
        self.assertNotIn('SUPER-SECRET', repr(parsed))
        self.assertNotIn('SUPER-SECRET', str([issue.as_dict() for issue in parsed.issues]))

    def test_exact_duplicate_marks_every_affected_row(self):
        parsed = parse_user_csv(csv_bytes([['a', 'true'], ['a', 'false'], ['a', 'true']],
                                          ['username', 'password_rotation']))
        self.assertEqual({issue.row for issue in parsed.issues if issue.code == 'duplicate_username'}, {1, 2, 3})

    def test_width_and_quoting_errors_are_not_silently_skipped(self):
        parsed = parse_user_csv(b'username,password_rotation\na\nb,false,extra\nc,true\n')
        self.assertEqual([issue.code for issue in parsed.issues], ['invalid_width', 'invalid_width'])
        self.assertEqual(parsed.rows[0].username, 'c')
        malformed = parse_user_csv(b'username,password_rotation\n"never closed,true')
        self.assertEqual(malformed.issues[0].code, 'malformed_csv')

    def test_invalid_encoding_and_nul_are_rejected_without_raw_content(self):
        for data in (b'\xffpassword', b'username,password_rotation\na\x00,true'):
            with self.subTest(data=data):
                self.assertEqual(parse_user_csv(data).issues[0].code, 'invalid_encoding')

    @override_settings(DMOJ_USER_IMPORT_MAX_ROWS=2)
    def test_row_limit_counts_malformed_records_too(self):
        parsed = parse_user_csv(b'username,password_rotation\na\nb\nc,true\n')
        self.assertEqual([issue.code for issue in parsed.issues],
                         ['invalid_width', 'invalid_width', 'too_many_rows'])

    @override_settings(DMOJ_USER_IMPORT_MAX_BYTES=12)
    def test_byte_limit_is_checked_with_a_bounded_read(self):
        stream = io.BytesIO(b'x' * 100)
        with patch.object(stream, 'read', wraps=stream.read) as reader:
            self.assertEqual(parse_user_csv(stream).issues[0].code, 'file_too_large')
        reader.assert_called_once_with(13)
        with self.assertRaisesRegex(Exception, 'size limit'):
            read_import_csv(b'x' * 13)


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class UserImportAccountValidationTests(TestCase):
    fixtures = ['language_small']

    def setUp(self):
        self.user = User.objects.create_user('existing', password='Old-password-2026', first_name='Old name',
                                             last_name='Old school', email='old@example.org')
        self.profile = Profile.objects.create(user=self.user, language=Language.objects.get(key='CPP17'))
        self.org = Organization.objects.create(name='Existing org', short_name='Org', slug='existing_org')
        self.profile.organizations.add(self.org)

    def validate(self, rows):
        return validate_import_accounts(parse_user_csv(csv_bytes(rows)))

    def test_mixed_new_existing_blank_fields_only_read_accounts(self):
        before = list(User.objects.values())
        profiles = list(Profile.objects.values())
        memberships = list(Profile.organizations.through.objects.values())
        parsed = self.validate([['existing', '', '', '', '', 'existing_org', 'false'],
                                ['new_user', 'A-new-password-2026', 'New', 'UTC', '', '', 'true']])
        self.assertFalse(parsed.issues)
        self.assertEqual([row.target_id for row in parsed.rows], [self.user.pk, None])
        self.assertTrue(all(row.resolved for row in parsed.rows))
        self.assertEqual(list(User.objects.values()), before)
        self.assertEqual(list(Profile.objects.values()), profiles)
        self.assertEqual(list(Profile.organizations.through.objects.values()), memberships)

    def test_new_password_required_errors_for_all_rows(self):
        parsed = self.validate([['a', '', '', '', '', '', 'true'], ['b', '', '', '', '', '', 'false']])
        self.assertEqual([(issue.row, issue.field, issue.code) for issue in parsed.issues],
                         [(1, 'password', 'password_required'), (2, 'password', 'password_required')])
        self.assertEqual(User.objects.count(), 1)

    def test_database_collation_detects_case_duplicate_new_accounts(self):
        parsed = self.validate([['Student', 'pw', '', '', '', '', 'true'],
                                ['student', 'pw', '', '', '', '', 'true']])
        # The local/CI MySQL username column uses a case-insensitive collation.
        with connection.cursor() as cursor:
            cursor.execute('SELECT COLLATION_NAME FROM information_schema.COLUMNS '
                           'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s',
                           [User._meta.db_table, 'username'])
            self.assertTrue(cursor.fetchone()[0].endswith('_ci'))
        self.assertEqual({issue.row for issue in parsed.issues if issue.code == 'duplicate_username'}, {1, 2})

    def test_existing_account_spelling_collision_is_not_an_implicit_update(self):
        parsed = self.validate([['EXISTING', '', '', '', '', '', 'false']])
        self.assertEqual([issue.code for issue in parsed.issues], ['username_conflict'])
        self.assertEqual(parsed.rows[0].target_id, self.user.pk)

    def test_unknown_organizations_are_errors(self):
        parsed = self.validate([['existing', '', '', '', '', 'existing_org&missing', 'false']])
        self.assertEqual([issue.code for issue in parsed.issues], ['unknown_organization'])
        self.assertEqual(self.profile.organizations.count(), 1)

    def test_default_maximum_batch_can_be_resolved_without_account_writes(self):
        data = [[f'bulk_{i}', 'Not-a-real-password-2026', '', '', '', '', 'false'] for i in range(2000)]
        before = User.objects.count()
        parsed = self.validate(data)
        self.assertFalse(parsed.issues)
        self.assertEqual(len(parsed.rows), 2000)
        self.assertTrue(all(row.resolved for row in parsed.rows))
        self.assertEqual(User.objects.count(), before)

    def test_privileged_inactive_and_profileless_targets_are_rejected(self):
        for name, fields in [('staff', {'is_staff': True}), ('root', {'is_superuser': True}),
                             ('inactive', {'is_active': False}), ('profileless', {})]:
            user = User.objects.create_user(name, **fields)
            if name != 'profileless':
                Profile.objects.create(user=user, language=Language.objects.get(key='CPP17'))
        parsed = self.validate([[name, '', '', '', '', '', 'false']
                                for name in ['staff', 'root', 'inactive', 'profileless']])
        self.assertEqual({issue.row for issue in parsed.issues if issue.code == 'ineligible_account'}, {1, 2, 3, 4})

    @override_settings(DEFAULT_USER_LANGUAGE='does_not_exist')
    def test_missing_default_language_blocks_new_accounts_only(self):
        parsed = self.validate([['existing', '', '', '', '', '', 'false'],
                                ['new', 'Password-2026', '', '', '', '', 'false']])
        self.assertEqual([(issue.row, issue.code) for issue in parsed.issues], [(2, 'missing_default_language')])

    def test_email_domain_whitelist_does_not_apply_to_admin_imports(self):
        self.assertEqual(settings.DEFAULT_USER_LANGUAGE, 'CPP17')
        parsed = self.validate([['new', 'Password-2026', '', '', 'new@outside.example', '', 'true']])
        self.assertFalse(parsed.issues)
