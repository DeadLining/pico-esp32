#!/usr/bin/env python3
"""Export an IDF build as explicit USB segments for the Pico browser flasher.

No guessed offsets, no merged-image flashing, no credentials in the manifest.
Only the verified project/board/target combination is supported initially.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


def export_package(build_dir: Path, destination: Path) -> dict:
    build_dir = build_dir.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(build_dir):
        raise ValueError('Export destination must be outside the build directory')
    description = json.loads((build_dir / 'project_description.json').read_text())
    if description.get('project_name') != 'pico' or description.get('target') != 'esp32c3':
        raise ValueError('Requires a Pico ESP32-C3 build')
    config = json.loads((build_dir / 'config/sdkconfig.json').read_text())
    if not config.get('BOARD_TYPE_FOLOTOY_AI_PASSPORT'):
        raise ValueError('Requires FoloToy AI Passport sdkconfig')
    if not config.get('ESPTOOLPY_FLASHSIZE_8MB'):
        raise ValueError('Requires 8MB flash configuration')
    args = json.loads((build_dir / 'flasher_args.json').read_text())
    source_files = args.get('flash_files', {})
    if not isinstance(source_files, dict) or not 1 <= len(source_files) <= 16:
        raise ValueError('Missing or invalid IDF flash_files')
    parts = []; sources = []; names = set(); ranges = []
    for offset, relative in source_files.items():
        source = (build_dir / relative).resolve()
        if not source.is_relative_to(build_dir) or not source.is_file():
            raise ValueError('Firmware file must be inside build directory')
        name = source.name
        if not re.fullmatch(r'[a-zA-Z0-9_-]+\.bin', name) or name in names:
            raise ValueError('Invalid or duplicate firmware basename')
        names.add(name)
        address = int(offset, 0); data = source.read_bytes(); size = len(data)
        if address < 0 or address % 4096 or size < 1 or address + size > 8 * 1024 * 1024:
            raise ValueError('Invalid flash region')
        parts.append({'name': name, 'address': address, 'size': size, 'sha256': hashlib.sha256(data).hexdigest()})
        sources.append(source); ranges.append((address, address + ((size + 4095) // 4096) * 4096))
    ranges.sort()
    if any(ranges[i][0] < ranges[i-1][1] for i in range(1, len(ranges))):
        raise ValueError('Overlapping flash erase regions')
    version = description.get('project_version')
    if not isinstance(version, str) or not 1 <= len(version) <= 80:
        raise ValueError('Missing project version')
    manifest = {'schema': 1, 'product': 'Pico', 'board': 'folotoy/ai-passport', 'chip': 'ESP32-C3', 'flashSize': 8388608, 'version': version, 'files': parts}
    # Refuse replacement of an existing package or accidental writes into the build.
    destination.mkdir(parents=True, exist_ok=False)
    for source in sources:
        shutil.copyfile(source, destination / source.name)
    (destination / 'pico-firmware.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=Path('build'))
    parser.add_argument('--output', type=Path, required=True)
    opts = parser.parse_args()
    export_package(opts.build_dir, opts.output)
    print(f'Pico USB firmware package: {opts.output}')
