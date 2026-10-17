"""Build a Windows x64 ZIP from committed public files only.

Run with the project venv after installing requirements-build.txt.
Build output remains ignored under dist/; untracked files are never bundled.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

from release_entry import VERSION


def build(output):
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Build this release with 64-bit Python on Windows x64.')
    root = Path(__file__).resolve().parent
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    with tempfile.TemporaryDirectory(prefix='maple-release-', dir=output) as temporary:
        workspace = Path(temporary)
        archive = workspace / 'source.zip'
        subprocess.run(['git', 'archive', '--format=zip', '-o', str(archive), commit], cwd=root, check=True)
        source = workspace / 'source'
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(source)
        version_file = workspace / 'version.txt'
        version_info = (*map(int, VERSION.split('.')), 0)
        version_file.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={version_info!r}, prodvers={version_info!r}, mask=0x3f,
                   flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'MapleStory EXP Tracker'),
    StringStruct('FileVersion', '{VERSION}'),
    StringStruct('ProductName', 'MapleStory EXP Tracker'),
    StringStruct('ProductVersion', '{VERSION}'),
    StringStruct('OriginalFilename', 'MapleExpTracker.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033,1200])])])
""", encoding='utf-8')
        # RapidOCR 1.2.3 loads these top-level modules by name from config.yaml.
        # Static import analysis and collecting model data alone cannot find them.
        ocr_path = importlib.metadata.distribution('rapidocr-onnxruntime').locate_file('rapidocr_onnxruntime')
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                   '--onedir', '--windowed', '--name', 'MapleExpTracker',
                   '--distpath', str(workspace / 'bundle'),
                   '--workpath', str(workspace / 'work'), '--specpath', str(workspace),
                   '--paths', str(source), '--hidden-import', 'orb',
                   '--paths', str(ocr_path),
                   '--hidden-import', 'ch_ppocr_v3_rec',
                   '--hidden-import', 'ch_ppocr_v3_det',
                   '--hidden-import', 'ch_ppocr_v2_cls',
                   '--collect-data', 'rapidocr_onnxruntime',
                   '--collect-binaries', 'onnxruntime', '--collect-submodules', 'dxcam',
                   '--add-data', f'{source / "assets"}:assets',
                   '--icon', str(source / 'assets/maple-exp-icon-v1.ico'),
                   '--version-file', str(version_file), str(source / 'release_entry.py')]
        subprocess.run(command, cwd=source, check=True)
        app = workspace / 'bundle' / 'MapleExpTracker'
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(('PYTHON', 'CONDA', 'VIRTUAL_ENV', 'MAPLE_EXP_'))}
        environment['PATH'] = str(Path(environment['WINDIR']) / 'System32')
        smoke_report = workspace / 'self-test.json'
        smoke = subprocess.run([str(app / 'MapleExpTracker.exe'), '--self-test-report', str(smoke_report)],
                               cwd=workspace, env=environment, timeout=60,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(smoke_report.read_text(encoding='utf-8'))
        if smoke.returncode or result.get('status') != 'passed' or not result.get('frozen'):
            raise RuntimeError(f'Frozen runtime self-test failed: {result}')
        shutil.copyfile(source / 'README.md', app / 'README.md')
        shutil.copyfile(source / 'assets/maple-exp-icon-v1.ico', app / 'maple-exp-icon-v1.ico')
        shutil.copyfile(smoke_report, output / 'self-test.json')
        licenses = app / 'THIRD_PARTY_LICENSES'
        licenses.mkdir()
        packages = {}
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata['Name']
            if name.lower() in {'pip', 'setuptools', 'pyinstaller', 'pyinstaller-hooks-contrib', 'pefile', 'altgraph', 'pywin32-ctypes'}:
                continue
            packages[name] = distribution.version
            for item in distribution.files or []:
                is_notice = item.name.lower().startswith(('license', 'copying', 'notice'))
                in_metadata = any(part.endswith('.dist-info') for part in item.parts)
                if (is_notice or (in_metadata and 'licenses' in item.parts)) and item.suffix.lower() not in {'.py', '.pyc', '.pyo'}:
                    original = Path(distribution.locate_file(item))
                    if original.is_file():
                        target = licenses / name / str(item).replace('/', '__').replace('\\', '__')
                        target.parent.mkdir(exist_ok=True)
                        shutil.copyfile(original, target)
        python_license = Path(sys.base_prefix) / 'LICENSE.txt'
        if python_license.is_file():
            shutil.copyfile(python_license, licenses / 'Python-LICENSE.txt')
        (app / 'BUILD.json').write_text(json.dumps(dict(version=VERSION, commit=commit,
            python=platform.python_version(), platform='windows-x64', dependencies=packages),
            indent=2, sort_keys=True), encoding='utf-8')
        destination = output / f'MapleExpTracker-{VERSION}-windows-x64.zip'
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(app.rglob('*')):
                if path.is_file():
                    bundle.write(path, path.relative_to(app.parent))
        with destination.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        (output / 'SHA256SUMS.txt').write_text(f'{digest}  {destination.name}\n', encoding='utf-8')
        print(json.dumps({'file': str(destination), 'sha256': digest, 'bytes': destination.stat().st_size}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'dist')
    build(parser.parse_args().output)
