import contextlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

import penstock
import office_oauth as oauth
from managed_access import token
from managed_oidc import portable_hash


ORIGIN = 'https://penstock.example.org'
HOST = urlsplit(ORIGIN).netloc
CLIENT = 'registered-web-client.apps.googleusercontent.com'
TENANT = '104fcb9a-6902-4384-afe2-d2935376e3e1'
MICROSOFT_CLIENT = 'acc26fea-0077-41c4-9017-e5a3f56451ac'
ACTOR = 'owner-test-actor'
GOOGLE = {'oauth_origin':ORIGIN, 'oauth_client_id':CLIENT, 'oauth_client_secret_env':'PENSTOCK_TEST_GOOGLE_SECRET'}
MICROSOFT = {**GOOGLE, 'oauth_client_id':MICROSOFT_CLIENT, 'oauth_tenant':TENANT}


@unittest.skipUnless(importlib.util.find_spec('cryptography'), 'Install optional office OAuth dependencies.')
class OfficeSignIn(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'PENSTOCK_TEST_GOOGLE_SECRET':'fake-test-secret'}))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        penstock.initialize(self.root)
        self.space = penstock.Workspace(self.root, penstock.Connections(self.root))
        self.project = self.space.create_project('Exec brief')['id']
        self.other = self.space.create_project('Engineering')['id']

    def begin(self, kind, cfg=GOOGLE, **overrides):
        start = oauth.begin(self.space, self.project, kind, ACTOR, cfg)
        url = urlsplit(start['url'])
        fields = parse_qs(url.query)
        query = {'code':['fixture-auth-code'], 'state':fields['state'], **overrides}
        return start['_browser'], query, fields, url

    def grant(self, kind, **fields):
        return {'token_type':'Bearer', 'expires_in':3600, 'access_token':'fixture-access',
                'refresh_token':'fixture-refresh', 'scope':oauth.SCOPES[kind], **fields}

    def test_google_scope_pkce_one_time_project_actor_and_encryption(self):
        kind = 'google-mail'
        browser, query, fields, url = self.begin(kind)
        self.assertEqual((url.scheme,url.netloc,url.path), ('https','accounts.google.com','/o/oauth2/v2/auth'))
        self.assertEqual(fields['scope'], [oauth.SCOPES[kind]])
        self.assertEqual(fields['access_type'], ['offline'])
        self.assertEqual(fields['include_granted_scopes'], ['false'])
        self.assertEqual(fields['code_challenge_method'], ['S256'])
        for bad_kind, bad_browser, bad_host in [
            (kind,token(),HOST), (kind,browser,'other.example.org'), ('google-drive',browser,HOST),
        ]:
            with self.assertRaises(PermissionError):
                oauth.callback(self.space,bad_kind,query,bad_browser,bad_host)
        with self.assertRaises(PermissionError):
            oauth.callback(self.space,kind,{**query,'iss':['https://wrong.example.org']},browser,HOST)
        ticket = oauth.callback(self.space,kind,query,browser,HOST)
        self.assertNotEqual(ticket, query['state'][0])
        with self.assertRaises(PermissionError):
            oauth.callback(self.space,kind,query,browser,HOST)
        with self.assertRaises(PermissionError):
            oauth.context(self.space,ticket,'different-admin',browser,HOST)
        self.assertEqual(oauth.context(self.space,ticket,ACTOR,browser,HOST)['project'],self.project)

        def exchange(app, body):
            self.assertEqual(app.token_url, 'https://oauth2.googleapis.com/token')
            self.assertEqual(body['grant_type'], 'authorization_code')
            self.assertEqual(body['code'], query['code'][0])
            self.assertEqual(portable_hash(body['code_verifier']), fields['code_challenge'][0])
            self.assertEqual(body['redirect_uri'], ORIGIN+'/api/office-oauth/callback/'+kind)
            return self.grant(kind)

        with patch('office_oauth.token_request', side_effect=exchange) as provider:
            with self.assertRaises(PermissionError):
                oauth.finish(self.space,self.other,kind,ACTOR,GOOGLE,None,ticket,browser=browser)
            provider.assert_not_called()
            self.assertEqual(oauth.finish(self.space,self.project,kind,ACTOR,GOOGLE,None,ticket,browser=browser), {'connected':True})
            provider.assert_called_once()
            with self.assertRaises(ValueError):
                oauth.finish(self.space,self.project,kind,ACTOR,GOOGLE,None,ticket,browser=browser)
        records = self.space.file.read_bytes()
        self.assertNotIn(b'fixture-access', records)
        self.assertNotIn(b'fixture-refresh', records)
        key = (self.root/'.office-oauth.key')
        self.assertEqual(len(key.read_bytes()),32)
        if os.name != 'nt':
            self.assertEqual(key.stat().st_mode & 0o777,0o600)
        self.assertTrue(oauth.status(self.space,self.project,kind,GOOGLE))
        self.assertFalse(oauth.status(self.space,self.other,kind,GOOGLE))
        self.assertFalse(oauth.status(self.space,self.project,kind,{**GOOGLE,'oauth_client_id':'changed'}))
        self.assertIsNone(oauth.access_token(self.space,self.other,kind,GOOGLE))
        self.assertEqual(oauth.access_token(self.space,self.project,kind,GOOGLE),'fixture-access')

    def test_microsoft_requires_fixed_tenant_renews_and_preserves_rotated_token(self):
        kind = 'microsoft-mail'
        browser, query, fields, url = self.begin(kind, MICROSOFT)
        self.assertEqual(url.netloc,'login.microsoftonline.com')
        self.assertEqual(url.path,f'/{TENANT}/oauth2/v2.0/authorize')
        self.assertEqual(fields['scope'],['Mail.ReadBasic offline_access'])
        self.assertEqual(fields['response_mode'],['query'])
        self.assertFalse(oauth.configured(kind,{**MICROSOFT,'oauth_tenant':'common'}))
        ticket = oauth.callback(self.space,kind,query,browser,HOST)
        issued = self.grant(kind,scope='https://graph.microsoft.com/Mail.ReadBasic',expires_in=10)
        renewed = self.grant(kind,scope='Mail.ReadBasic',access_token='fixture-renewed',refresh_token='fixture-rotated')
        with patch('office_oauth.token_request',side_effect=[issued,renewed,oauth.TokenError(expired=True)]) as provider:
            oauth.finish(self.space,self.project,kind,ACTOR,MICROSOFT,None,ticket,browser=browser)
            self.assertEqual(oauth.access_token(self.space,self.project,kind,MICROSOFT),'fixture-renewed')
            args,_ = provider.call_args
            self.assertEqual(args[0].token_url,f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token')
            self.assertEqual(args[1],{'grant_type':'refresh_token','refresh_token':'fixture-refresh'})
            with self.space.db() as db:
                db.execute('UPDATE office_oauth_tokens SET expires=0 WHERE project=? AND kind=?',(self.project,kind))
            with self.assertRaisesRegex(ValueError,'expired'):
                oauth.access_token(self.space,self.project,kind,MICROSOFT)
            self.assertEqual(provider.call_args.args[1]['refresh_token'],'fixture-rotated')
        self.assertFalse(oauth.status(self.space,self.project,kind,MICROSOFT))

    def test_cancel_scope_drift_and_revoke_during_exchange_cannot_connect(self):
        kind = 'google-drive'
        browser, query, _, _ = self.begin(kind)
        ticket = oauth.callback(self.space,kind,{'state':query['state'],'error':['access_denied']},browser,HOST)
        with patch('office_oauth.token_request') as provider:
            with self.assertRaisesRegex(ValueError,'canceled'):
                oauth.finish(self.space,self.project,kind,ACTOR,GOOGLE,None,ticket,browser=browser)
            provider.assert_not_called()
        browser, query, _, _ = self.begin(kind)
        ticket = oauth.callback(self.space,kind,query,browser,HOST)
        with patch('office_oauth.token_request',return_value=self.grant(kind,scope=oauth.SCOPES[kind]+' https://www.googleapis.com/auth/drive')):
            with self.assertRaisesRegex(PermissionError,'scope'):
                oauth.finish(self.space,self.project,kind,ACTOR,GOOGLE,None,ticket,browser=browser)
        self.assertFalse(oauth.status(self.space,self.project,kind))
        browser, query, _, _ = self.begin(kind)
        ticket = oauth.callback(self.space,kind,query,browser,HOST)
        def revoke_while_exchange(*_):
            oauth.revoke(self.space,self.project,kind)
            return self.grant(kind)
        with patch('office_oauth.token_request',side_effect=revoke_while_exchange):
            with self.assertRaisesRegex(PermissionError,'changed'):
                oauth.finish(self.space,self.project,kind,ACTOR,GOOGLE,None,ticket,browser=browser)
        self.assertFalse(oauth.status(self.space,self.project,kind))

    def test_https_callback_can_resume_after_app_signin_without_exposing_code(self):
        kind = 'google-drive'
        (self.root/'operations.toml').write_text(f'''[projects."{self.project}".{kind}]
oauth_origin = "{ORIGIN}"
oauth_client_id = "{CLIENT}"
oauth_client_secret_env = "PENSTOCK_TEST_GOOGLE_SECRET"
''')
        owner_key = (self.root/'owner.key').read_text().strip()
        owner = {'Authorization':f'Bearer {owner_key}'}
        listener = self.stack.enter_context(penstock.ThreadingHTTPServer(('127.0.0.1',0), penstock.make_handler(self.space,owner_key)))
        worker = threading.Thread(target=listener.serve_forever, kwargs={'poll_interval':0.01})
        worker.start()
        self.stack.callback(worker.join)
        self.stack.callback(listener.shutdown)

        def request(path, body=None, **headers):
            headers.setdefault('Host', HOST)
            if body is not None:
                headers.setdefault('Origin', ORIGIN)
                headers['Content-Type'] = 'application/json'
            with contextlib.closing(http.client.HTTPConnection('127.0.0.1',listener.server_port,timeout=5)) as client:
                client.request('GET' if body is None else 'POST',path,body=json.dumps(body) if body is not None else None,headers=headers)
                response = client.getresponse()
                raw = response.read()
                return response.status, json.loads(raw) if raw else None, dict(response.getheaders())
        payload = {'project':self.project,'kind':kind,'consent':True}
        self.assertEqual(request('/api/ops-oauth-start',{**payload,'consent':False},**owner)[0],403)
        status, start, headers = request('/api/ops-oauth-start',payload,**owner)
        self.assertEqual(status,200,start)
        self.assertEqual(set(start),{'url'})
        cookie = headers['Set-Cookie']
        self.assertTrue(cookie.startswith(oauth.COOKIE+'='))
        self.assertIn('HttpOnly; SameSite=Lax; Secure',cookie)
        browser = {'Cookie':cookie.split(';',1)[0]}
        state = parse_qs(urlsplit(start['url']).query)['state'][0]
        path = f'/api/office-oauth/callback/{kind}?'+urlencode({'state':state,'code':'fixture-auth-code'})
        status, _, redirected = request(path,**browser)
        self.assertEqual(status,303)
        self.assertNotIn('fixture-auth-code',redirected['Location'])
        self.assertNotIn(state,redirected['Location'])
        ticket = parse_qs(urlsplit(redirected['Location']).fragment)['office-oauth'][0]
        self.assertEqual(request('/api/ops-oauth-finish',{'ticket':ticket},**browser)[0],401)
        with patch('office_oauth.token_request',return_value=self.grant(kind)) as provider:
            self.assertEqual(request('/api/ops-oauth-finish',{'ticket':ticket},**owner)[0],403)
            provider.assert_not_called()
            status, complete, _ = request('/api/ops-oauth-finish',{'ticket':ticket},**owner,**browser)
            self.assertEqual(status,200,complete)
            self.assertTrue(complete['connected'])
            self.assertEqual(complete['project'],self.project)
            provider.assert_called_once()
        self.assertEqual(request('/api/ops-oauth-finish',{'ticket':ticket},**owner,**browser)[0],400)


if __name__ == '__main__':
    unittest.main()
