import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import penstock
from starter_api import dispatch
import starter_tables
import typed_tables


class StarterTableSources(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        penstock.initialize(root)
        self.workspace = penstock.Workspace(root, penstock.Connections(root))
        self.project = self.workspace.create_project('Executive')['id']
        self.other = self.workspace.create_project('Confidential')['id']

    def table(self, name, columns, records=(), *, project=None, in_current_file=False):
        project = project or self.project
        with self.workspace.data.db(self.project if in_current_file else project) as c:
            created = typed_tables.new_table(c,project,project,{'name':name,'columns':columns})
            table = created['id']
            for values in records:
                typed_tables.append(c,project,project,{'id':table,'values':values})
        return table

    def request(self, endpoint, fields=None, *, can_run=True):
        return dispatch(self.workspace,endpoint,{'project':self.project,**(fields or {})},can_run=can_run)

    def test_project_snapshot_maps_only_needed_columns_and_feeds_a_real_brief(self):
        table = self.table('Operations updates',['Date','Update','Status','Owner','Due Date','Decision','Source','Internal note'],[
            ['2026-11-14','Review forecast','In progress','Finance','2026-11-15','','','omit this'],
            ['2026-11-19','Decide reporting scope','Completed','Operations','','Report totals','Meeting record','omit that'],
        ])
        self.table('Other project table',['Date','Title','Status'],project=self.other,in_current_file=True)
        with patch.object(self.workspace.connections,'complete') as model:
            self.assertEqual(self.request('starter-table-list'),{'tables':[{'id':table,'name':'Operations updates'}]})
            imported = self.request('starter-table-input',{'table':table})
            self.assertEqual(imported['name'],'Operations updates')
            self.assertEqual((imported['input']['from'],imported['input']['through']),('2026-11-14','2026-11-19'))
            self.assertEqual([row['status'] for row in imported['input']['records']],['in-progress','complete'])
            self.assertEqual([row['source'] for row in imported['input']['records']],['Operations updates','Meeting record'])
            self.assertNotIn('omit',json.dumps(imported))
            with self.workspace.data.db(self.project) as c:
                self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='starter_runs'").fetchone())
            receipt = self.request('starter-run',{'kind':'weekly-brief','version':1,'input':imported['input'],
                'source':'custom','request':str(uuid.uuid4())})
            self.assertEqual(receipt['status'],'awaiting-review')
            self.assertEqual(receipt['output']['total'],2)
            self.assertIn('Operations updates',receipt['output']['markdown'])
            self.assertEqual(self.workspace.data.describe(self.project)['files'],[])
            model.assert_not_called()

    def test_run_role_and_project_are_required_in_both_table_operations(self):
        other = self.table('Other project table',['Date','Title','Status'],project=self.other,in_current_file=True)
        self.assertEqual(self.request('starter-table-list'),{'tables':[]})
        for endpoint,fields in [('starter-table-list',{}),('starter-table-input',{'table':other})]:
            with self.assertRaises(PermissionError):
                self.request(endpoint,fields,can_run=False)
        with self.assertRaisesRegex(ValueError,'from this project'):
            self.request('starter-table-input',{'table':other})

    def test_table_mismatches_are_rejected_without_a_partial_result(self):
        aliases = self.table('Duplicate aliases',['Date','Title','Update','Status'])
        with self.assertRaisesRegex(ValueError,'one column for title'):
            self.request('starter-table-input',{'table':aliases})
        missing = self.table('Missing status',['Date','Title'])
        with self.assertRaisesRegex(ValueError,'Status columns'):
            self.request('starter-table-input',{'table':missing})
        oversized = self.table('Too many rows',['Date','Title','Status'],
            [['2026-11-14',str(i),'Complete'] for i in range(starter_tables.MAX_UPDATES+1)])
        with patch('starter_recipes.update_record') as validate, self.assertRaisesRegex(ValueError,'between 1 and 40'):
            self.request('starter-table-input',{'table':oversized})
        validate.assert_not_called()
        mixed = self.table('Mixed records',['Date','Title','Status'],[
            ['2026-11-14','Completed item','Complete'], ['2026-11-16','Invalid state','TBD'],
        ])
        with self.assertRaisesRegex(ValueError,'Row 2: Choose a status'):
            self.request('starter-table-input',{'table':mixed})
        with self.workspace.data.db(self.project) as c:
            c.execute("UPDATE project_records SET values_json='[]' WHERE table_id=?",(mixed,))
        with self.assertRaisesRegex(ValueError,'no longer match this schema'):
            self.request('starter-table-input',{'table':mixed})

    def test_long_date_range_defaults_to_a_valid_period_and_keeps_all_rows(self):
        table = self.table('Quarter updates',['Date','Title','Status'],[
            ['2026-02-03','Newest','Complete'], ['2026-01-02','Earlier','Blocked'],
        ])
        imported = self.request('starter-table-input',{'table':table})['input']
        self.assertEqual((imported['from'],imported['through']),('2026-01-04','2026-02-03'))
        self.assertEqual([record['title'] for record in imported['records']],['Earlier','Newest'])
