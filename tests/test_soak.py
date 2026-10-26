import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('soak',ROOT/'scripts/soak.py');soak=importlib.util.module_from_spec(spec);spec.loader.exec_module(soak)


class SoakTests(unittest.TestCase):
    def test_plan_requires_exact_seven_days_and_bounded_cycles(self):
        plan=json.loads((ROOT/'config/soak/week.example.json').read_text(encoding='utf-8'))
        self.assertEqual(len(soak.validate_plan(plan)['days']),7)
        for key in ('days','cycles','profiles'):
            bad=copy.deepcopy(plan)
            if key=='days':bad['days'].pop()
            if key=='cycles':bad['days'][0]['cycles']=True
            if key=='profiles':bad['days'][0]['profiles']=[]
            with self.assertRaises(ValueError):soak.validate_plan(bad)

    def test_empty_summary_preserves_unexecuted_days_and_missing_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            result=soak.summarize(Path(temp))
            self.assertEqual(result['record_count'],0)
            self.assertTrue(all(day['status']=='未执行' for day in result['days']))
            self.assertTrue(all(day['accepted_load_s'] is None for day in result['days']))

    def test_summary_keeps_failure_retry_and_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            rows=[{'day':1,'state':'FAILED','measured_workload_s':4,'accepted_load_s':None},
                  {'day':1,'state':'COMPLETED','collection_verified':True,'measured_workload_s':10,'accepted_load_s':10},
                  {'day':1,'state':'SKIPPED'}]
            for row in rows:soak.append_ledger(root/'ledger.jsonl',row)
            day=soak.summarize(root)['days'][0]
            self.assertEqual((day['attempts'],day['passed'],day['failed'],day['skipped']),(2,1,1,1))
            self.assertEqual(day['measured_workload_s'],14)
            self.assertEqual(day['accepted_load_s'],10)

    def test_malformed_ledger_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'ledger.jsonl';path.write_text('{broken')
            with self.assertRaises(ValueError):soak.ledger_rows(path)

    def test_framework_does_not_count_as_pressure_time(self):
        profile=SimpleNamespace(target='npu')
        config={'backend':'null','verify_mode':'none'}
        values=soak.measurements({'inference_count':10,'duration_s':30},profile,config,'PASS',True)
        self.assertIsNone(values['measured_workload_s'])
        self.assertEqual(values['scope'],'框架检查')

    def test_measured_time_is_not_wall_time_and_rejected_evidence_stays_unavailable(self):
        profile=SimpleNamespace(target='cpu')
        values=soak.measurements({'batch_count':2,'duration_s':5},profile,{'backend':'integer','verify_mode':'none'},'PASS',False)
        self.assertEqual(values['measured_workload_s'],5)
        self.assertIsNone(values['accepted_load_s'])
        self.assertFalse(values['correctness_checked'])
        for invalid in (None,True,float('inf'),-1):
            values=soak.measurements({'batch_count':2,'duration_s':invalid},profile,{'backend':'integer','verify_mode':'none'},'PASS',True)
            self.assertIsNone(values['measured_workload_s'])

    def test_board_reference_conflicts_and_invalid_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'board.json'
            initial={'board_id':'A','device':'x','pc_serial':'x','platform':'test','available_targets':['cpu']}
            for patch_value in ({'board_id':'../x'},{'npu_delegate_ready':1},{'golden_by_profile':{'p':'x'},'baseline_by_profile':{'p':'x'}}):
                p.write_text(json.dumps(initial|patch_value))
                with self.assertRaises(ValueError):soak.board_config(p)

class RunnerEvidenceTests(unittest.TestCase):
    def create_runner(self,root,dry=False):
        plan=ROOT/'config/soak/week.example.json'
        board=root/'board.json';board.write_text(json.dumps({'board_id':'B','device':'not-contacted','pc_serial':'not-contacted','platform':'kirin9030','available_targets':['cpu'],'npu_delegate_ready':False}))
        campaign=root/'campaign';campaign.mkdir()
        args=SimpleNamespace(board=board,plan=plan,campaign=campaign,day=4,dry_run=dry,retry_failed=False,prepare=False,continue_on_error=False)
        return soak.Runner(args)

    def test_unsupported_target_is_skipped_without_invoking_device(self):
        with tempfile.TemporaryDirectory() as temp:
            runner=self.create_runner(Path(temp))
            with patch.object(runner,'invoke',side_effect=AssertionError('device must not be contacted')):
                self.assertTrue(runner.execute('npu_kws01_load',1))
            record=soak.ledger_rows(runner.ledger)[0]
            self.assertEqual(record['state'],'SKIPPED')
            self.assertFalse(record['collection_verified'])

    def test_dry_run_does_not_write_actual_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            runner=self.create_runner(Path(temp),dry=True)
            runner.execute('npu_kws01_load',1)
            self.assertFalse(runner.ledger.exists())

    def test_valid_completed_evidence_is_not_rerun(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            runner=self.create_runner(Path(temp))
            key=hashlib.sha256(f'{runner.identity}:B:4:cpu_qualification_kirin9030:1'.encode()).hexdigest()
            runner.history=[{'execution_key':key,'state':'COMPLETED','collection_verified':True}]
            with patch.object(runner,'invoke',side_effect=AssertionError('completed attempt must not rerun')):
                self.assertTrue(runner.execute('cpu_qualification_kirin9030',1))
            self.assertFalse(runner.ledger.exists())

    def test_day_numbers_do_not_create_fake_calendar_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for day in range(1,8):soak.append_ledger(root/'ledger.jsonl',{'day':day,'state':'SKIPPED','started_at':'2026-10-26T10:00:00+08:00'})
            self.assertEqual(soak.summarize(root)['recorded_calendar_days'],1)
    def test_prior_failure_without_retry_does_not_turn_day_into_success(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            runner=self.create_runner(Path(temp))
            key=hashlib.sha256(f'{runner.identity}:B:4:cpu_qualification_kirin9030:1'.encode()).hexdigest()
            runner.history=[{'execution_key':key,'state':'FAILED','collection_verified':False}]
            self.assertFalse(runner.execute('cpu_qualification_kirin9030',1))
