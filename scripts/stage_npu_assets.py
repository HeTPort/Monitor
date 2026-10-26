"""Stage checked NPU model assets and an explicitly selected device executable."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.artifact_store import sha256_file
from src.config_loader import ProfileConfig
from src.target_adapters import get_target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload-root', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    args = parser.parse_args()
    source = args.workload_root.resolve(strict=True) / 'npuworkload'
    binary = args.binary.resolve(strict=True)
    profile = ProfileConfig.from_file(ROOT / 'config/profiles/npu_kws01_smoke.yaml')
    get_target('npu').check_binary(binary, profile)
    tools = ROOT / 'tools'
    destination = tools / 'npu'
    # Verify source content before copying anything.
    for model in ('kws01', 'ic01', 'ad01', 'sww01'):
        folder = source / 'profiles' / model
        manifest = json.loads((folder / 'manifest.json').read_text())
        asset_root = get_target('npu')._confined(folder, manifest['asset_root'], directory=True)
        for item in [manifest['model'], *manifest['inputs']]:
            asset = get_target('npu')._confined(asset_root, item['file'])
            if sha256_file(asset) != item['sha256']:
                raise ValueError(f'asset hash mismatch: {asset}')
    destination.mkdir(parents=True, exist_ok=True)
    for model in ('kws01', 'ic01', 'ad01', 'sww01'):
        shutil.copytree(source / 'profiles' / model, destination / 'profiles' / model, dirs_exist_ok=True)
    shutil.copytree(source / 'third_party', destination / 'third_party', dirs_exist_ok=True)
    shutil.copy2(binary, tools / 'npu-avs-workload')
    print('Staged AArch64 executable and four checked profiles. Supply vendor libraries separately.')


if __name__ == '__main__':
    main()
