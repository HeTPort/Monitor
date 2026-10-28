"""Exercise public CLI, portable paths and packaged subprocess selection."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import stress
from src.path_resolver import PathResolver

ROOT = Path(__file__).resolve().parents[1]


class StressIntegrationTests(unittest.TestCase):
    def plan(self):
        return {'schema_version': 1, 'stages': [
            {'stage': 1, 'cycles': 1, 'profiles': ['cpu_smoke_kirin9030']}]}

    def test_stages_are_not_tied_to_calendar_days(self):
        plan = self.plan()
        self.assertEqual(len(stress.plan_stages(stress.validate_plan(plan))), 1)
        for value in (True, 0, 2):
            bad = copy.deepcopy(plan)
            bad['stages'][0]['stage'] = value
            with self.assertRaises(ValueError):
                stress.validate_plan(bad)
        with self.assertRaises(ValueError):
            stress.validate_plan(plan | {'days': []})

    def test_frozen_child_uses_same_executable_without_python_source(self):
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', 'X:/Release/vmin_judge.exe'):
            self.assertEqual(stress.monitor_command(), ['X:/Release/vmin_judge.exe'])
        self.assertEqual(stress.monitor_command()[1], str(ROOT / 'main.py'))

    def test_named_profiles_honor_external_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # A portable external config-only tree keeps bundled assets available.
            import shutil
            shutil.copytree(ROOT / 'config', root / 'config')
            paths = PathResolver.create(config_dir=root / 'config', entrypoint=ROOT / 'main.py')
            profile, config, selected = stress.resolve_profile('cpu_smoke_kirin9030', root / 'board.json', paths)
            self.assertTrue(profile.source_path.is_relative_to(root))
            self.assertTrue(selected.is_relative_to(root))
            self.assertEqual(config['backend'], 'mixed')

    def test_dry_run_through_public_entry_is_read_only_and_uses_existing_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'plan.json').write_text(json.dumps(self.plan()), encoding='utf-8')
            (root / 'board.json').write_text(json.dumps({
                'board_id': 'B', 'device': 'not-contacted', 'pc_serial': 'not-contacted',
                'device_uart': '/dev/test-uart', 'platform': 'kirin9030', 'available_targets': ['cpu']}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(ROOT / 'main.py'),
                '--hdc-bin', 'X:/SDK/hdc.exe', 'stress', 'run',
                '--plan', str(root / 'plan.json'), '--board', str(root / 'board.json'),
                '--campaign', str(root / 'campaign'), '--stage', '1', '--prepare', '--dry-run'],
                capture_output=True, text=True, encoding='utf-8', timeout=30, cwd=folder)
            self.assertEqual(result.returncode, 0, result.stderr)
            for command in ('01-validate', '02-deploy', '03-verify-deployment', '04-run', '05-collect'):
                self.assertIn(command, result.stdout)
            self.assertIn('X:/SDK/hdc.exe', result.stdout)
            self.assertIn('/dev/test-uart', result.stdout)
            self.assertFalse((root / 'campaign').exists())

    def test_stage_outside_plan_fails_before_creating_campaign(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'p.json').write_text(json.dumps(self.plan()), encoding='utf-8')
            (root / 'b.json').write_text(json.dumps({'board_id': 'B', 'device': 'x', 'pc_serial': 'x', 'platform': 'kirin9030', 'available_targets': ['cpu']}), encoding='utf-8')
            args = SimpleNamespace(plan=root / 'p.json', board=root / 'b.json', campaign=root / 'campaign', day=2)
            with self.assertRaisesRegex(ValueError, '阶段编号'):
                stress.Runner(args)
            self.assertFalse((root / 'campaign').exists())

    def test_summary_uses_saved_plan_stages_and_keeps_missing_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshot = root / 'configuration' / 'B'
            snapshot.mkdir(parents=True)
            (snapshot / 'plan.json').write_text(json.dumps(self.plan()), encoding='utf-8')
            result = stress.summarize(root)
            self.assertEqual(len(result['days']), 1)
            self.assertIsNone(result['days'][0]['accepted_load_s'])
            report = (root / 'summary' / '压测汇总.md').read_text(encoding='utf-8')
            self.assertIn('阶段', report)
            self.assertNotIn('七天', report)
            self.assertTrue((root / 'summary' / 'stress-summary.json').exists())


if __name__ == '__main__':
    unittest.main()
