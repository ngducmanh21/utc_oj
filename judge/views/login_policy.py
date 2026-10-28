import json
from dataclasses import asdict

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError
from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from judge.models import Profile
from judge.utils.managed_login import LoginPolicyConflict, PENDING_LOGIN_KEY, can_manage_login_policy, \
    discard_login_session, get_login_policy, session_epoch_matches, set_login_policy


def policy_error(code, message, status):
    response = JsonResponse({'error': {'code': code, 'message': str(message)}}, status=status)
    response['Cache-Control'] = 'no-store'
    return response


def policy_session_error(request):
    if request.headers.get('Authorization'):
        return policy_error('session_required', _('Use an administrator session for this operation.'), 403)
    if not request.user.is_authenticated:
        return policy_error('authentication_required', _('Log in as an administrator.'), 401)
    if '_impersonate' in request.session or getattr(request.user, 'is_impersonate', False):
        return policy_error('permission_denied', _('End impersonation before managing login policies.'), 403)
    if not session_epoch_matches(request.session, request.user.profile):
        discard_login_session(request)
        return policy_error('session_expired', _('Your session expired. Log in again.'), 401)
    if not can_manage_login_policy(request.user):
        return policy_error('permission_denied', _('You do not have permission to manage login policies.'), 403)
    profile = request.user.profile
    if (PENDING_LOGIN_KEY in request.session or profile.rotate_password_on_login or
            ((profile.is_totp_enabled or profile.is_webauthn_enabled) and not request.session.get('2fa_passed'))):
        return policy_error('two_factor_required', _('Complete two-factor authentication first.'), 403)
    if request.session.get('password_pwned') and not request.official_contest_mode:
        return policy_error('password_change_required', _('Change your password before managing login policies.'), 403)


@never_cache
@csrf_protect
def login_policy(request, user_id):
    try:
        error = policy_session_error(request)
        if error is not None:
            return error
        if user_id <= 0:
            return policy_error('invalid_user_id', _('Invalid user ID.'), 422)
        if request.method == 'GET':
            state = get_login_policy(request.user, user_id)
        elif request.method == 'PATCH':
            if request.content_type != 'application/json' or len(request.body) > 16384:
                return policy_error('invalid_request', _('Expected a JSON policy object.'), 422)
            try:
                data = json.loads(request.body)
                if (not isinstance(data, dict) or not {'enabled', 'expected_epoch'} <= set(data) or
                        not set(data) <= {'enabled', 'expected_epoch', 'reason'}):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return policy_error('invalid_request', _('Expected a JSON policy object.'), 422)
            state = set_login_policy(request.user, user_id, **data)
        else:
            response = policy_error('method_not_allowed', _('Use GET or PATCH for login policies.'), 405)
            response['Allow'] = 'GET, PATCH'
            return response
        return JsonResponse(asdict(state))
    except LoginPolicyConflict as error:
        return policy_error(error.code, error.message, 409)
    except PermissionDenied:
        return policy_error('permission_denied', _('You do not have permission to manage this login policy.'), 403)
    except (User.DoesNotExist, Profile.DoesNotExist):
        return policy_error('not_found', _('Account not found.'), 404)
    except ValidationError as error:
        return policy_error(error.code or 'invalid_request', error.messages[0], 422)
    except DatabaseError:
        return policy_error('unavailable', _('Unable to save account changes right now. Please try again.'), 503)


login_policy.login_policy_api = True
