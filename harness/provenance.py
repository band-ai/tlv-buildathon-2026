"""Pin build inputs and identify the exact conformance sources used."""
import hashlib
import pathlib
import subprocess

from harness.challenges import challenge_dir

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGE_DIRS = {'1': 'stage_1', '2': 'stage_2', '3': 'stage_3', '4': 'stage_4'}
RUN_SOURCES = ('__init__.py', '__main__.py', 'cli.py', 'docker_driver.py', 'plugin.py',
               'concurrent.py', 'http.py', 'report.py', 'provenance.py', 'challenges.py',
               'mockweb.py', 'control.py', 'Dockerfile', 'requirements.txt')


def git(repo, *args):
    """Run git in `repo` and return its stripped stdout. Raises ValueError on failure."""
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            text=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'git command failed')
    return result.stdout.strip()


def suite_digest(track, stages):
    """Digest relative names and bytes, independent of checkout path and mtimes.

    Records which suite bytes a run used. It hashes the LOCAL checkout, so it does not
    detect edited tests: a team that edits a suite gets a digest matching their edit.
    Tampering is caught by judges running their own pristine harness against the
    submitted stage folders.
    """
    # Only the sources that decide a run are pinned. Tools that act before or after a
    # run cannot change what it decided, and pinning them would invalidate reports
    # whenever one is fixed; leaving them out also gives every package that runs the
    # checks the same digest as the source tree.
    files = {ROOT / 'harness' / name for name in RUN_SOURCES
             if (ROOT / 'harness' / name).is_file()}
    tests = challenge_dir(track) / 'test'
    files.update(tests.glob('*.py'))
    for stage in stages:
        files.update((tests / STAGE_DIRS[stage]).rglob('*.py'))
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(ROOT).as_posix().encode() + b'\0')
        digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()
