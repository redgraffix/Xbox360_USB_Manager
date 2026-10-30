"""Check the Git index and reachable HEAD history without printing secret values."""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1024 * 1024
ALLOWED_SUFFIXES = {'.py', '.md', '.txt', '.yml', '.yaml', '.toml', '.ini'}
ALLOWED_NAMES = {'.gitignore', '.gitattributes', 'LICENSE', 'NOTICE'}
BLOCKED_PARTS = {'_state', '_work', '_app', '_internal', '_downloads', 'outputs', 'backups',
                 'games', 'content', '.ssh', '.aws', '.codex', 'manifests'}
BLOCKED_NAMES = {'settings.json', 'queue.json', 'downloads.json', '.env'}
PATTERNS = {
    'private-key material': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'GitHub token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'AWS access key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'credential literal': re.compile(r"""(?i)\b(?:password|api_key|access_token|client_secret)\s*[:=]\s*["'][^"'\s]{12,}["']"""),
    'personal Windows path': re.compile(r'[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)[A-Za-z0-9_.-]+'),
    'personal Unix path': re.compile(r'/(?:home|Users)/[A-Za-z0-9_.-]+/'),
    'private network address': re.compile(r'\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b'),
}
EMAIL = re.compile(r'\b[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def main():
    failures = []
    entries = git('ls-files', '--stage', '-z').decode('utf-8').split('\0')
    count = 0
    for entry in filter(None, entries):
        attributes, name = entry.split('\t', 1)
        mode, blob, stage = attributes.split()
        count += 1
        path = Path(name)
        parts = {part.casefold() for part in path.parts}
        if mode not in {'100644', '100755'} or stage != '0':
            failures.append(f'{name}: unexpected file mode or unresolved merge')
            continue
        if (parts & BLOCKED_PARTS or path.name.casefold() in BLOCKED_NAMES
                or path.name.startswith('.env.')
                or (path.suffix.casefold() not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES)):
            failures.append(f'{name}: not a permitted source/documentation file')
        data = git('cat-file', 'blob', blob)
        if len(data) > MAX_BYTES:
            failures.append(f'{name}: exceeds source-file size limit')
            continue
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            failures.append(f'{name}: binary or non-UTF-8 content')
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count('\n', 0, match.start()) + 1
                failures.append(f'{name}:{line}: possible {label}')
        if any(not (match.group().endswith('@users.noreply.github.com') or match.group() == 'noreply@github.com')
               for match in EMAIL.finditer(text)):
            failures.append(f'{name}: email address requires privacy review')
    # Do not dump author addresses or commit messages into CI logs.
    for row in git('log', 'HEAD', '--format=%H%x09%ae%x09%ce').decode('utf-8').splitlines():
        commit, author, committer = row.split('\t')
        if not all(address.endswith('@users.noreply.github.com') or address == 'noreply@github.com'
                   for address in (author, committer)):
            failures.append(f'commit {commit[:12]}: use a GitHub no-reply email')
    if failures:
        print('Public-tree check needs review:')
        for issue in failures:
            print(' - ' + issue)
        return 1
    print(f'Public-tree check passed: {count} tracked source/documentation files; no flagged patterns.')
    print('This is a lightweight check, not an exhaustive security or secret audit.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
