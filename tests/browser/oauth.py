"""Loopback OAuth2 protocol fixture; never contact a real identity provider."""

import json
import secrets
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings

from judge.social_auth import GitHubSecureEmailOAuth2


class FixtureGitHubOAuth2(GitHubSecureEmailOAuth2):
    """Keep GitHub's state/code exchange and UTCOJ's verified-email handling."""

    def authorization_url(self):
        return settings.M5_OAUTH_PROVIDER_URL + '/authorize'

    def access_token_url(self):
        return settings.M5_OAUTH_PROVIDER_URL + '/token'

    def api_url(self):
        return settings.M5_OAUTH_PROVIDER_URL + '/api/'


class OAuthProvider:
    def __init__(self, callback_url, email):
        self.callback_url = callback_url
        self.email = email
        self.codes = {}
        self.tokens = set()
        self.token_requests = 0
        self.user_requests = 0
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                # Do not write codes, state or access tokens to test logs.
                pass

            def respond(self, status, body, content_type='application/json', **headers):
                content = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(content)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                url = urlsplit(self.path)
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                if url.path == '/authorize':
                    if (query.get('client_id') != 'm5-client' or query.get('response_type') != 'code' or
                            query.get('redirect_uri') != provider.callback_url or not query.get('state')):
                        return self.respond(400, '{}')
                    code = secrets.token_urlsafe(24)
                    provider.codes[code] = query
                    return self.respond(200, '<!doctype html><title>M5 local OAuth provider</title>'
                                        '<form action="/approve"><input type="hidden" name="code" value="' +
                                        escape(code, quote=True) + '"><button>Approve test identity</button></form>',
                                        'text/html')
                if url.path == '/approve':
                    code = query.get('code')
                    authorization = provider.codes.get(code)
                    if authorization is None:
                        return self.respond(400, '{}')
                    callback = provider.callback_url + '?' + urlencode({
                        'code': code, 'state': authorization['state'],
                    })
                    return self.respond(302, '', Location=callback)
                if url.path in ('/api/user', '/api/user/emails'):
                    if self.headers.get('Authorization', '').removeprefix('token ') not in provider.tokens:
                        return self.respond(401, '{}')
                    provider.user_requests += 1
                    data = ({'id': 424242, 'login': 'm5_identity', 'email': provider.email} if
                            url.path == '/api/user' else
                            [{'email': provider.email, 'primary': True, 'verified': True}])
                    return self.respond(200, json.dumps(data))
                self.respond(404, '{}')

            def do_POST(self):
                if urlsplit(self.path).path != '/token':
                    return self.respond(404, '{}')
                data = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
                data = {key: values[0] for key, values in data.items()}
                if (data.get('client_id') != 'm5-client' or data.get('client_secret') != 'm5-test-secret' or
                        data.get('grant_type') != 'authorization_code' or
                        data.get('redirect_uri') != provider.callback_url):
                    return self.respond(400, '{}')
                if provider.codes.pop(data.get('code'), None) is None:
                    return self.respond(400, '{}')
                provider.token_requests += 1
                token = secrets.token_urlsafe(24)
                provider.tokens.add(token)
                self.respond(200, json.dumps({'access_token': token, 'token_type': 'bearer', 'scope': 'user:email'}))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
