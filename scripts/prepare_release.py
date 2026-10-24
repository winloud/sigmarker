"""Collect installed dependency notices and cache exact upstream source archives.

Run with the locked build environment. No package setup/build scripts are executed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sys
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ('PyQt6', 'PyQt6-Qt6', 'PyQt6-sip', 'Pillow', 'numpy', 'PyMuPDF',
           'opencv-python', 'pypdf', 'qrcode', 'pi-heif', 'colorama', 'typing_extensions')


def fetch(url):
    with urlopen(url, timeout=60) as response:
        return response.read()


def collect_notices():
    folder = ROOT / 'licenses'
    folder.mkdir(exist_ok=True)
    for identifier in ('AGPL-3.0-only', 'GPL-3.0-only', 'LGPL-3.0-only', 'LGPL-2.1-only'):
        path = folder / (identifier + '.txt')
        if not path.exists():
            path.write_bytes(fetch('https://raw.githubusercontent.com/spdx/license-list-data/'
                                   f'main/text/{identifier}.txt'))
    shutil.copy2(folder / 'AGPL-3.0-only.txt', ROOT / 'LICENSE')
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', folder / 'Python-LICENSE.txt')
    for name in (*RUNTIME, 'pyinstaller'):
        distribution = metadata.distribution(name)
        target = folder / name
        target.mkdir(exist_ok=True)
        copied = set()
        for path in distribution.files or ():
            text = str(path).lower()
            if any(word in text for word in ('license', 'copying', 'notice', 'copyright')):
                source = distribution.locate_file(path)
                if source.is_file() and source.suffix not in ('.py', '.pyc'):
                    destination = target / source.name
                    if destination.name in copied:
                        if destination.read_bytes() == source.read_bytes():
                            continue
                        destination = target / (source.parent.name + '-' + source.name)
                    shutil.copy2(source, destination)
                    copied.add(destination.name)
        if not copied:
            raise RuntimeError(f'No license found: {name}')
    runtime = [f'{name}=={metadata.version(name)}' for name in RUNTIME]
    (ROOT / 'requirements-lock.txt').write_text(
        '# V0.1.0 Windows x64 runtime; CPython 3.10.7\n' + '\n'.join(runtime) + '\n', encoding='utf-8')
    runtime_names = {name.lower().replace('_', '-') for name in RUNTIME}
    build = sorted(f'{d.metadata["Name"]}=={d.version}' for d in metadata.distributions()
                   if d.metadata['Name'].lower().replace('_', '-') not in runtime_names)
    (ROOT / 'requirements-build.txt').write_text(
        '# V0.1.0 tested build environment\n-r requirements-lock.txt\n' + '\n'.join(build) + '\n', encoding='utf-8')


def prepare_sources():
    manifest_path = ROOT / 'third_party_sources.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    else:
        manifest = []
        for name in (*RUNTIME, 'pyinstaller'):
            if name == 'PyQt6-Qt6':
                continue
            version = metadata.version(name)
            info = json.loads(fetch(f'https://pypi.org/pypi/{name}/{version}/json'))
            source = next(item for item in info['urls'] if item['packagetype'] == 'sdist')
            manifest.append(dict(name=name, version=version, filename=source['filename'],
                                 url=source['url'], sha256=source['digests']['sha256']))
        manifest.extend([
            dict(name='CPython', version='3.10.7', filename='Python-3.10.7.tar.xz',
                 url='https://www.python.org/ftp/python/3.10.7/Python-3.10.7.tar.xz'),
            *[dict(name=module, version='6.11.2', filename=f'{module}-everywhere-src-6.11.2.tar.xz',
                   url=f'https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/{module}-everywhere-src-6.11.2.tar.xz',
                   sha256=fetch(f'https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/{module}-everywhere-src-6.11.2.tar.xz.sha256').decode().split()[0])
              for module in ('qtbase', 'qtsvg')],
            dict(name='libheif', version='1.23.0', filename='libheif-1.23.0.tar.gz',
                 url='https://codeload.github.com/strukturag/libheif/tar.gz/refs/tags/v1.23.0'),
            dict(name='libde265', version='1.1.1', filename='libde265-1.1.1.tar.gz',
                 url='https://codeload.github.com/strukturag/libde265/tar.gz/refs/tags/v1.1.1'),
            dict(name='MuPDF', version='1.24.11', filename='mupdf-1.24.11-source.tar.gz',
                 url='https://mupdf.com/downloads/archive/mupdf-1.24.11-source.tar.gz'),
            dict(name='OpenCV-FFmpeg-build', version='394dca6ceb3085c979415e6385996b6570e94153',
                 filename='opencv-ffmpeg-build-394dca6.tar.gz',
                 url='https://codeload.github.com/opencv/opencv_3rdparty/tar.gz/394dca6ceb3085c979415e6385996b6570e94153'),
            *[dict(name=name, version=version, filename=f'{name}-{version}.tar.gz',
                   url=f'https://codeload.github.com/{repo}/tar.gz/refs/tags/{tag}')
              for name, version, repo, tag in (
                  ('FFmpeg', '4.4.4', 'FFmpeg/FFmpeg', 'n4.4.4'),
                  ('libvpx', '1.14.0', 'webmproject/libvpx', 'v1.14.0'),
                  ('openh264', '1.8.0', 'cisco/openh264', 'v1.8.0'))],
            dict(name='libaom', version='3.9.0', filename='libaom-3.9.0.tar.gz',
                 url='https://aomedia.googlesource.com/aom/+archive/refs/tags/v3.9.0.tar.gz'),
        ])
    cache = ROOT / '.verification' / 'release-sources'
    cache.mkdir(parents=True, exist_ok=True)

    def download(item):
        path = cache / item['filename']
        if not path.exists():
            temporary = path.with_suffix(path.suffix + '.part')
            temporary.write_bytes(fetch(item['url']))
            temporary.replace(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if item.get('sha256') and digest != item['sha256']:
            raise RuntimeError(f'Source checksum mismatch: {item["filename"]}')
        item['sha256'] = digest
        print(f'{item["filename"]}: {path.stat().st_size} bytes', flush=True)
        return item

    with ThreadPoolExecutor(max_workers=4) as executor:
        manifest = list(executor.map(download, manifest))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def collect_native_notices():
    manifest = json.loads((ROOT / 'third_party_sources.json').read_text(encoding='utf-8'))
    for item in manifest:
        if item['name'] not in ('qtbase', 'qtsvg', 'MuPDF', 'libheif', 'libde265'):
            continue
        with tarfile.open(ROOT / '.verification' / 'release-sources' / item['filename']) as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                name = path.name.lower()
                if not member.isfile() or path.is_absolute() or '..' in path.parts:
                    continue
                if (name.startswith(('license', 'copying', 'notice', 'copyright'))
                        or name == 'qt_attribution.json') and path.suffix.lower() in ('', '.txt', '.md', '.json'):
                    target = ROOT / 'licenses' / (item['name'] + '-native') / Path(*path.parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', action='store_true')
    args = parser.parse_args()
    collect_notices()
    if args.sources:
        prepare_sources()
        collect_native_notices()
