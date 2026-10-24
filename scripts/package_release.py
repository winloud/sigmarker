"""Package a verified build and the exact clean Git HEAD for GitHub Release."""

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

ROOT = Path(__file__).resolve().parents[1]


def package(product, output):
    version = runpy.run_path(str(ROOT / 'sigmarker' / 'metadata.py'))['APP_VERSION']
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip():
        raise RuntimeError('Commit release inputs before packaging; Git HEAD must match the build.')
    if not (product / 'SigMarker.exe').is_file() or not (product / '_internal').is_dir():
        raise RuntimeError('Incomplete portable build')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    info = json.loads((product / 'build-info.json').read_text(encoding='utf-8'))
    if info != {'version': version, 'commit': commit, 'dirty': False}:
        raise RuntimeError('Rebuild from the current clean Git HEAD before packaging.')
    output.mkdir(parents=True, exist_ok=True)
    prefix = f'SigMarker-v{version}'
    binary = output / f'{prefix}-windows-x64.zip'
    with ZipFile(binary, 'w', ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(product.rglob('*')):
            if path.is_file():
                archive.write(path, 'SigMarker/' + path.relative_to(product).as_posix())
    source = output / f'{prefix}-source.zip'
    subprocess.run(['git', 'archive', '--format=zip', f'--prefix={prefix}/',
                    f'--output={source}', 'HEAD'], cwd=ROOT, check=True)
    manifest = json.loads((ROOT / 'third_party_sources.json').read_text(encoding='utf-8'))
    third_party = output / f'{prefix}-third-party-sources.zip'
    with ZipFile(third_party, 'w', ZIP_STORED) as archive:
        for item in manifest:
            path = ROOT / '.verification' / 'release-sources' / item['filename']
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise RuntimeError(f'Source checksum mismatch: {path.name}')
            archive.write(path, 'sources/' + path.name)
        for name in ('third_party_sources.json', 'SOURCE_CODE.md', 'THIRD_PARTY_NOTICES.md',
                     'docs/development.md', 'requirements-lock.txt', 'requirements-build.txt'):
            archive.write(ROOT / name, name)
    sums = []
    for path in (binary, source, third_party):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sums.append(f'{digest}  {path.name}')
        print(f'{path.name}: {path.stat().st_size} bytes; SHA256 {digest}', flush=True)
    (output / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n', encoding='ascii')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product', type=Path, default=ROOT / 'dist' / 'SigMarker')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'release')
    args = parser.parse_args()
    package(args.product.resolve(), args.output.resolve())
