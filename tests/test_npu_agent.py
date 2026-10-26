import json
import os
import shlex
import unittest
from pathlib import Path

from src.events import EventEnvelope
from src.policy_engine import PolicyEngine, PolicyLimits
from src.verification_contract import verification_requirement
import test_shell_device_agent as shell_helpers

ROOT=Path(__file__).resolve().parents[1]
WORKLOAD=ROOT.parent/'npu-workload/npuworkload'
BINARY=WORKLOAD/'build/linux-release/npu-avs-workload'


@unittest.skipUnless(os.name=='posix' and BINARY.exists(),'requires POSIX shell and the paired native NPU build')
class NpuAgentTests(unittest.TestCase):
    def run_agent(self,script,identifiers=('npu-test','npu-run')):
        helper=shell_helpers.ShellDeviceAgentTests()
        return helper._run_verify_stream(script,target='npu',identifiers=identifiers)

    def test_native_kws_closed_loop_and_original_inference_evidence(self):
        argv=[str(BINARY),'--profile','kws01','--backend','null','--input-manifest',str(WORKLOAD/'profiles/kws01/manifest.json'),
              '--duration','0','--inferences','5','--warmup-inferences','1','--per-inference-log']
        result,wire,local,_=self.run_agent('exec '+shlex.join(argv)+'\n')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len([r for r in local if r['type']=='inference']),5)
        engine=PolicyEngine(PolicyLimits(verification=verification_requirement('npu',{'verify_mode':'checksum'})))
        for record in wire: engine.process(EventEnvelope.from_mapping(record))
        verdict=engine.finalize()
        self.assertEqual(verdict.verdict,'PASS',verdict.to_dict())
        self.assertEqual(verdict.workload_summary['inference_count'],5)
        self.assertEqual(verdict.workload_summary['verify_count'],5)
        self.assertFalse(verdict.workload_summary['device_time_valid'])
        self.assertIn('inferences_per_sec_avg',verdict.workload_summary)
        self.assertTrue(all(r['source'] in {'agent','npu-workload'} for r in wire))

    def test_failure_latched_without_summary(self):
        script="printf '%s\\n' '{\"type\":\"verify\",\"inference\":7,\"pass\":false,\"result\":\"FAIL\"}'\nexit 1\n"
        result,wire,local,_=self.run_agent(script)
        self.assertEqual(result.returncode,1)
        failure=next(r for r in wire if r['type']=='verify')
        self.assertEqual(failure['payload']['inference'],7)
        engine=PolicyEngine()
        for record in wire: engine.process(EventEnvelope.from_mapping(record))
        self.assertNotEqual(engine.finalize().verdict,'PASS')

    def test_long_summary_remains_local_and_metadata_is_split(self):
        maximum=2**64-1
        summary=dict(type='summary',result='PASS',exit_code=0,verify_pass=True,contract_version=2,verify_mode='checksum',
                     verify_count=maximum,verify_fail_count=0,verify_interval=1,success_log_interval=60,inference_count=maximum,
                     backend='tflite_delegate',fallback_used=False,checksum='a'*16,golden_checksum='a'*16,device_time_valid=False,
                     inferences_per_sec_avg=12345.5,host_inference_time_ms_p99=1.2,partitions=['x'*5000])
        result,wire,local,_=self.run_agent("printf '%s\\n' "+shlex.quote(json.dumps(summary))+'\n',('t'*60,'r'*70))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(next(r for r in local if r['type']=='summary')['payload']['partitions'],['x'*5000])
        engine=PolicyEngine(PolicyLimits(verification=verification_requirement('npu',{'verify_mode':'checksum'})))
        for record in wire: engine.process(EventEnvelope.from_mapping(record))
        self.assertEqual(engine.finalize().verdict,'PASS')
        self.assertNotIn('partitions',engine.workload_summary)
        self.assertEqual(engine.workload_summary['inference_count'],maximum)
