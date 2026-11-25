"""Build the Windows release from this checkout and an installed 2.3.1.2 client."""
import argparse
import hashlib
import os
from importlib import metadata
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('oh2_replay_compat', 'oh2_fleet', 'oh2_fleet_animation', 'oh2_fleet_hud',
           'oh2_fleet_radio', 'oh2_fleet_status')


def build(game, output):
    output.mkdir(parents=True, exist_ok=True)
    helper = Path('mods/offhangar-fleet')
    mods = Path('mods/2.3.1.2')
    # Use an empty stage on every build so runtime settings or private test data cannot enter the ZIP.
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(dir=output) as temporary:
        temp = Path(temporary)
        stage = temp / 'release'
        (stage / helper).mkdir(parents=True)
        shutil.copyfile(ROOT/'templates/bootstrap.dat', stage/helper/'bootstrap.dat')
        for name in MODULES:
            subprocess.run([sys.executable, ROOT/'tools/build_replay_compat.py', game, name, stage/mods], check=True)
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--noupx', '--onefile',
                        '--windowed', '--name', 'export', '--specpath', temp, '--workpath', temp/'build',
                        '--distpath', stage/helper, ROOT/'src/fleet_export.py'], cwd=ROOT/'src', check=True)
        csc = Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        subprocess.run([csc, '/nologo', '/target:winexe', '/codepage:65001',
                        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
                        '/out:'+str(stage/helper/'FleetReplaySettings.exe'), ROOT/'src/FleetReplaySettings.cs'], check=True)
        for name in ('README.md', 'README.en.md', 'RELEASE.md', 'Install.ps1'):
            shutil.copyfile(ROOT/name, stage/name)
        notices = stage/'third-party'
        notices.mkdir()
        shutil.copyfile(Path(sys.base_prefix)/'LICENSE.txt', notices/'Python-LICENSE.txt')
        for dist_name in ('pycryptodome', 'pyinstaller'):
            dist = metadata.distribution(dist_name)
            for member in dist.files or ():
                if '.dist-info/licenses/' in member.as_posix():
                    dest = notices/dist_name/member.as_posix().split('.dist-info/licenses/', 1)[1]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(dist.locate_file(member), dest)
        archive = output/'offhangar-replay-2.3.1.2-win64.zip'
        with ZipFile(archive, 'w', ZIP_DEFLATED) as package:
            for member in sorted(stage.rglob('*')):
                if member.is_file():
                    package.write(member, member.relative_to(stage))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output/'SHA256SUMS.txt').write_text(digest+'  '+archive.name+'\n', encoding='ascii')
    print(archive)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT/'build')
    args = parser.parse_args()
    build(args.game.resolve(), args.output.resolve())
