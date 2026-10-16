"""Check the explicit public manifest and, optionally, the actual Git index."""
from pathlib import Path, PurePosixPath
import argparse
import csv
import hashlib
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'docs/phase6_release_manifest.csv'
SELF_FILES = {MANIFEST, 'docs/phase6_changes.csv'}
SUFFIXES = {'.py', '.md', '.csv', '.json', '.patch', '.txt'}


def git(*args):
    completed = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, check=True)
    return completed.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-index', action='store_true', help='Require an exact, byte-identical staged whitelist and no remote.')
    args = parser.parse_args(argv)
    errors = []
    with (ROOT / MANIFEST).open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    names = [row['path'] for row in rows]
    if len(set(names)) != len(names):
        errors.append('Duplicate manifest paths')
    for row in rows:
        name = row['path']
        relative = PurePosixPath(name)
        path = ROOT / name
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or not name.isascii() or not path.resolve().is_relative_to(ROOT)
                or path.is_symlink()):
            errors.append('Unsafe path: ' + name)
            continue
        if not path.is_file():
            errors.append('Missing file: ' + name)
            continue
        if path.suffix not in SUFFIXES and name not in {'.gitignore', '.gitattributes'}:
            errors.append('Forbidden file type: ' + name)
        data = path.read_bytes()
        try:
            text = data.decode('utf-8-sig')
            if '\0' in text:
                errors.append('Binary content: ' + name)
        except UnicodeDecodeError:
            errors.append('Non-UTF-8 content: ' + name)
        if len(data) != int(row['size_bytes']):
            errors.append('Size mismatch: ' + name)
        expected = row['sha256']
        if expected == 'SELF_REFERENTIAL_MANIFEST':
            if name not in SELF_FILES:
                errors.append('Unexpected self-hash exemption: ' + name)
        elif hashlib.sha256(data).hexdigest() != expected:
            errors.append('SHA-256 mismatch: ' + name)
    if (ROOT / '.git').exists():
        visible = set(filter(None, git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode('utf-8').split('\0')))
    else:
        visible = set()
        import os
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in {'.git', '.venv', '.release-audit', '__pycache__'}]
            for name in files:
                rel = (Path(folder) / name).relative_to(ROOT).as_posix()
                if rel.startswith('results/runs/') and rel != 'results/runs/README.md':
                    continue
                visible.add(rel)
    if visible != set(names):
        errors.append('Working file set differs from manifest: ' + json.dumps(sorted(visible.symmetric_difference(names))))
    if args.check_index:
        indexed = set(filter(None, git('ls-files', '-z').decode('utf-8').split('\0')))
        if indexed != set(names):
            errors.append('Git index differs from manifest')
        for name in sorted(indexed.intersection(names)):
            if git('show', ':' + name) != (ROOT / name).read_bytes():
                errors.append('Index/worktree byte mismatch: ' + name)
        if git('remote').strip():
            errors.append('Remote configuration is present; expected a local-only candidate')
    print(json.dumps({'status': 'PASS' if not errors else 'FAIL', 'files': len(rows),
                      'total_size_bytes': sum(int(row['size_bytes']) for row in rows),
                      'index_checked': args.check_index, 'errors': errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
