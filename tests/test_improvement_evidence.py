import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import penstock
import guided_improvements


class ImprovementEvidence(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        penstock.initialize(root)
        self.ws = penstock.Workspace(root, penstock.Connections(root))
        self.project = self.ws.create_project('Executive')['id']
        self.other = self.ws.create_project('Other')['id']

    def version(self, input_type, *, default=None, workflow=None, base=None, project=None):
        field = {'id':'target','name':'target','type':input_type,'description':'Target value'}
        if default is not None:
            field['value'] = default
        plan = {'name':'Exact input', 'steps':[
            {'id':'start','type':'start','config':{'runMetadata':False,'inputFormat':[field]},'next':'finish'},
            {'id':'finish','type':'finish'},
        ]}
        return self.ws.save(plan, workflow, base, project or self.project)

    def execute(self, version, input):
        return self.ws.start(version['workflow'], version['version'], 'offline', input, str(uuid.uuid4()))

    def request(self, version, project=None):
        return guided_improvements.test_change(self.ws, project or self.project,
            {'workflow':version['workflow'],'version':version['version'],'field':'target'})

    def test_multiple_exact_types_replay_against_real_start_and_preserve_the_plan(self):
        fixtures = [
            ('number','string',['pending','unknown']), ('boolean','string',['blocked','in progress']),
            ('object','string',['plain text','another update']), ('string','object',[{'count':1},{'count':2}]),
            ('object','array',[[1,2],[3,4]]), ('boolean','number',[2,3.5]),
        ]
        with patch.object(self.ws.connections, 'complete') as model:
            for existing,target,examples in fixtures:
                with self.subTest(existing=existing,target=target):
                    v = self.version(existing)
                    unaffected = self.execute(v, {})
                    self.assertEqual(unaffected['state'],'finished')
                    failures = [self.execute(v, {'target':example}) for example in examples]
                    self.assertEqual([r['state'] for r in failures],['failed','failed'])
                    change = self.request(v)
                    self.assertEqual((change['fromType'],change['toType']),(existing,target))
                    self.assertEqual(change['evidence'],[r['id'] for r in failures])
                    self.assertEqual(change['historicalInputs'],3)
                    self.assertEqual(change['results'][0],{'run':unaffected['id'],'before':True,'after':True})
                    self.assertEqual(change['plan']['steps'][0]['config']['inputFormat'][0]['type'],target)
                    self.assertEqual(self.ws.version(v['workflow'],1)['plan'],v['plan'])
                    self.assertEqual(self.ws.version(v['workflow'],1)['published'],0)
            listing = guided_improvements.improvements(self.ws,self.project)['items']
            self.assertEqual(len(listing),len(fixtures))
            model.assert_not_called()

    def test_refuses_to_rewrite_accepted_coercions_defaults_or_mixed_inputs(self):
        for before_values,d,failures in [
            (['true'],None,['blocked','waiting']), ([], 'true',['blocked','waiting']),
            ([],None,['blocked',{'state':'waiting'}]),
        ]:
            with self.subTest(before_values=before_values,default=d,failures=failures):
                v = self.version('boolean',default=d)
                for value in before_values:
                    self.assertEqual(self.execute(v,{'target':value})['state'],'finished')
                for bad in failures:
                    self.assertEqual(self.execute(v,{'target':bad})['state'],'failed')
                with self.assertRaisesRegex(ValueError,'every saved input'):
                    self.request(v)
        self.assertEqual(guided_improvements.improvements(self.ws,self.project)['items'],[])

    def test_isolates_project_version_and_revalidates_previously_listed_evidence(self):
        first = self.version('boolean')
        second = self.version('boolean',workflow=first['workflow'],base=first['version'])
        remote = self.version('boolean',project=self.other)
        self.execute(first,{'target':'pending'})
        self.execute(second,{'target':'waiting'})
        self.execute(remote,{'target':'unavailable'})
        self.assertEqual(guided_improvements.improvements(self.ws,self.project)['items'],[])
        self.execute(second,{'target':'blocked'})
        listing = guided_improvements.improvements(self.ws,self.project)['items']
        self.assertEqual([(item['workflow'],item['version']) for item in listing],[(second['workflow'],2)])
        with self.assertRaisesRegex(ValueError,'matching Start failures'):
            self.request(second,self.other)
        self.execute(second,{'target':{'state':'blocked'}})
        self.assertEqual(guided_improvements.improvements(self.ws,self.project)['items'],[])
        with self.assertRaisesRegex(ValueError,'every saved input'):
            self.request(second)

    def test_history_over_bound_is_never_reported_as_comprehensive(self):
        v = self.version('object')
        for value in ['first','second','third','fourth']:
            self.execute(v,{'target':value})
        with patch.object(guided_improvements,'HISTORY_PER_VERSION',3):
            self.assertEqual(guided_improvements.improvements(self.ws,self.project)['items'],[])
            with self.assertRaisesRegex(ValueError,'every saved input'):
                self.request(v)
