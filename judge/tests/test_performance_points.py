from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import Language, Problem, ProblemGroup, Profile, Submission
from judge.performance_points import PP_WEIGHT_TABLE, get_pp_breakdown


@override_settings(DEFAULT_USER_LANGUAGE='CPP17')
class PerformancePointsTests(TestCase):
    fixtures = ['language_small']

    def setUp(self):
        self.language = Language.objects.get(key='CPP17')
        self.user = User.objects.create_user('pp_student')
        self.profile = Profile.objects.create(user=self.user, language=self.language)
        self.group = ProblemGroup.objects.create(name='pp_group', full_name='PP Group')
        self.now = timezone.now()
        if connection.vendor == 'mysql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT @@SESSION.sql_mode')
                original = cursor.fetchone()[0]
                modes = set(original.split(',')) | {'ONLY_FULL_GROUP_BY'}
                cursor.execute('SET SESSION sql_mode = %s', [','.join(sorted(modes))])
            self.addCleanup(self.restore_sql_mode, original)

    def restore_sql_mode(self, mode):
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION sql_mode = %s', [mode])

    def problem(self, code, **kwargs):
        return Problem.objects.create(code=code, name=code, group=self.group, points=100,
                                      time_limit=1, memory_limit=65536,
                                      is_public=kwargs.pop('is_public', True), **kwargs)

    def submit(self, problem, points, date=None, user=None, **kwargs):
        submission = Submission.objects.create(user=user or self.profile, problem=problem, language=self.language,
                                               points=points, status='D', result='AC', **kwargs)
        if date is not None:
            Submission.objects.filter(pk=submission.pk).update(date=date)
        return submission

    def test_empty_profile_with_full_group_by(self):
        self.assertEqual(get_pp_breakdown(self.profile), ([], False))

    def test_best_score_chooses_latest_submission_then_highest_id(self):
        problem = self.problem('pp_tie')
        self.submit(problem, 80, self.now - timedelta(days=5))
        self.submit(problem, 80, self.now - timedelta(days=2))
        self.submit(problem, 80, self.now - timedelta(days=3))
        winner = self.submit(problem, 80, self.now - timedelta(days=2), case_points=8, case_total=10)
        self.submit(problem, 40, self.now)
        breakdown, has_more = get_pp_breakdown(self.profile)
        self.assertFalse(has_more)
        self.assertEqual(len(breakdown), 1)
        entry = breakdown[0]
        self.assertEqual(entry.points, 80)
        self.assertEqual(entry.sub_id, winner.pk)
        self.assertEqual((entry.sub_points, entry.sub_total), (8, 10))
        self.assertEqual(entry.sub_date, self.now - timedelta(days=2))

    def test_filters_other_users_private_problems_and_nonpositive_scores(self):
        public = self.problem('pp_public')
        own = self.submit(public, 25)
        other = User.objects.create_user('pp_other')
        profile = Profile.objects.create(user=other, language=self.language)
        self.submit(public, 100, user=profile)
        self.submit(self.problem('pp_private', is_public=False), 100)
        self.submit(self.problem('pp_org', is_organization_private=True), 100)
        self.submit(self.problem('pp_zero'), 0)
        self.submit(self.problem('pp_null'), None)
        self.submit(self.problem('pp_negative'), -10)
        breakdown, has_more = get_pp_breakdown(self.profile)
        self.assertFalse(has_more)
        self.assertEqual([(entry.problem_code, entry.points, entry.sub_id) for entry in breakdown],
                         [('pp_public', 25, own.pk)])

    def test_pagination_preserves_order_weights_and_has_more(self):
        self.submit(self.problem('pp_a'), 100, self.now - timedelta(days=2))
        self.submit(self.problem('pp_b'), 100, self.now - timedelta(days=1))
        self.submit(self.problem('pp_c'), 50, self.now)
        first, has_more = get_pp_breakdown(self.profile, 0, 2)
        self.assertTrue(has_more)
        self.assertEqual([entry.problem_code for entry in first], ['pp_b', 'pp_a'])
        self.assertEqual([entry.weight for entry in first], [weight * 100 for weight in PP_WEIGHT_TABLE[:2]])
        second, has_more = get_pp_breakdown(self.profile, 2, 4)
        self.assertFalse(has_more)
        self.assertEqual([entry.problem_code for entry in second], ['pp_c'])
        self.assertAlmostEqual(second[0].scaled_points, 50 * PP_WEIGHT_TABLE[2])

    def test_user_solved_page_and_pp_ajax_accept_empty_profile(self):
        response = self.client.get(reverse('user_problems', args=[self.user.username]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pp_breakdown'], [])
        response = self.client.get(reverse('user_pp_ajax', args=[self.user.username]), {'start': 0, 'end': 10})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['has_more'])

    def test_user_solved_page_shows_best_submission_once(self):
        problem = self.problem('pp_rendered')
        self.submit(problem, 50)
        winner = self.submit(problem, 50)
        response = self.client.get(reverse('user_problems', args=[self.user.username]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([entry.sub_id for entry in response.context['pp_breakdown']], [winner.pk])
