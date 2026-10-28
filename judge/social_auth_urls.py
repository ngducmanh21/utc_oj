"""Keep social-auth's URL contract while guarding credential management."""

from django.conf import settings
from django.urls import path

from judge import social_auth

extra = '/' if getattr(settings, 'SOCIAL_AUTH_TRAILING_SLASH', True) else ''
app_name = 'social'

urlpatterns = [
    path(f'login/<str:backend>{extra}', social_auth.auth, name='begin'),
    path(f'complete/<str:backend>{extra}', social_auth.complete, name='complete'),
    path(f'disconnect/<str:backend>{extra}', social_auth.disconnect, name='disconnect'),
    path(f'disconnect/<str:backend>/<int:association_id>{extra}',
         social_auth.disconnect, name='disconnect_individual'),
]
