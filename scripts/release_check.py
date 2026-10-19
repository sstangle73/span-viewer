#!/usr/bin/env python3
"""release_check.py — fail if a tree contains any value from a deny list.

For checking a release tree before it's published: your street, your panel's
serial, account numbers, private hostnames. The deny list stays outside the
repo, one entry per line:

    re:<regex>                 case-insensitive, matched against each text line
    sha256:<length>:<hex>      a secret you don't want written down in clear:
                               any run of <length> characters (letters, digits,
                               . _ - @ :) whose SHA-256 is <hex>
    allow:<path glob>:<regex>  a match of <regex> in a matching file is fine
    # comment

Make a hashed entry with `release_check.py --hash <value>` (it reads the value
from stdin when <value> is -, so it stays out of shell history).

Usage:
    python scripts/release_check.py --deny ~/span-viewer-denylist.txt <tree>

Exit 0 when nothing matches; 1 with one line per hit (file, line, entry), with
hashed hits shown by their entry, never their value. Binary files are skipped
by name and listed, so a screenshot can't slip past unseen.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import sys
from pathlib import Path

RUN = re.compile(r'[A-Za-z0-9._@:-]+')


def load(path: Path):
    regexes, hashes, allows = [], {}, []
    for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        kind, _, rest = line.partition(':')
        if kind == 're':
            regexes.append((line, re.compile(rest, re.I)))
        elif kind == 'sha256':
            length, _, digest = rest.partition(':')
            hashes.setdefault(int(length), {})[digest.lower()] = f'{path.name}:{n} (sha256)'
        elif kind == 'allow':
            glob, _, rx = rest.partition(':')
            allows.append((glob, re.compile(rx, re.I)))
        else:
            sys.exit(f'release_check: {path}:{n}: unknown entry kind {kind!r}')
    return regexes, hashes, allows


def hashed_hits(line: str, hashes: dict) -> list[str]:
    hits = []
    for run in RUN.findall(line):
        for length, digests in hashes.items():
            for i in range(len(run) - length + 1):
                d = hashlib.sha256(run[i:i + length].encode()).hexdigest()
                if d in digests:
                    hits.append(digests[d])
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--deny', type=Path, help='the deny list')
    ap.add_argument('--hash', metavar='VALUE', help='print a sha256 entry for VALUE (- reads stdin)')
    ap.add_argument('tree', nargs='?', type=Path)
    args = ap.parse_args()
    if args.hash is not None:
        v = sys.stdin.readline().rstrip('\r\n') if args.hash == '-' else args.hash
        print(f'sha256:{len(v)}:{hashlib.sha256(v.encode()).hexdigest()}')
        return 0
    if args.deny is None or args.tree is None:
        ap.error('--deny and a tree are required')
    regexes, hashes, allows = load(args.deny)

    hits, binary = [], []
    for f in sorted(p for p in args.tree.rglob('*') if p.is_file() and '.git' not in p.parts):
        rel = f.relative_to(args.tree).as_posix()
        try:
            text = f.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            binary.append(rel)
            continue
        ok = [rx for glob, rx in allows if fnmatch.fnmatch(rel, glob)]
        for n, line in enumerate(text.splitlines(), 1):
            for entry, rx in regexes:
                for m in rx.finditer(line):
                    if not any(a.search(m.group(0)) or a.search(line) for a in ok):
                        hits.append(f'{rel}:{n}: {entry}')
                        break
            hits.extend(f'{rel}:{n}: {h}' for h in hashed_hits(line, hashes))
        for entry, rx in regexes:       # a file's name can leak too
            if rx.search(rel):
                hits.append(f'{rel}: (path) {entry}')
    for b in binary:
        print(f'release_check: binary, not scanned: {b}')
    for h in hits:
        print(h)
    print(f'release_check: {len(hits)} hit(s), {len(regexes)} patterns, '
          f'{sum(len(d) for d in hashes.values())} hashed values')
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
