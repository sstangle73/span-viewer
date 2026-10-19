"""scripts/release_check.py: the deny-list scan run on every public release tree."""

import hashlib
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'release_check.py'


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def test_regex_hash_and_allow(tmp_path):
    hidden = 'my-old-topic'
    deny = tmp_path / 'deny.txt'
    deny.write_text('re:192\\.0\\.2\\.\nre:\\bAda\\b\nallow:LICENSE:Ada Lovelace\n'
                    f'sha256:{len(hidden)}:{hashlib.sha256(hidden.encode()).hexdigest()}\n')
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / 'LICENSE').write_text('Copyright (c) Ada Lovelace\n')
    (tree / 'clean.py').write_text('HOST = "example.lan"\n')
    assert run('--deny', deny, tree).returncode == 0

    (tree / 'leak.py').write_text(f'URL = "https://ntfy.sh/{hidden}"  # 192.0.2.7\nwho = "Ada"\n')
    r = run('--deny', deny, tree)
    assert r.returncode == 1
    assert 'leak.py:1: re:192' in r.stdout
    assert 'leak.py:1: deny.txt:4 (sha256)' in r.stdout
    assert 'leak.py:2: re:\\bAda\\b' in r.stdout
    assert hidden not in r.stdout                   # a hashed hit never prints its value
    assert 'LICENSE' not in r.stdout


def test_hash_entry_from_stdin():
    r = subprocess.run([sys.executable, str(SCRIPT), '--hash', '-'], input='abc\n',
                       capture_output=True, text=True)
    assert r.stdout.strip() == f'sha256:3:{hashlib.sha256(b"abc").hexdigest()}'
