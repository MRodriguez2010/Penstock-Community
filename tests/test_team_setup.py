import contextlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import penstock
from managed_access import ManagedAccess, configure, token
from managed_http import DesktopSession


class TeamSetupHttp(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        penstock.initialize(self.root)
        self.workspace = penstock.Workspace(self.root, penstock.Connections(self.root))
        self.key = (self.root / 'owner.key').read_text().strip()
        self.owner = {'Authorization': 'Bearer ' + self.key}
        self.http = stack.enter_context(penstock.ThreadingHTTPServer(('127.0.0.1', 0), penstock.BaseHTTPRequestHandler))
        self.origin = f'http://127.0.0.1:{self.http.server_port}'
        self.handler()
        worker = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        stack.callback(worker.join)
        stack.callback(self.http.shutdown)

    def handler(self, managed=None, desktop=None):
        self.http.RequestHandlerClass = penstock.make_handler(self.workspace, self.key, managed, desktop)

    def request(self, path, data=None, *, headers=None, origin=True, port=None):
        options = dict(headers or {})
        port = port or self.http.server_port
        if data is not None:
            options['Content-Type'] = 'application/json'
            if origin:
                options.setdefault('Origin', f'http://127.0.0.1:{port}')
            body = json.dumps(data)
        else:
            body = None
        with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5)) as client:
            client.request('GET' if body is None else 'POST', path, body=body, headers=options)
            response = client.getresponse()
            raw = response.read()
            data = json.loads(raw) if raw and response.getheader('Content-Type') == 'application/json' else raw
            return response.status, data, dict(response.getheaders())

    def settings(self, **fields):
        return {'action': 'save', 'method': 'local', 'origin': self.origin, **fields}

    def test_requires_owner_key_and_secure_same_origin(self):
        path = '/api/team-setup'
        self.assertEqual(self.request(path, self.settings())[0], 401)
        self.assertEqual(self.request(path, self.settings(), headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request(path, self.settings(), headers=self.owner, origin=False)[0], 403)
        self.assertEqual(self.request(path, self.settings(), headers={**self.owner, 'Origin': 'https://elsewhere.example'})[0], 403)
        self.assertEqual(self.request(path, self.settings(), headers={**self.owner, 'Sec-Fetch-Site': 'same-site'})[0], 403)
        self.assertEqual(self.request(path, self.settings(), headers={**self.owner, 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request(path, self.settings(), headers={**self.owner, 'Host': 'studio.example',
                                                                      'Origin': 'http://studio.example'})[0], 403)
        self.assertEqual(self.request(path, self.settings(origin='http://studio.example'), headers=self.owner)[0], 400)
        self.assertEqual(self.request(path, self.settings(origin='https://studio.example/path'), headers=self.owner)[0], 400)
        self.assertFalse((self.root / 'managed.json').exists())
        self.assertEqual(self.request('/api/workspace', headers=self.owner)[0], 200)

    def test_restart_activates_local_invites_and_preserves_owner_and_project_roles(self):
        path = '/api/team-setup'
        state = {'action': 'status'}
        existing = self.workspace.create_project('Existing project')
        self.assertEqual(self.request(path, state, headers=self.owner)[1]['state'], 'unconfigured')
        status, pending, _ = self.request(path, self.settings(), headers=self.owner)
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending['state'], 'restart-required')
        self.assertEqual(self.request(path, state, headers=self.owner)[1], pending)
        self.assertEqual(self.request(path, self.settings(origin='https://other.example'), headers=self.owner)[1], pending)
        self.assertEqual(self.request('/api/auth/status')[1]['invitations'], False)
        self.assertEqual((self.root / 'owner.key').read_text().strip(), self.key)
        self.assertEqual(self.request('/api/project', {'project': existing['id']}, headers=self.owner)[0], 200)

        self.handler(ManagedAccess(self.root))
        self.assertEqual(self.request('/api/workspace', headers=self.owner)[0], 200)
        self.assertEqual(self.request('/api/auth/status')[1]['invitations'], True)
        self.assertEqual(self.request(path, state, headers=self.owner)[1], {**pending, 'state': 'active'})
        invite = {'project': existing['id'], 'email': 'admin@example.org', 'role': 'admin', 'method': 'local'}
        status, invitation, _ = self.request('/api/managed/invite', invite, headers=self.owner)
        self.assertEqual(status, 200, invitation)
        invite_token = parse_qs(urlsplit(invitation['url']).fragment)['invite'][0]
        status, result, headers = self.request('/api/auth/accept', {'token': invite_token,
                                                                    'password': 'a temporary administrator password'})
        self.assertEqual(status, 200, result)
        session = {'Cookie': headers['Set-Cookie'].split(';', 1)[0]}
        session['X-CSRF-Token'] = self.request('/api/auth/status', headers=session)[1]['csrf']
        self.assertEqual(self.request('/api/managed/members', {'project': existing['id']}, headers=session)[0], 200)
        self.assertEqual(self.request(path, state, headers=session)[0], 403)
        self.assertEqual(self.request(path, self.settings(), headers=session)[0], 403)

    def test_oidc_runtime_validation_saves_only_variable_name_and_makes_no_requests(self):
        path = '/api/team-setup'
        fields = self.settings(method='oidc', origin='https://penstock.example.org/', issuer='https://login.example.org/tenant',
                               clientId='organization-app', keyEnv='TEAM_SETUP_TEST_SECRET')
        self.assertEqual(self.request(path, {**fields, 'secret': 'literal-secret'}, headers=self.owner)[0], 400)
        self.assertEqual(self.request(path, {**fields, 'issuer': 'http://login.example.org'}, headers=self.owner)[0], 400)
        self.assertEqual(self.request(path, {**fields, 'keyEnv': 'invalid name'}, headers=self.owner)[0], 400)
        self.assertFalse((self.root / 'managed.json').exists())
        try:
            import jwt  # noqa: F401
        except ImportError:
            self.assertEqual(self.request(path, fields, headers=self.owner)[0], 400)
            self.skipTest('Optional OIDC runtime dependencies are unavailable.')
        with patch.dict(os.environ, {'TEAM_SETUP_TEST_SECRET': ''}):
            code, error, _ = self.request(path, fields, headers=self.owner)
            self.assertEqual(code, 400, error)
            self.assertIn('Set TEAM_SETUP_TEST_SECRET', error['error'])
            self.assertFalse((self.root / 'managed.json').exists())
        with patch.dict(os.environ, {'TEAM_SETUP_TEST_SECRET': 'server-secret-placeholder'}), \
             patch('managed_oidc.json_request', side_effect=AssertionError('Unexpected provider request')) as outbound:
            code, saved, _ = self.request(path, fields, headers=self.owner)
            self.assertEqual(code, 200, saved)
            self.assertEqual(saved, {'state': 'restart-required', 'origin': 'https://penstock.example.org', 'method': 'oidc',
                                     'redirectUri': 'https://penstock.example.org/api/auth/oidc/callback'})
            self.assertTrue(ManagedAccess(self.root).oidc)
            outbound.assert_not_called()
        stored = (self.root / 'managed.json').read_text()
        self.assertEqual(json.loads(stored)['oidc']['secret_env'], 'TEAM_SETUP_TEST_SECRET')
        self.assertNotIn('server-secret-placeholder', stored)

    def test_desktop_session_cannot_stage_hosted_team_settings(self):
        bootstrap = token()
        self.handler(desktop=DesktopSession(bootstrap))
        status, _, headers = self.request('/desktop/bootstrap/' + bootstrap)
        self.assertEqual(status, 303)
        session = {'Cookie': headers['Set-Cookie'].split(';', 1)[0]}
        session['X-CSRF-Token'] = self.request('/api/auth/status', headers=session)[1]['csrf']
        self.assertEqual(self.request('/api/team-setup', self.settings(), headers=session)[0], 403)
        self.assertEqual(self.request('/api/team-setup', self.settings(), headers=self.owner)[0], 403)
        self.assertFalse((self.root / 'managed.json').exists())

    def test_serve_keeps_owner_access_if_oidc_runtime_fails_at_restart(self):
        configure(self.root, 'https://penstock.example.org', 'https://login.example.org', 'penstock', 'TEAM_SETUP_TEST_SECRET')
        env = {**os.environ, 'TEAM_SETUP_TEST_SECRET': ''}
        env.pop('PENSTOCK_DESKTOP_BOOTSTRAP', None)
        log = self.root / 'setup-test.log'
        with log.open('wb') as output:
            server = subprocess.Popen([sys.executable, str(penstock.ROOT / 'penstock.py'), 'serve', '--workspace',
                                       str(self.root), '--port', '0'], stdout=output, stderr=output, env=env)
            try:
                limit = time.monotonic() + 5
                while time.monotonic() < limit and server.poll() is None:
                    lines = log.read_text().splitlines()
                    address = next((line for line in lines if line.startswith('Penstock listening on ')), None)
                    if address:
                        break
                    time.sleep(0.025)
                else:
                    self.fail(f'Penstock did not start. {log.read_text()}')
                port = int(address.rsplit(':', 1)[-1])
                self.assertEqual(self.request('/api/auth/status', port=port)[1]['invitations'], False)
                self.assertEqual(self.request('/api/workspace', headers=self.owner, port=port)[0], 200)
                code, state, _ = self.request('/api/team-setup', {'action': 'status'}, headers=self.owner, port=port)
                self.assertEqual(code, 200, state)
                self.assertEqual(state['state'], 'restart-required')
                self.assertTrue(state['activationError'])
            finally:
                if server.poll() is None:
                    server.terminate()
                server.wait(timeout=5)
