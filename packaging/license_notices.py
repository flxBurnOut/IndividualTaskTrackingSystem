"""Generate notices from the actual PyInstaller COLLECT/PYZ inputs.

No application data or user configuration is read. Installed license texts are
copied verbatim; missing Qt texts come from pinned official Qt source snapshots.
Downloads are bounded, verified and cached under .build, never in user data.
This records upstream terms, not a claim that all dependencies use one license.
"""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build' / 'pyinstaller' / 'personal_management'
CACHE = ROOT / '.build' / 'third-party-license-cache'
MAX_DOWNLOAD = 12 * 1024 * 1024
QT_VERSION = '6.11.2'
# Tags were resolved against the official Qt repositories, not third-party mirrors.
QT_SOURCES = {
    'qtbase': 'ef55f427f2c8b410d34f8a7681020a3000cf6866',
    'qtsvg': '17ca512f903f935282ebeca496aac5d11ba4199a',
    'qtimageformats': '47b6139dda3b84d1d3ec15caf8d04eff8d744c8d',
    'qtdeclarative': '4e3399c26ec57246c08de019cfcbda8d23604cfa',
    'qtwebengine': 'a33fa2a897e5ee58e385b3f88dc247d99fca56db',
}
QT_CHROMIUM_COMMIT = '5170777d28bee1ce92cc693a0dbf2ad01492e5cf'
QT_PDFIUM_TREE = 'a73e60360d4a1b21df9b41b59d46db1ab489d42a'
QT_LICENSES = {
    'LGPL-3.0-only.txt': 'da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768',
    'GPL-3.0-only.txt': '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903',
    'GPL-2.0-only.txt': '8177f97513213526df2cf6184d8ff986c675afb514d4e68a404010521b880643',
}


def _key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _entries(value):
    if isinstance(value, (tuple, list)):
        if (len(value) == 3 and all(isinstance(v, str) for v in value)
                and value[2] in {'BINARY', 'DATA', 'EXTENSION', 'PYMODULE', 'PYSOURCE'}):
            yield value
        for child in value:
            yield from _entries(child)


def _build_inputs(package):
    collect = BUILD / 'COLLECT-00.toc'
    pyz = sorted(BUILD.glob('PYZ-*.toc'))
    if not collect.is_file() or not pyz:
        raise RuntimeError('Build COLLECT/PYZ manifests before generating notices')
    sources, binaries = set(), []
    for path in (collect, *pyz):
        for name, original, kind in _entries(ast.literal_eval(path.read_text('utf-8'))):
            if path == collect:
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise RuntimeError('Unexpected collected destination path')
                # The final package, not an analysis-only dependency, is authoritative.
                if not (package / relative).is_file() and not (package / '_internal' / relative).is_file():
                    continue
                if kind in {'BINARY', 'EXTENSION'}:
                    binaries.append(relative.as_posix())
            if Path(original).is_file():
                sources.add(_key(original))
    if not sources or not binaries:
        raise RuntimeError('No runtime dependency inputs were found')
    return sources, sorted(set(binaries))


def _license_file(path):
    name = path.name.lower()
    if path.suffix.lower() in {'.py', '.pyc', '.pyd', '.dll', '.exe', '.so'}:
        return False
    return (any(part.lower() == 'licenses' for part in path.parts)
            or bool(re.search(r'(?:^|[-_.])(licen[sc]e|copying|copyright|notice|authors)(?:$|[-_.])', name))
            or name in {'ftl.txt', 'gplv2.txt'})


def _put(folder, relative, content):
    relative = PurePosixPath(str(relative).replace('\\', '/'))
    if relative.is_absolute() or '..' in relative.parts or ':' in str(relative):
        raise RuntimeError('Invalid notice destination')
    destination = folder.joinpath(*relative.parts)
    if not destination.resolve().is_relative_to(folder.resolve()):
        raise RuntimeError('Notice destination escaped its package')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return relative.as_posix()


def _fetch(url, *, sha256=None, git_blob=None):
    if not url.startswith(('https://raw.githubusercontent.com/qt/', 'https://api.github.com/repos/qt/')):
        raise RuntimeError('Only official Qt source URLs are allowed')
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (hashlib.sha256(url.encode()).hexdigest() + '.cache')
    if path.is_file():
        content = path.read_bytes()
    else:
        request = urllib.request.Request(url, headers={'User-Agent': 'PersonalManagement-LicenseNotices/1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(MAX_DOWNLOAD + 1)
        if not content or len(content) > MAX_DOWNLOAD:
            raise RuntimeError('Upstream license response is empty or too large')
    if sha256 and hashlib.sha256(content).hexdigest() != sha256:
        raise RuntimeError('Pinned upstream license SHA256 mismatch')
    if git_blob and hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest() != git_blob:
        raise RuntimeError('Pinned upstream license Git object mismatch')
    path.write_bytes(content)
    return content


def _qt_notices(folder, binaries, versions):
    names = {PurePosixPath(p).name.lower() for p in binaries}
    if {'qt6virtualkeyboard.dll', 'qtvirtualkeyboardplugin.dll'} & names:
        raise RuntimeError('Qt Virtual Keyboard needs a separately reviewed GPL/commercial release decision')
    if not any(n.startswith('qt6') for n in names):
        return None
    if any(v != QT_VERSION for name, v in versions.items() if name.lower().startswith(('pyside6', 'shiboken6'))):
        raise RuntimeError('Review pinned Qt source/license snapshots for this new Qt version')
    base = 'https://raw.githubusercontent.com/qt/qtbase/' + QT_SOURCES['qtbase'] + '/LICENSES/'
    for name, digest in QT_LICENSES.items():
        _put(folder, 'Qt-' + QT_VERSION + '/LICENSES/' + name, _fetch(base + name, sha256=digest))
    repositories = ['qtbase']
    if 'qt6svg.dll' in names:
        repositories.append('qtsvg')
    if any('imageformats/' in p.replace('\\', '/').lower() for p in binaries):
        repositories.append('qtimageformats')
    if any(n.startswith(('qt6qml', 'qt6quick')) for n in names):
        repositories.append('qtdeclarative')
    if 'qt6pdf.dll' in names:
        repositories.append('qtwebengine')
    records = []
    for repo in repositories:
        revision = QT_SOURCES[repo]
        tree = json.loads(_fetch(f'https://api.github.com/repos/qt/{repo}/git/trees/{revision}?recursive=1'))
        if tree.get('truncated') or tree.get('sha') != revision:
            raise RuntimeError('Incomplete or mismatched official Qt source tree')
        selected = [r for r in tree['tree'] if r.get('type') == 'blob'
                    and (r['path'].startswith('LICENSES/')
                         or '/' not in r['path'] and _license_file(PurePosixPath(r['path']))
                         or r['path'].startswith('src/3rdparty/')
                         and (_license_file(PurePosixPath(r['path']))
                              or PurePosixPath(r['path']).name == 'qt_attribution.json'))
                    and not r['path'].endswith(('.c', '.h', '.cpp', '.pl', '.py', '.sh'))]
        def fetch(record):
            url = f'https://raw.githubusercontent.com/qt/{repo}/{revision}/' + record['path']
            content = _fetch(url, git_blob=record['sha'])
            relative = _put(folder, 'Qt-' + QT_VERSION + '/' + repo + '/' + record['path'], content)
            return {'file': relative, 'source': url, 'sha256': hashlib.sha256(content).hexdigest()}
        with ThreadPoolExecutor(max_workers=6) as pool:
            records.extend(pool.map(fetch, selected))
    if 'qt6pdf.dll' in names:
        # QtPdf builds PDFium from qtwebengine's pinned Chromium submodule.
        # A parent-tree recursive listing does not descend into this gitlink.
        tree = json.loads(_fetch('https://api.github.com/repos/qt/qtwebengine-chromium/git/trees/'
                                 + QT_PDFIUM_TREE + '?recursive=1'))
        if tree.get('truncated') or tree.get('sha') != QT_PDFIUM_TREE:
            raise RuntimeError('Incomplete or mismatched Qt PDFium source tree')
        pdf_licenses = [r for r in tree['tree'] if r.get('type') == 'blob'
                        and _license_file(PurePosixPath(r['path']))]
        if not any(r['path'] == 'LICENSE' for r in pdf_licenses):
            raise RuntimeError('The Qt PDFium root license is missing')
        for record in pdf_licenses:
            relative = 'chromium/third_party/pdfium/' + record['path']
            url = ('https://raw.githubusercontent.com/qt/qtwebengine-chromium/'
                   + QT_CHROMIUM_COMMIT + '/' + relative)
            content = _fetch(url, git_blob=record['sha'])
            dest = _put(folder, 'Qt-' + QT_VERSION + '/qtwebengine-chromium/' + relative, content)
            records.append({'file': dest, 'source': url, 'sha256': hashlib.sha256(content).hexdigest()})
    source = f'https://download.qt.io/official_releases/qt/6.11/{QT_VERSION}/single/'
    _put(folder, 'Qt-' + QT_VERSION + '/SOURCE_AND_REPLACEMENT.md', (
        '# Qt / Qt for Python\n\n'
        f'Installed distribution version: {QT_VERSION}. Qt libraries are dynamically loaded DLLs, '
        'distributed separately from the application in `_internal/PySide6/`.\n\n'
        'The PySide6/shiboken wheel metadata declares LGPL-3.0-only OR GPL-2.0-only OR '
        'GPL-3.0-only; the wheel also includes a Qt commercial-license reference. These '
        'are upstream alternatives, not a claim that a commercial license is required '
        'or that every Qt component has identical terms. Preserve the supplied texts.\n\n'
        f'Qt corresponding upstream source archives: {source}\n\n'
        f'Qt for Python source: https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v{QT_VERSION}\n\n'
        'Qt module source and license provenance are recorded in `UPSTREAM_FILES.json`. '
        'Supplementary third-party notices include the named modules\' upstream license '
        'and attribution files conservatively; they do not assert every optional '
        'platform-specific dependency was compiled into these Windows DLLs.\n\n'
        'When QtPdf is present, the QtWebEngine module license texts and its exact '
        'PDFium/third-party license subtree are included. PDFium source revision: '
        f'https://github.com/qt/qtwebengine-chromium/tree/{QT_CHROMIUM_COMMIT}/chromium/third_party/pdfium\n\n'
        'Users may replace the dynamically loaded Qt/PySide libraries with compatible '
        'modified versions for debugging modifications, subject to their licenses. '
        'No application signature check locks these libraries. Keep the originals and '
        'use matching Python/Qt ABI and architectures; compatibility is not guaranteed. '
        'Application source/build instructions are distributed in the project repository.\n'
    ).encode('utf-8'))
    _put(folder, 'Qt-' + QT_VERSION + '/UPSTREAM_FILES.json', json.dumps(records, indent=2).encode())
    return {'version': QT_VERSION, 'libraries': sorted(n for n in names if n.startswith('qt6')),
            'source_archives': source, 'upstream_repositories': repositories,
            'supplemental_files': len(records)}


def write_notices(package: Path):
    """Write package-local notices; fail rather than silently omit a license.

    Call after PyInstaller COLLECT and before packaging/signing. Returns counts
    only; public output never contains local source paths, usernames or settings.
    """
    package = Path(package).resolve(strict=True)
    if not package.is_dir() or not (package / 'PersonalManagement.exe').is_file():
        raise RuntimeError('Expected a completed PersonalManagement package directory')
    sources, binaries = _build_inputs(package)
    folder = package / 'THIRD_PARTY_LICENSES'
    if folder.exists():
        raise RuntimeError('Notice output already exists; use a fresh package build')
    folder.mkdir()
    inventory, versions = [], {}
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata.get('Name', '').lower()):
        name, version = dist.metadata.get('Name'), dist.version
        if not name:
            continue
        files = list(dist.files or [])
        used = any(_key(dist.locate_file(p)) in sources for p in files)
        if not used and name.lower() != 'pyinstaller':
            continue
        slug = re.sub(r'[^A-Za-z0-9_.-]', '_', name + '-' + version)
        copies = []
        for item in sorted(files, key=str):
            if not _license_file(PurePosixPath(str(item))):
                continue
            path = Path(dist.locate_file(item))
            if not path.is_file():
                continue
            relative = PurePosixPath(str(item).replace('\\', '/'))
            if '..' in relative.parts:
                continue
            content = path.read_bytes()
            if len(content) > 4 * 1024 * 1024:
                raise RuntimeError('Unexpectedly large installed license text: ' + name)
            dest = _put(folder, slug + '/' + relative.as_posix(), content)
            copies.append({'file': dest, 'sha256': hashlib.sha256(content).hexdigest()})
        if not copies:
            raise RuntimeError('Missing installed license text for runtime dependency: ' + name)
        versions[name] = version
        inventory.append({'name': name, 'version': version,
                          'declared_license': dist.metadata.get('License-Expression') or dist.metadata.get('License')
                                              or 'See the supplied upstream license text',
                          'role': 'bootloader and bundled runtime hooks' if name.lower() == 'pyinstaller'
                                  else 'bundled runtime distribution (possibly a subset)',
                          'license_files': copies})
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise RuntimeError('The bundled Python runtime LICENSE.txt is missing')
    python_version = '.'.join(map(str, sys.version_info[:3]))
    _put(folder, 'Python-' + python_version + '/LICENSE.txt', python_license.read_bytes())
    qt = _qt_notices(folder, binaries, versions)
    summary = {'python_version': python_version, 'distributions': inventory,
               'qt': qt, 'bundled_native_files': binaries}
    _put(folder, 'INVENTORY.json', json.dumps(summary, ensure_ascii=False, indent=2).encode('utf-8'))
    lines = ['# Third-party notices', '',
             'This distribution contains third-party software. License and copyright texts are '
             'reproduced in `THIRD_PARTY_LICENSES/`; their terms remain with their respective owners. '
             'This inventory does not relicense them or choose an application license.', '',
             'The inventory is generated from the actual PyInstaller COLLECT/PYZ inputs and installed '
             'distribution metadata. Some distributions are only partly bundled; supplementary '
             'upstream notices may cover components not used on this platform. No business data, '
             'account settings or development-machine paths are included.', '',
             f'Python runtime: **{python_version}**. Its license is in `THIRD_PARTY_LICENSES/Python-{python_version}/LICENSE.txt`. '
             f'Corresponding CPython source: https://www.python.org/downloads/release/python-{python_version.replace(".", "")}/', '',
             'PyInstaller supplies the executable bootloader and runtime hooks. Its original COPYING '
             'text includes the bootloader distribution exception; it is preserved, not replaced by '
             'a blanket claim that the application uses GPL.', '',
             '| Distribution | Version | Upstream declared license |', '|---|---|---|']
    for row in inventory:
        terms = ' '.join(row['declared_license'].split()).replace('|', '\\|')
        lines.append(f'| {row["name"]} | {row["version"]} | {terms} |')
    if qt:
        lines += ['', '## Qt', '',
                  'See `THIRD_PARTY_LICENSES/Qt-' + QT_VERSION + '/SOURCE_AND_REPLACEMENT.md` '
                  'for the source archives, dynamic-library replacement information and pinned '
                  'upstream license/attribution files. Actual Qt DLLs are listed in `INVENTORY.json`.']
    lines += ['', 'The software is provided under the applicable upstream terms and warranty '
              'disclaimers. The application\'s own license and release terms are separate.', '']
    (package / 'THIRD_PARTY_NOTICES.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'runtime_distributions': len(inventory), 'license_files': sum(1 for p in folder.rglob('*') if p.is_file()),
            'python_version': python_version, 'qt_version': qt['version'] if qt else None}
