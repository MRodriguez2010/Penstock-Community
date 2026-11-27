import contextlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from uuid import uuid4

import penstock
from managed_access import ManagedAccess, configure, fingerprint, token
from managed_http import DesktopSession
from managed_oidc import portable_hash


class HttpManagedAccess(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        penstock.initialize(root)
        self.root = root
        self.workspace = penstock.Workspace(root, penstock.Connections(root))
        self.key = (root / 'owner.key').read_text().strip()
        self.owner = {'Authorization': f'Bearer {self.key}'}
        self.first, self.second = [self.workspace.create_project(name)['id'] for name in ('Executive', 'Finance')]
        plan = json.loads((penstock.ROOT / 'examples/review.json').read_text())
        self.versions = [self.workspace.save(plan, project=project) for project in (self.first, self.second)]
        # Real listener and origin selected together so Origin/Host checks are exercised.
        self.http = self.stack.enter_context(penstock.ThreadingHTTPServer(('127.0.0.1', 0), penstock.BaseHTTPRequestHandler))
        self.origin = f'http://127.0.0.1:{self.http.server_port}'
        configure(root, self.origin)
        self.managed = ManagedAccess(root)
        self.bootstrap = token()
        self.desktop = DesktopSession(self.bootstrap)
        self.downloads = root / 'Downloads'
        self.http.RequestHandlerClass = penstock.make_handler(self.workspace, self.key, self.managed, self.desktop,
                                                             downloads=self.downloads)
        worker = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        self.stack.callback(worker.join)
        self.stack.callback(self.http.shutdown)

    def request(self, path, data=None, headers=None):
        headers = dict(headers or {})
        body = None
        if data is not None:
            headers['Content-Type'] = 'application/json'
            headers.setdefault('Origin', self.origin)
            body = json.dumps(data)
        with contextlib.closing(http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)) as client:
            client.request('GET' if body is None else 'POST', path, body=body, headers=headers)
            response = client.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, dict(response.getheaders())

    def invite(self, role='reviewer', email='review@example.org'):
        status, result, _ = self.request('/api/managed/invite',
                                         {'project': self.first, 'email': email, 'role': role, 'method': 'local'}, self.owner)
        self.assertEqual(status, 200, result)
        raw = parse_qs(urlsplit(result['url']).fragment)['invite'][0]
        return raw

    def accept(self, raw):
        status, result, headers = self.request('/api/auth/accept', {'token': raw, 'password': 'a long temporary password'})
        self.assertEqual(status, 200, result)
        session = {'Cookie': headers['Set-Cookie'].split(';', 1)[0]}
        auth = self.request('/api/auth/status', headers=session)[1]
        session['X-CSRF-Token'] = auth['csrf']
        return session, auth['user']

    def start(self, index, header, mode='offline', **extra):
        version = self.versions[index]
        return self.request('/api/start', {'workflow': version['workflow'], 'version': version['version'],
                                           'mode': mode, 'input': 'urgent', 'request': token(), **extra}, header)

    def test_invitation_project_scope_review_and_revocation(self):
        self.assertEqual(self.request('/api/workspace')[0], 401)
        self.assertEqual(self.request('/api/workspace', headers=self.owner)[0], 200)
        raw = self.invite()
        session, identity = self.accept(raw)
        self.assertEqual(self.request('/api/auth/accept', {'token': raw, 'password': 'a long temporary password'})[0], 403)
        with self.managed.db() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM sessions WHERE hash=?', (session['Cookie'].split('=',1)[1],)).fetchone())
            self.assertIsNotNone(db.execute('SELECT 1 FROM sessions WHERE hash=?',
                                            (fingerprint(session['Cookie'].split('=',1)[1]),)).fetchone())
        status, catalog, _ = self.request('/api/workspace', headers=session)
        self.assertEqual(status, 200)
        self.assertEqual([project['id'] for project in catalog['projects']], [self.first])
        self.assertEqual(catalog['managed']['role'], 'reviewer')
        self.assertEqual(self.request('/api/project', {'project': self.second}, session)[0], 403)
        self.assertEqual(self.request('/api/version', {'workflow': self.versions[1]['workflow'], 'version': 1, 'project': self.first}, session)[0], 403)
        self.assertEqual(self.request('/api/managed/members', {'project': self.first}, session)[0], 403)
        self.assertEqual(self.request('/api/starter-save', {'project': self.first}, session)[0], 403)
        self.assertEqual(self.request('/api/starter-catalog', {'project': self.second}, session)[0], 403)
        self.assertEqual(self.request('/api/ops-catalog', {'project': self.second}, session)[0], 403)
        self.assertEqual(self.start(0, session)[0], 403)
        _, pending, _ = self.start(0, self.owner)
        _, outside, _ = self.start(1, self.owner)
        self.assertEqual(self.request('/api/run', {'id': outside['id'], 'project': self.first}, session)[0], 403)
        data = {'id': pending['id'], 'node': 'review', 'approve': True, 'actor': {'id': 'made-up'}}
        self.assertEqual(self.request('/api/decide', data, {'Cookie': session['Cookie']})[0], 403)
        self.assertEqual(self.request('/api/decide', data, {**session, 'Origin': 'https://other.example.org'})[0], 403)
        status, receipt, _ = self.request('/api/decide', data, session)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt['state'], 'finished')
        actor = receipt['decisions'][0]['actor']
        self.assertEqual(actor, {'id': identity['id'], 'displayName': 'review@example.org', 'method': 'local', 'role': 'reviewer'})
        self.assertEqual(receipt['history'][2]['decisionBy'], actor)
        self.assertEqual(self.request('/api/decide', data, session)[0], 409)
        self.assertEqual(self.request('/api/managed/revoke', {'project': self.first, 'id': identity['id']}, self.owner)[0], 200)
        self.assertEqual(self.request('/api/workspace', headers=session)[0], 401)
        reopened = penstock.Workspace(self.root, self.workspace.connections)
        self.assertEqual(reopened.run(receipt['id'])['decisions'][0]['actor'], actor)

    def test_model_consent_and_offline_execution(self):
        self.workspace.connections.providers['test'] = {'protocol':'ollama', 'model':'test', 'url':'http://127.0.0.1:42'}
        self.versions[0] = self.workspace.save({'name': 'Model-assisted summary', 'steps': [
            {'id':'start','type':'start','next':'model'},
            {'id':'model','type':'agent','config':{'provider':'test','instructions':'Summarize'},'next':'end'},
            {'id':'end','type':'finish'},
        ]}, project=self.first)
        session, _ = self.accept(self.invite('editor', 'editor@example.org'))
        with patch.object(self.workspace.connections, 'complete', return_value='summary') as model:
            status, result, _ = self.start(0, session, 'offline', modelConsent=True)
            self.assertEqual(status, 200, result)
            self.assertEqual(result['state'], 'failed')
            model.assert_not_called()
            status, result, _ = self.start(0, session, 'test')
            self.assertEqual((status, result['state']), (200, 'failed'))
            model.assert_not_called()
            status, result, _ = self.start(0, session, 'test', modelConsent=True)
            self.assertEqual((status, result['state']), (200, 'finished'))
            model.assert_called_once()
            body = {'project':self.first, 'provider':'test', 'brief':'Summarize requests'}
            self.assertEqual(self.request('/api/propose', body, session)[0], 403)
            model.assert_called_once()
        self.assertEqual(self.start(0, session, 'live', modelConsent=True)[0], 403)

    def test_local_sign_in_rotates_and_revokes_the_browser_session(self):
        previous, user = self.accept(self.invite())
        form = {'email':'review@example.org', 'password':'a long temporary password'}
        self.assertEqual(self.request('/api/auth/login', {**form,'password':'incorrect'})[0],403)
        status, logged_in, headers = self.request('/api/auth/login', form, previous)
        self.assertEqual(status,200,logged_in)
        self.assertEqual(self.request('/api/workspace', headers=previous)[0],401)
        session = {'Cookie':headers['Set-Cookie'].split(';',1)[0]}
        identity = self.request('/api/auth/status', headers=session)[1]
        self.assertEqual(identity['user']['id'],user['id'])
        self.assertNotEqual(identity['csrf'],previous['X-CSRF-Token'])
        session['X-CSRF-Token'] = identity['csrf']
        self.assertEqual(self.request('/api/auth/logout', {}, session)[0],200)
        self.assertEqual(self.request('/api/workspace', headers=session)[0],401)

    def test_desktop_bootstrap_and_session(self):
        path = '/desktop/bootstrap/' + self.bootstrap
        self.assertEqual(self.request(path, headers={'Host':'local.example.org'})[0], 404)
        status, _, headers = self.request(path)
        self.assertEqual(status, 303)
        self.assertEqual(headers['Location'], '/')
        self.assertIn('HttpOnly; SameSite=Strict', headers['Set-Cookie'])
        self.assertNotIn('; Secure', headers['Set-Cookie'])
        self.assertTrue(headers['Set-Cookie'].startswith(f'penstock_desktop_{self.http.server_port}_'))
        self.assertNotIn(self.bootstrap, headers['Set-Cookie'])
        self.assertEqual(self.request(path)[0], 404)
        cookie = headers['Set-Cookie'].split(';', 1)[0]
        session = {'Cookie': cookie}
        self.assertEqual(self.request('/api/auth/status', headers={**session, 'Sec-Fetch-Site':'same-site'})[0], 404)
        result = self.request('/api/auth/status', headers=session)[1]
        self.assertEqual(result['user']['method'], 'desktop')
        self.assertEqual(self.request('/api/project-create', {'name':'Desktop project'}, session)[0], 403)
        session['X-CSRF-Token'] = result['csrf']
        self.assertEqual(self.request('/api/project-create', {'name':'Desktop project'}, session)[0], 200)
        self.assertEqual(self.request('/api/auth/logout', {}, session)[0], 200)
        self.assertEqual(self.request('/api/workspace', headers=session)[0], 401)

    def test_desktop_brief_copy_requires_approved_project_receipt_and_preserves_collisions(self):
        endpoint = '/api/desktop/save-brief-copy'
        body = {'project': self.first, 'run': str(uuid4())}
        self.assertEqual(self.request(endpoint, body)[0], 401)
        self.assertEqual(self.request(endpoint, body, self.owner)[0], 404)
        _, _, headers = self.request('/desktop/bootstrap/' + self.bootstrap)
        session = {'Cookie': headers['Set-Cookie'].split(';', 1)[0]}
        self.assertEqual(self.request(endpoint, body, session)[0], 403)
        session['X-CSRF-Token'] = self.request('/api/auth/status', headers=session)[1]['csrf']
        catalog = self.request('/api/starter-catalog', {'project': self.first}, session)[1]
        brief = next(starter for starter in catalog['starters'] if starter['kind'] == 'weekly-brief')
        status, run, _ = self.request('/api/starter-run', {
            'project': self.first, 'kind': brief['kind'], 'version': brief['version'], 'source': 'sample',
            'input': brief['sample'], 'request': str(uuid4()),
        }, session)
        self.assertEqual(status, 200, run)
        body['run'] = run['id']
        self.assertEqual(self.request(endpoint, body, session)[0], 400)
        self.assertFalse(self.downloads.exists())
        status, decision, _ = self.request('/api/starter-decide', {
            'project': self.first, 'id': run['id'], 'approve': True,
        }, session)
        self.assertEqual(status, 200, decision)
        self.assertEqual(self.request(endpoint, {**body, 'project': self.second}, session)[0], 400)
        self.assertEqual(self.request(endpoint, {**body, 'path': '../other.md'}, session)[0], 400)
        self.assertEqual(self.request(endpoint, body, {**session, 'Sec-Fetch-Site': 'cross-site'})[0], 403)
        file = self.workspace.data.action(self.first, 'file-read', {'id': decision['effect']['id']})
        name = file['name']
        self.downloads.mkdir()
        matching_name = self.downloads / name
        matching_name.write_text('Existing user file.')
        first_status, first, _ = self.request(endpoint, body, session)
        second_status, second, _ = self.request(endpoint, body, session)
        self.assertEqual((first_status, second_status), (200, 200))
        self.assertEqual(first['name'], name[:-3] + ' (1).md')
        self.assertEqual(second['name'], name[:-3] + ' (2).md')
        self.assertEqual(matching_name.read_text(), 'Existing user file.')
        for result in (first, second):
            self.assertEqual((self.downloads / result['name']).read_bytes(), file['text'].encode())

    def test_live_service_read_and_write_require_distinct_owner_consent(self):
        connections = self.workspace.connections
        operator, _ = self.accept(self.invite('operator', 'operator@example.org'))
        for method, rejected, permitted in [('GET', 'liveWriteConsent', 'liveReadConsent'),
                                             ('POST', 'liveReadConsent', 'liveWriteConsent')]:
            with self.subTest(method=method), patch('blocks.deliver', return_value={'done': True}) as network:
                connections.services['sample'] = {'protocol':'http', 'method':method, 'url':'https://adapter.example.org'}
                self.versions[0] = self.workspace.save({'name': 'Registered operation', 'steps': [
                    {'id':'start','type':'start','next':'connector'},
                    {'id':'connector','type':'http','config':{'service':'sample','preview':{'done':False}},'next':'end'},
                    {'id':'end','type':'finish'},
                ]}, project=self.first)
                _, preview, _ = self.start(0, operator)
                self.assertEqual(preview['state'], 'waiting')
                self.assertTrue(preview['context']['pending']['preview'])
                review = self.request('/api/decide', {'id':preview['id'], 'node':'connector', 'approve':True}, operator)[1]
                self.assertEqual(review['context']['result'], {'done':False})
                self.workspace.publish(self.versions[0]['workflow'], 1)
                _, denied, _ = self.start(0, self.owner, 'live', **{rejected:True})
                self.assertEqual(denied['state'], 'failed')
                _, denied, _ = self.start(0, operator, 'live', **{permitted:True})
                self.assertEqual(denied['state'], 'failed')
                network.assert_not_called()
                _, approved, _ = self.start(0, self.owner, 'live', **{permitted:True})
                self.assertEqual(approved['state'], 'waiting')
                self.assertFalse(approved['context']['pending']['preview'])
                actual = self.request('/api/decide', {'id':approved['id'], 'node':'connector', 'approve':True}, operator)[1]
                self.assertEqual(actual['context']['result'], {'done':True})
                network.assert_called_once()


class OIDCFlow(unittest.TestCase):
    def test_signed_oidc_exchange_replay_and_claim_checks(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest('Install optional OIDC requirements to run cryptographic verification.')
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        jwk.update(kid='primary', use='sig', alg='RS256')
        issuer = 'https://id.example.org'
        client_id = 'penstock-tests'
        challenge = None
        signed = None

        def fake_provider(url, payload=None, headers=None):
            if url.endswith('openid-configuration'):
                return {'issuer':issuer, 'authorization_endpoint':issuer+'/login', 'token_endpoint':issuer+'/token',
                        'jwks_uri':issuer+'/keys', 'response_types_supported':['code'],
                        'id_token_signing_alg_values_supported':['RS256'], 'token_endpoint_auth_methods_supported':['none'],
                        'code_challenge_methods_supported':['S256']}
            if url.endswith('/keys'):
                return {'keys':[jwk]}
            self.assertEqual(payload['grant_type'], 'authorization_code')
            self.assertEqual(payload['client_id'], client_id)
            self.assertEqual(portable_hash(payload['code_verifier']), challenge)
            return {'id_token':signed}

        with tempfile.TemporaryDirectory() as root, patch('managed_oidc.json_request', side_effect=fake_provider):
            origin = 'https://penstock.example.org'
            configure(root, origin, issuer, client_id)
            store = ManagedAccess(root)
            invite = store.invite('first', 'executive@example.org', 'reviewer', 'oidc', {'id':'admin'})
            raw = parse_qs(urlsplit(invite['url']).fragment)['invite'][0]
            location, browser = store.begin_oidc(raw)
            params = parse_qs(urlsplit(location).query)
            challenge = params['code_challenge'][0]
            nonce = params['nonce'][0]
            self.assertEqual(params['redirect_uri'], [origin+'/api/auth/oidc/callback'])
            self.assertEqual(params['code_challenge_method'], ['S256'])
            claims = {'sub':'stable-subject','iss':issuer,'aud':client_id,'iat':int(time.time()),
                      'exp':int(time.time())+60,'nonce':nonce,'email_verified':True,'email':'executive@example.org'}
            signed = jwt.encode(claims, private, algorithm='RS256', headers={'kid':'primary'})
            response = {'state':params['state'], 'code':['issuer-code']}
            with self.assertRaises(PermissionError):
                store.finish_oidc(response, token())
            session = store.finish_oidc(response, browser)
            identity = store.session(session)
            self.assertEqual(identity['email'], 'executive@example.org')
            self.assertEqual(store.role(identity['id'], 'first'), 'reviewer')
            with self.assertRaises(PermissionError):
                store.finish_oidc(response, browser)
            for extra in ({'iss':'https://wrong.example.org'}, {'aud':'other-client'}, {'nonce':token()},
                          {'exp':int(time.time())-300}, {'aud':[client_id,'other'], 'azp':'other'}, {'sub':None}):
                with self.subTest(extra=extra), self.assertRaises(PermissionError):
                    store.oidc.validate(jwt.encode({**claims,**extra}, private, algorithm='RS256', headers={'kid':'primary'}), nonce)
            forged = jwt.encode(claims, 'an unauthorized symmetric key for tests', algorithm='HS256', headers={'kid':'primary'})
            with self.assertRaises(PermissionError):
                store.oidc.validate(forged, nonce)


if __name__ == '__main__':
    unittest.main()
