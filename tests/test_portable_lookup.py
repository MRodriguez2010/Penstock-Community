import contextlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import penstock
from managed_access import ManagedAccess, configure, token
from portable_lookup import SAMPLES
from resources import DEFAULT_PROJECT


CASES = [
    ('web-search', 'search', 'project reports', 5),
    ('github', 'repository', 'example/repository', 1),
    ('github', 'issues', 'example/repository', 5),
    ('github', 'pull-requests', 'example/repository', 5),
    ('enrichment', 'person', 'person@example.org', 1),
    ('enrichment', 'company', 'example.org', 1),
    ('service-health', 'service-health', 'example-service', 1),
    ('worker-status', 'worker-status', 'example-worker', 1),
    ('published-workflow', 'published-workflow', 'example-workflow', 1),
    ('weather-observation', 'weather-observation', 'example-station', 1),
]


class TypedLookupHttp(unittest.TestCase):
    def serve(self, handler):
        server = self.stack.enter_context(penstock.ThreadingHTTPServer(('127.0.0.1', 0), handler))
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        self.stack.callback(worker.join)
        self.stack.callback(server.shutdown)
        return server

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {'LOOKUP_ADAPTER_TEST_KEY': 'large-local-adapter-test-value'}))
        self.requests = []
        self.response_override = None
        fixture = self

        class Adapter(penstock.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append((self.path, body, self.headers.get('Authorization')))
                status, headers, body = fixture.response_override or (
                    200, {}, {'items': [{**SAMPLES[body['operation']], 'sourceUrl': 'https://evidence.example.org/item'}]})
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

        self.adapter = self.serve(Adapter)
        penstock.initialize(self.root)
        self.key = {'Authorization': 'Bearer ' + (self.root / 'owner.key').read_text().strip()}
        key = self.key['Authorization'].split()[1]
        configfile = self.root / 'connections.toml'
        with configfile.open('a', encoding='utf-8') as handle:
            for kind in dict.fromkeys(case[0] for case in CASES):
                handle.write(f'\n[lookups.{kind}-adapter]\nkind = "{kind}"\nprojects = ["{DEFAULT_PROJECT}"]\n'
                             f'url = "http://127.0.0.1:{self.adapter.server_port}/{kind}"\nkey_env = "LOOKUP_ADAPTER_TEST_KEY"\n')
        self.connections = penstock.Connections(self.root)
        self.workspace = penstock.Workspace(self.root, self.connections)
        self.other = self.workspace.create_project('Separate project')['id']
        self.http = self.serve(penstock.BaseHTTPRequestHandler)
        self.origin = f'http://127.0.0.1:{self.http.server_port}'
        configure(self.root, self.origin)
        self.http.RequestHandlerClass = penstock.make_handler(self.workspace, key, ManagedAccess(self.root))

    def api(self, path, data=None, *, auth=None, expected=200):
        headers = self.key.copy() if auth is None else auth.copy()
        if data is not None:
            headers.update({'Origin': self.origin, 'Content-Type': 'application/json'})
        with contextlib.closing(http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)) as client:
            client.request('GET' if data is None else 'POST', '/api/' + path, json.dumps(data) if data is not None else None, headers)
            response = client.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, expected, payload)
            return payload, dict(response.getheaders())

    def plan(self, kind, operation, query, limit):
        return {'name': 'Typed lookup', 'steps': [
            {'id': 'start', 'type': 'start', 'next': 'lookup'},
            {'id': 'lookup', 'type': kind, 'config': {'adapter': kind + '-adapter', 'operation': operation,
                                                     'query': query, 'limit': limit}, 'next': 'done'},
            {'id': 'done', 'type': 'finish'},
        ]}

    def version(self, kind, operation, query, limit):
        version = self.api('save', {'project': DEFAULT_PROJECT, 'plan': self.plan(kind, operation, query, limit)})[0]
        return {key: version[key] for key in ('workflow', 'version')}

    def start(self, version, mode, fields=None, **kwargs):
        return self.api('start', {**version, 'input': {'privateUnrelatedValue': 'unused'},
                                  'request': token(), 'mode': mode, **(fields or {})}, **kwargs)[0]

    def decide(self, run, approve=True, **kwargs):
        return self.api('decide', {'id': run['id'], 'node': 'lookup', 'approve': approve}, **kwargs)[0]

    def publish(self, version):
        test_run = self.start(version, 'offline', {'input': {'query': 'example-query'}})
        self.assertEqual(test_run['state'], 'waiting', test_run['history'])
        tested = self.decide(test_run)
        self.assertEqual(tested['state'], 'finished', tested['history'])
        self.api('publish', version)

    def user(self, role, project=DEFAULT_PROJECT):
        invite = self.api('managed/invite', {'project': project, 'email': role + '@example.org', 'role': role, 'method': 'local'})[0]
        raw = parse_qs(urlsplit(invite['url']).fragment)['invite'][0]
        _, headers = self.api('auth/accept', {'token': raw, 'password': 'a strong onboarding password'}, auth={})
        session = {'Cookie': headers['Set-Cookie'].split(';', 1)[0]}
        session['X-CSRF-Token'] = self.api('auth/status', auth=session)[0]['csrf']
        return session

    def test_offline_and_model_test_never_send_any_lookup(self):
        for kind, operation, query, limit in CASES:
            with self.subTest(kind=kind, operation=operation):
                version = self.version(kind, operation, query, limit)
                for mode in ('offline', 'test'):
                    waiting = self.start(version, mode, {'modelConsent': True, 'liveReadConsent': True})
                    self.assertEqual(waiting['state'], 'waiting', waiting['history'])
                    self.assertEqual(waiting['context']['pending']['value'], {'operation': operation, 'query': query, 'limit': limit})
                    completed = self.decide(waiting)
                    self.assertEqual(completed['state'], 'finished')
                    self.assertEqual(completed['context']['result']['provenance'], {
                        'adapter': kind + '-adapter', 'operation': operation, 'mode': 'sample',
                    })
                    self.assertEqual(completed['context']['result']['items'][0]['sourceUrl'],
                                     'https://example.org/fixtures/' + operation)
        self.assertEqual(self.requests, [])

    def test_managed_live_paths_send_typed_fields_once_after_consent_and_review(self):
        operator = self.user('operator')
        reviewer = self.user('reviewer')
        editor = self.user('editor')
        for index, (kind, operation, query, limit) in enumerate(CASES):
            with self.subTest(kind=kind, operation=operation):
                version = self.version(kind, operation, query, limit)
                self.publish(version)
                for fields in ({}, {'liveWriteConsent': True}):
                    run = self.start(version, 'live', fields, auth=operator)
                    self.assertEqual(run['state'], 'failed')
                    self.assertIn('live source read permission', run['history'][-1]['error'])
                self.start(version, 'live', {'liveReadConsent': True}, auth=editor, expected=403)
                decline = self.start(version, 'live', {'liveReadConsent': True}, auth=operator)
                self.assertEqual(self.decide(decline, False, auth=reviewer)['state'], 'declined')
                waiting = self.start(version, 'live', {'liveReadConsent': True}, auth=operator)
                self.assertEqual(waiting['state'], 'waiting')
                self.assertEqual(len(self.requests), index)
                result = self.decide(waiting, auth=reviewer)
                self.assertEqual(result['state'], 'finished')
                self.assertEqual(result['decisions'][0]['actor']['role'], 'reviewer')
                self.assertEqual(result['context']['result']['items'], [
                    {**SAMPLES[operation], 'sourceUrl': 'https://evidence.example.org/item'},
                ])
                self.assertEqual(result['context']['result']['provenance']['mode'], 'live')
                self.assertIn('retrievedAt', result['context']['result']['provenance'])
                self.decide(waiting, auth=reviewer, expected=409)
                self.assertEqual(len(self.requests), index + 1)
                self.assertEqual(self.requests[-1], ('/' + kind, {'operation': operation, 'query': query, 'limit': limit},
                                                      'Bearer large-local-adapter-test-value'))

    def test_project_catalog_and_save_are_scoped_and_adapter_binding_is_pinned(self):
        editor = self.user('editor', self.other)
        catalog = self.api('workspace', auth=editor)[0]
        self.assertEqual(catalog['lookupAdapters'], {})
        own = self.api('workspace')[0]['lookupAdapters']
        self.assertEqual(own['github-adapter']['operations'], ['issues', 'pull-requests', 'repository'])
        self.assertNotIn('LOOKUP_ADAPTER_TEST_KEY', json.dumps(own))
        self.assertNotIn('/github', json.dumps(own))
        self.api('save', {'project': self.other, 'plan': self.plan(*CASES[0])}, auth=editor, expected=400)
        version = self.version(*CASES[0])
        self.publish(version)
        waiting = self.start(version, 'live', {'liveReadConsent': True})
        self.connections.lookups['web-search-adapter']['url'] += '/changed'
        self.decide(waiting, expected=409)
        self.assertEqual(self.requests, [])

    def test_invalid_inputs_results_redirects_and_secrets_cannot_pass_typed_boundary(self):
        for kind, op, query, limit in [('github', 'repository', 'https://service.example/repo', 1),
                                        ('github', 'issues', 'example/..', 1),
                                        ('enrichment', 'company', 'https://example.org', 1),
                                        ('enrichment', 'person', 'person@localhost', 1),
                                        ('service-health', 'service-health', 'https://example.org/health', 1),
                                        ('worker-status', 'worker-status', 'worker; run task', 1),
                                        ('worker-status', 'start', 'example-worker', 1),
                                        ('published-workflow', 'publish', 'example-workflow', 1),
                                        ('weather-observation', 'weather-observation', 'example-station', 2),
                                        ('web-search', 'search', 'x' * 301, 1),
                                        ('web-search', 'search', 'report', 11)]:
            self.api('save', {'project': DEFAULT_PROJECT, 'plan': self.plan(kind, op, query, limit)}, expected=400)
        version = self.version('web-search', 'search', {'ref': {'node': '$input', 'path': ['query']}}, 1)
        self.publish(version)
        failure = self.start(version, 'live', {'input': {'query': ['raw'], 'secret': 'unused'}, 'liveReadConsent': True})
        self.assertEqual(failure['state'], 'failed')
        self.assertEqual(self.requests, [])

        attempts = [
            (307, {'Location': f'http://127.0.0.1:{self.adapter.server_port}/redirected'}, {}),
            (200, {}, {'items': [{'title': 'A record', 'sourceUrl': 'javascript:alert(1)'}]}),
            (200, {}, {'items': [{'title': 'A record', 'sourceUrl': 'https://example.org', 'extra': 'unexpected'}]}),
            (200, {}, {'items': [{'title': 'large-local-adapter-test-value', 'sourceUrl': 'https://example.org'}]}),
            (200, {}, {'items': [{'title': 'A record', 'sourceUrl': 'https://example.org'}] * 2}),
        ]
        for index, response in enumerate(attempts, 1):
            self.response_override = response
            waiting = self.start(version, 'live', {'input': {'query': 'public data'}, 'liveReadConsent': True})
            result = self.decide(waiting)
            self.assertEqual(result['state'], 'failed')
            self.assertNotIn('large-local-adapter-test-value', json.dumps(result))
            self.assertEqual(len(self.requests), index)
            self.assertEqual(self.requests[-1][0], '/web-search')

    def test_observations_require_typed_units_and_real_timestamps(self):
        version = self.version('weather-observation', 'weather-observation', {'ref': {'node': '$input', 'path': ['query']}}, 1)
        self.publish(version)
        station = {**SAMPLES['weather-observation'], 'sourceUrl': 'https://evidence.example.org/station'}
        for index, change in enumerate([
            {'observedAt': '2099-01-01T12:00:00Z'}, {'observedAt': '2025-01-01T12:00:00'},
            {'observedAt': '2025-02-30T12:00:00Z'}, {'temperatureC': '12.5'},
            {'temperatureC': True}, {'humidityPercent': 120}, {'windSpeedMps': -1},
        ], 1):
            self.response_override = (200, {}, {'items': [{**station, **change}]})
            ready = self.start(version, 'live', {'input': {'query': 'example-station'}, 'liveReadConsent': True})
            result = self.decide(ready)
            self.assertEqual(result['state'], 'failed')
            self.assertIn('invalid field value', result['history'][-1]['error'])
            self.assertEqual(len(self.requests), index)

    def test_health_and_publication_reject_actions_or_unsupported_statuses(self):
        cases = [
            ('service-health', 'example-service', {'status': 'starting'}),
            ('worker-status', 'example-worker', {'command': 'run job'}),
            ('published-workflow', 'example-workflow', {'version': 3}),
        ]
        for kind, query, change in cases:
            with self.subTest(kind=kind):
                version = self.version(kind, kind, query, 1)
                self.publish(version)
                item = {**SAMPLES[kind], **change, 'sourceUrl': 'https://evidence.example.org/item'}
                self.response_override = (200, {}, {'items': [item]})
                ready = self.start(version, 'live', {'liveReadConsent': True})
                result = self.decide(ready)
                self.assertEqual(result['state'], 'failed')
                self.assertIn('Lookup adapter returned an invalid', result['history'][-1]['error'])
