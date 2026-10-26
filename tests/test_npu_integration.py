import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from src.cli_commands import _asset_plan, _runtime_preflight, _telemetry_plan_bytes
from src.config_loader import ConfigError, PlatformConfig, ProfileConfig, validate_workload_config
from src.events import EventEnvelope, build_event
from src.path_resolver import PathResolver
from src.policy_engine import PolicyEngine, PolicyLimits
from src.qualification import GoldenService, QualificationError
from src.run_orchestrator import RunManifestBuilder
from src.target_adapters import get_target
from src.transport import FakeTransport
from src.verification_contract import verification_issues, verification_requirement, validate_capabilities, REQUIRED_FEATURES

ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = ROOT.parent / 'npu-workload' / 'npuworkload'


def config():
    return dict(profile='kws01', api='npu', mode='inference', backend='null',
                input_manifest=str(WORKLOAD / 'profiles/kws01/manifest.json'), duration=2, timeout=15,
                inferences=10, warmup_inferences=0, verify_mode='checksum', verify_interval=1,
                success_log_interval=60, heartbeat_interval=1, output_format='jsonl')


def event(seq, kind, payload):
    return EventEnvelope.from_mapping(build_event(run_id='npu-test', seq=seq, timestamp_ms=seq,
        source='npu-workload', event_type=kind, payload=payload))


class NpuConfigTests(unittest.TestCase):
    def test_four_profiles_and_native_warmup_semantics(self):
        for model in ('kws01', 'ic01', 'ad01', 'sww01'):
            for purpose in ('smoke', 'load', 'qualification'):
                path = ROOT / f'config/workloads/npu_{model}_{purpose}.json'
                value = validate_workload_config(path, 'npu')
                self.assertEqual(value['profile'], model)
                self.assertIn('warmup_inferences', value)
                self.assertNotIn('warmup', value)
                self.assertEqual(verification_requirement('npu', value)['unit_metric'], 'inference_count')

    def test_rejects_bad_types_aliases_nonfinite_and_log_modes(self):
        cases = [dict(duration=True), dict(duration=float('nan')), dict(timeout=float('inf')),
                 dict(inferences=-1), dict(inferences=1.5), dict(warmup_inferences=True),
                 dict(verify_interval=0), dict(success_log_interval=-1), dict(summary_only=True),
                 dict(per_inference_log=1), dict(heartbeat_interval=0), dict(warmup=1),
                 dict(verify_mode='golden-image'), {'verify-interval': 1}, dict(golden_checksum='xyz'),
                 dict(runtime_library='bad\npath'), dict(backend='tflite_delegate')]
        for patch in cases:
            with self.subTest(patch=patch), self.assertRaises(ConfigError):
                get_target('npu').validate_config(config() | patch)

    def test_metric_units_and_unknown_device_time(self):
        value = get_target('npu').metrics(dict(inference_count=5, inferences_per_sec_avg=10,
             host_inference_time_ms_p99=2, device_time_valid=False, device_inference_time_ms_p99=0))
        self.assertEqual(value['throughput_unit'], 'inferences/s')
        self.assertIsNone(value['device_latency_p99_ms'])
        self.assertEqual(value['execution_placement'], 'unknown')

    def test_success_interval_is_count_not_seconds(self):
        self.assertEqual(get_target('npu').validate_config(config() | {'success_log_interval': 1})['success_log_interval'], 1)

    def test_npu_telemetry_requires_explicit_platform_paths(self):
        platform = PlatformConfig.from_file(ROOT / 'config/platforms/kirin9030.yaml')
        profile = ProfileConfig.from_file(ROOT / 'config/profiles/npu_kws01_smoke.yaml')
        from dataclasses import replace
        with self.assertRaises(ConfigError):
            _telemetry_plan_bytes(replace(profile, telemetry={'required': ['npu.temperature']}), platform)
        self.assertNotIn('cpu.', _telemetry_plan_bytes(profile, platform).decode())


@unittest.skipUnless(WORKLOAD.exists(), 'paired workload checkout is needed for asset integration')
class NpuAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = PathResolver.create(entrypoint=ROOT / 'main.py', output_dir=self.root / 'out', state_dir=self.root / 'state')
        self.binary = self.root / 'npu executable'
        self.binary.write_bytes(b'MZ host fixture')
        self.cfg_path = self.root / 'workload.json'
        self.data = config()
        self.cfg_path.write_text(json.dumps(self.data))
        self.profile = ProfileConfig.from_mapping(dict(schema_version=1, name='npu fixture', target='npu',
            platform='kirin9030', purpose='smoke', workload=dict(binary=str(self.binary), config=str(self.cfg_path), target_abi='host-test')),
            source_path=self.root / 'profile.json')

    def test_complete_assets_and_effective_device_config(self):
        assets, _, original = _asset_plan(self.paths, self.profile, None)
        self.assertEqual(original, self.cfg_path)
        self.assertEqual({a.kind for a in assets}, {'agent','workload','workload-config','npu-manifest','npu-model','npu-input'})
        effective_asset = next(a for a in assets if a.kind == 'workload-config')
        effective = json.loads(effective_asset.local.read_text())
        self.assertEqual(effective['input_manifest'], '/data/local/tmp/avs/npu/npu fixture/manifest.json')
        self.assertEqual(json.loads(self.cfg_path.read_text())['input_manifest'], self.data['input_manifest'])
        self.assertEqual(next(a for a in assets if a.kind=='npu-model').remote.name, 'model.tflite')
        self.assertEqual(effective_asset.local, next(a for a in _asset_plan(self.paths,self.profile,None)[0] if a.kind=='workload-config').local)

    def test_all_four_manifests_are_checked(self):
        for model in ('kws01','ic01','ad01','sww01'):
            self.cfg_path.write_text(json.dumps(self.data | dict(profile=model,input_manifest=str(WORKLOAD/f'profiles/{model}/manifest.json'))))
            self.assertTrue(any(a.kind=='npu-model' for a in _asset_plan(self.paths,self.profile,None)[0]))

    def test_manifest_hash_descriptor_and_escape_rejection(self):
        shutil.copytree(WORKLOAD/'profiles/kws01', self.root/'model')
        path=self.root/'model/manifest.json'
        initial=json.loads(path.read_text())
        self.cfg_path.write_text(json.dumps(self.data | dict(input_manifest=str(path))))
        for mutate in ('hash','shape','escape','absolute','backslash','profile_type','inputs_type','model_type','tensor_type'):
            doc=copy.deepcopy(initial)
            if mutate=='profile_type': doc['profile']=[]
            if mutate=='inputs_type': doc['inputs']=None
            if mutate=='model_type': doc['model']=None
            if mutate=='tensor_type': doc['inputs']=[1]
            if mutate=='hash': doc['model']['sha256']='0'*64
            if mutate=='shape': doc['inputs'][0]['shape']=[1,1]
            if mutate=='escape': doc['inputs'][0]['file']='../workload.json'
            if mutate=='absolute': doc['asset_root']=str(self.root)
            if mutate=='backslash': doc['inputs'][0]['file']='..\\workload.json'
            path.write_text(json.dumps(doc))
            with self.subTest(mutate=mutate), self.assertRaises(ConfigError):
                _asset_plan(self.paths,self.profile,None)

    def test_host_libraries_cannot_be_deployed_as_device_elf(self):
        from dataclasses import replace
        with self.assertRaises(ConfigError):
            get_target('npu').check_binary(self.binary,replace(self.profile,workload=self.profile.workload|{'target_abi':'aarch64-linux'}))

    def test_qualification_rejects_framework_and_unknown_placement(self):
        with self.assertRaises(ConfigError):
            get_target('npu').qualification_evidence(self.paths,self.profile,self.data,[])

    def test_installed_runtime_needs_absolute_path_and_hash(self):
        from dataclasses import replace
        for entry in ({'path':'relative','sha256':'0'*64}, {'path':'/vendor/lib64/lib.so','sha256':'bad'}):
            with self.assertRaises(ConfigError):
                get_target('npu').installed_libraries(replace(self.profile,workload=self.profile.workload|{'installed_libraries':{'runtime_library':entry}}),self.data)


class NpuPolicyTests(unittest.TestCase):
    def setUp(self):
        self.requirement=verification_requirement('npu',config())
        self.summary=dict(result='PASS',exit_code=0,verify_pass=True,contract_version=2,verify_mode='checksum',
            verify_interval=1,success_log_interval=60,inference_count=10,verify_count=10,verify_fail_count=0)

    def test_actual_counts_required_and_checked(self):
        for patch in ({'verify_count':9},{'inference_count':True},{'verify_count':None},{'contract_version':1}):
            with self.subTest(patch=patch):
                self.assertTrue(verification_issues(self.summary|patch,self.requirement))
        self.assertEqual(verification_issues(self.summary,self.requirement),[])

    def test_bounded_split_evidence_and_failure_latch(self):
        engine=PolicyEngine(PolicyLimits(verification=self.requirement))
        for seq,(key,value) in enumerate(self.summary.items(),1):
            if key not in {'result','exit_code','verify_pass'}:
                engine.process(event(seq,'verification_summary',{key:value}))
        engine.process(event(20,'inference',{'inference':10}))
        engine.process(event(21,'summary',{'result':'PASS','exit_code':0,'verify_pass':True}))
        self.assertEqual(engine.finalize(require_agent_final=False).verdict,'PASS')
        failure=PolicyEngine()
        failure.process(event(1,'verify',{'pass':False,'inference':1}))
        self.assertTrue(failure.finalize(timed_out=True,require_agent_final=False).dut_reasons)

    def test_approved_reference_and_real_backend_required(self):
        req=self.requirement|{'expected_checksum':'abc','real_npu':True}
        self.assertTrue(verification_issues(self.summary|{'golden_checksum':'abc','backend':'null','fallback_used':False},req))
        self.assertTrue(verification_issues(self.summary|{'golden_checksum':'bad','backend':'tflite_delegate','fallback_used':False},req))
        self.assertFalse(verification_issues(self.summary|{'golden_checksum':'abc','checksum':'abc','backend':'tflite_delegate','fallback_used':False},req))

    def test_missing_summary_and_contradictory_parts_fail(self):
        engine=PolicyEngine(PolicyLimits(verification=self.requirement))
        engine.process(event(1,'verification_summary',{'verify_count':9}))
        engine.process(event(2,'summary',self.summary))
        self.assertEqual(engine.finalize(require_agent_final=False).verdict,'INFRA_ERROR')
        self.assertNotEqual(PolicyEngine().finalize(require_agent_final=False).verdict,'PASS')

    def test_wrong_target_capabilities_are_rejected(self):
        data={'workload':'npu','contract_version':2,'features':list(REQUIRED_FEATURES | {'success_log_rate_limit'}),'success_log_min_period_ms':1000,'verify_modes':['checksum']}
        validate_capabilities(data,'npu','checksum')
        for patch in ({'workload':'gpu'},{'contract_version':True},{'features':[]}):
            with self.assertRaises(ValueError): validate_capabilities(data|patch,'npu','checksum')

    def test_golden_requires_approval_identity_and_consistent_records(self):
        with tempfile.TemporaryDirectory() as temp:
            service=GoldenService(Path(temp))
            args=dict(qualification_id='npu',profile='npu_kws01_qualification',fingerprint_fields={'target':'npu','placement_evidence_sha256':'a'*64},
                      golden_records=[{'checksum':'a'*16}],board_ids=['known-good'],approver='operator',conditions='nominal OPP; vendor diagnostic 123')
            result=service.create_npu(**args)
            self.assertEqual(result['kind'],'npu-checksum')
            self.assertTrue(result['approval']['known_good'])
            with self.assertRaises(QualificationError): service.create_npu(**args)
            with self.assertRaises(QualificationError): service.create_npu(**(args|{'qualification_id':'bad','approver':''}))
            with self.assertRaises(QualificationError): service.create_npu(**(args|{'qualification_id':'bad','golden_records':[{'checksum':'a'*16},{'checksum':'b'*16}]}))

class NpuQualificationTests(unittest.TestCase):
    def test_real_sample_requires_external_golden_counts_and_placement(self):
        from src.qualification import CalibrationSample, CalibrationPolicy, CalibrationService
        summary=dict(result='PASS',exit_code=0,verify_pass=True,contract_version=2,verify_mode='checksum',
            verify_interval=1,success_log_interval=60,inference_count=10,verify_count=10,verify_fail_count=0,
            golden_checksum='a'*16,checksum='a'*16,backend='tflite_delegate',fallback_used=False,inferences_per_sec_avg=10,
            host_inference_time_ms_p99=2)
        golden={'kind':'npu-checksum','checksum':'a'*16,'approval':{'known_good':True,'approver':'operator'}}
        policy=CalibrationPolicy(minimum_boards=2,minimum_accepted_samples=2)
        samples=[CalibrationSample(f'run-{i}',f'board-{i}',summary,qualification_evidence={'status':'full_npu'}) for i in range(2)]
        args=dict(profile='npu_kws',target='npu',platform='test',fingerprints={},golden=golden,
                  samples=samples,policy=policy,baseline_id='npu-test')
        proposal=CalibrationService().calibrate(**args)
        self.assertEqual(proposal['target'],'npu')
        self.assertEqual(proposal['status'],'draft')
        self.assertIn('inferences_per_sec_avg',proposal['thresholds']['performance'])
        from dataclasses import replace
        for bad in (replace(samples[0],qualification_evidence={}),
                    replace(samples[0],summary=summary|{'backend':'null'}),
                    replace(samples[0],summary=summary|{'verify_count':9}),
                    replace(samples[0],summary=summary|{'golden_checksum':'b'*16})):
            with self.assertRaises(QualificationError):
                CalibrationService().calibrate(**(args|{'samples':[bad,samples[1]]}))
        with self.assertRaises(QualificationError):
            CalibrationService().calibrate(**(args|{'golden':golden|{'approval':{}}}))

    def test_collected_source_requires_matching_final_hashes_and_vendor_reference(self):
        from src.npu_qualification import validate_source_run
        from src.artifact_store import sha256_file
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);spool=root/'spool';spool.mkdir()
            evidence=root/'placement.json';evidence.write_text('{}')
            (root/'workload.json').write_text(json.dumps({'profile':'kws01'}))
            profile=ProfileConfig.from_mapping(dict(schema_version=1,name='npu-test',target='npu',platform='test',
                workload={'binary':'fake','config':'workload.json','placement_evidence':str(evidence)}),source_path=root/'profile.json')
            manifest={'run_id':'attempt','test_id':'test','attempt_id':'attempt','target':'npu',
                      'profile':{'sha256':profile.fingerprint},'qualification':{'mode':'golden'}}
            (root/'run-manifest.json').write_text(json.dumps(manifest))
            (root/'result.json').write_text(json.dumps(dict(run_id='attempt',verdict='PASS',exit_code=0)))
            placement={'status':'full_npu','evidence_sha256':sha256_file(evidence)}
            (root/'deployment-manifest.json').write_text(json.dumps({'npu_placement':placement}))
            summary=dict(type='summary',profile='kws01',result='PASS',exit_code=0,contract_version=2,backend='tflite_delegate',
                         fallback_used=False,inference_count=0,verify_count=0)
            (spool/'workload-summary.json').write_text(json.dumps(summary))
            (spool/'events.jsonl').write_text(json.dumps({'run_id':'attempt','type':'golden','payload':{'checksum':'a'*16}})+'\n')
            final=dict(test_id='test',attempt_id='attempt',workload_exit_code=0,summary_seen=True,timed_out=False)
            (spool/'final.json').write_text(json.dumps(final))
            def rehash():
                hashes={p.name:sha256_file(p) for p in spool.iterdir() if p.name!='artifact-hashes.json'}
                (spool/'artifact-hashes.json').write_text(json.dumps({'sha256':hashes}))
            rehash()
            self.assertEqual(validate_source_run(root,profile,golden=True)[1]['status'],'full_npu')
            (spool/'final.json').write_text(json.dumps(final|{'attempt_id':'other'}));rehash()
            with self.assertRaises(QualificationError): validate_source_run(root,profile,golden=True)
            (spool/'final.json').write_text(json.dumps(final));rehash()
            (spool/'workload-summary.json').write_text(json.dumps(summary|{'verify_count':1}))
            with self.assertRaises(QualificationError): validate_source_run(root,profile,golden=True)
            rehash()
            with self.assertRaises(QualificationError): validate_source_run(root,profile,golden=True)

class NpuReplayTests(unittest.TestCase):
    def test_raw_and_events_replay_require_the_same_actual_checks(self):
        import subprocess, sys
        from src.events import encode_event
        from src.uart_protocol import encode_uart_frame
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for missing in (False,True):
                summary=dict(result='PASS',exit_code=0,verify_pass=True,contract_version=2,verify_mode='checksum',
                    verify_interval=1,success_log_interval=60,inference_count=2,verify_count=2,verify_fail_count=0)
                if missing: summary.pop('verify_count')
                records=[build_event(run_id='replay-npu',seq=1,timestamp_ms=1,source='agent',event_type='agent_start',payload={}),
                         build_event(run_id='replay-npu',seq=2,timestamp_ms=2,source='npu-workload',event_type='summary',payload=summary),
                         build_event(run_id='replay-npu',seq=3,timestamp_ms=3,source='agent',event_type='agent_final',payload={'workload_exit_code':0,'summary_seen':True})]
                from src.events import event_crc32
                for record in records:
                    record['test_id']='replay-npu-test'
                    record['crc32']=event_crc32(record)
                for raw in (False,True):
                    path=root/('raw.bin' if raw else 'events.jsonl')
                    path.write_bytes(b''.join(encode_uart_frame(r) if raw else encode_event(r) for r in records))
                    result=subprocess.run([sys.executable,str(ROOT/'main.py'),'--json','--output-dir',str(root/'out'),
                        'simulate','--profile','npu_kws01_smoke','--raw-serial' if raw else '--events',str(path)],capture_output=True,text=True,timeout=15,cwd=ROOT)
                    self.assertEqual(result.returncode,3 if missing else 0,result.stdout+result.stderr)
                    self.assertEqual(json.loads(result.stdout)['verdict'],'INFRA_ERROR' if missing else 'PASS')

class NpuApprovalTests(unittest.TestCase):
    def test_run_builder_rejects_unapproved_external_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            profile=ProfileConfig.from_mapping(dict(schema_version=1,name='npu-test',target='npu',platform='test',
                workload={'binary':'unused'}),source_path=root/'profile.json')
            paths=PathResolver.create(entrypoint=ROOT/'main.py',output_dir=root/'out',state_dir=root/'state')
            from src.run_orchestrator import RunError
            for approval in ({},{'approver':'operator','known_good':False},{'approver':'operator','known_good':1}):
                with self.assertRaises(RunError):
                    RunManifestBuilder(paths).build(profile=profile,golden={'kind':'npu-checksum','checksum':'a'*16,'approval':approval},device_uart='/dev/fixture')
