"""Run real local CLI tasks in disposable Git projects and independently check outputs.

Uses installed Ollama, no mock providers, no paid calls. Reports CLI behavior only.
Run with .venv/Scripts/python scripts/audit_local_workflows.py --output artifacts/<new-folder>.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def command(argv, root, timeout=120):
    started = time.monotonic()
    try:
        run = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=timeout,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return {'argv': argv, 'exit': run.returncode, 'stdout': run.stdout, 'stderr': run.stderr,
                'seconds': time.monotonic() - started}
    except subprocess.TimeoutExpired as exc:
        return {'argv': argv, 'exit': None, 'stdout': str(exc.stdout or ''), 'stderr': 'Timed out',
                'seconds': time.monotonic() - started}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append', choices=[f'C{i:02}' for i in range(1, 9)],
                        help='Run only selected cases; repeat this option for a focused regression')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    scenarios = [
        ('C01', 'Create a file called empty.txt', {}, {'empty.txt': b''}, None),
        ('C02', 'Update greeting.txt to contain exactly "Hello local AI!" followed by a newline. Preserve tests.',
         {'greeting.txt': 'old\n'}, {'greeting.txt': b'Hello local AI!\n'}, None),
        ('C03', 'Create calculator.py with a function add(a,b) returning a+b. Preserve the tests and run validation.',
         {'test_behavior.py': 'def test_add():\n    from calculator import add\n    assert add(2,3)==5\n    assert add(-3,3)==0\n    assert add(1.5,2.5)==4\n'},
         {}, 'from calculator import add; assert add(2,3)==5; assert add(-3,3)==0; assert add(1.5,2.5)==4'),
        ('C04', 'Fix calculator.py: add(a,b) must add instead of subtract. Preserve tests and run validation.',
         {'calculator.py': 'def add(a,b):\n    return a-b\n',
          'test_behavior.py': 'from calculator import add\ndef test_add():\n    assert add(2,3)==5\n    assert add(-3,3)==0\n'},
         {}, 'from calculator import add; assert add(2,3)==5; assert add(-3,3)==0'),
        ('C05', 'Implement the tested greeting feature across formatter.py and service.py. formatter.format_name(name) strips spaces and title-cases name. service.greet(name) imports format_name and returns "Hello, {formatted name}!". Inspect tests first. Preserve tests and README.md. Run validation.',
         {'test_behavior.py': 'def test_greet():\n    from service import greet\n    assert greet("  ada lovelace ")=="Hello, Ada Lovelace!"\n    assert greet("ALAN")=="Hello, Alan!"\n'},
         {}, 'from service import greet; assert greet("  ada lovelace ")=="Hello, Ada Lovelace!"; assert greet("ALAN")=="Hello, Alan!"'),
        ('C06', 'Run validation and report the failure. Do not edit any files; this is a controlled failing test.',
         {'test_behavior.py': 'def test_controlled_failure():\n    assert False, "controlled audit failure"\n'}, {}, None),
        ('C07', 'Read missing.txt and tell me whether it exists. Do not create it or change any file. Run validation.', {}, {}, None),
        ('C08', 'Read README.md and summarize it.', {}, {}, None),
    ]
    reports = []
    for case_id, task, files, expected, functional in scenarios:
        if args.case and case_id not in args.case:
            continue
        root = output / case_id
        root.mkdir()
        baseline = {'pyproject.toml': '[tool.pytest.ini_options]\npythonpath=["."]\n',
                    '.gitignore': '__pycache__/\n.pytest_cache/\n.molicoder/\n',
                    'README.md': 'Controlled local CLI audit fixture.\n',
                    'test_behavior.py': 'def test_fixture():\n    assert 2+2==4\n'} | files
        for name, content in baseline.items():
            (root / name).write_text(content, encoding='utf-8')
        for argv in (['git', 'init', '-b', 'molicoder/audit'], ['git', 'add', '.'],
                     ['git', '-c', 'user.name=CLI Audit', '-c', 'user.email=audit@example.invalid',
                      'commit', '-m', 'Controlled baseline']):
            result = command(argv, root)
            if result['exit'] != 0:
                raise RuntimeError(result)
        protected = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                     for name in ('test_behavior.py', 'README.md', 'pyproject.toml')}
        before = command([sys.executable, '-m', 'pytest', '-q'], root)
        report_path = output / f'{case_id}-executor.json'
        argv = [sys.executable, '-m', 'local_model_chat.molicoder.cli', task,
                '--project', str(root), '--yes', '--report', str(report_path)]
        if case_id == 'C08':
            argv.extend(['--model', 'molicoder-audit-nonexistent:never'])
        execution = command(argv, root)
        after = command([sys.executable, '-m', 'pytest', '-q'], root)
        function_check = command([sys.executable, '-c', functional], root) if functional else None
        unchanged = all((root / name).is_file() and hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                        for name, digest in protected.items())
        disk_matches = all((root / name).is_file() and (root / name).read_bytes() == value
                           for name, value in expected.items())
        diff = command(['git', 'diff', '--stat'], root)
        generated = command(['git', 'ls-files', '--others', '--exclude-standard'], root)
        record = json.loads(report_path.read_text()) if report_path.is_file() else {}
        if case_id == 'C06':
            passed = execution['exit'] == 2 and after['exit'] == 1 and unchanged and not generated['stdout']
        elif case_id == 'C08':
            passed = execution['exit'] == 2 and record.get('result', {}).get('status') == 'failed' and unchanged
        elif case_id == 'C07':
            trace = '\n'.join(record.get('result', {}).get('trace', []))
            passed = execution['exit'] == 0 and not (root / 'missing.txt').exists() and unchanged and 'missing.txt' in trace
        else:
            passed = execution['exit'] == 0 and after['exit'] == 0 and unchanged and disk_matches and (
                function_check is None or function_check['exit'] == 0)
        if case_id == 'C01':
            passed = passed and record.get('provider') == 'none' and record.get('result', {}).get('tool_calls') == 0
        report = dict(id=case_id, timestamp=datetime.now(timezone.utc).isoformat(),
                      execution_method='real molicoder-exec CLI, local Ollama; not desktop orchestration',
                      objective=task, project=str(root), status='PASS' if passed else 'FAIL',
                      provider=record.get('provider'), model=record.get('model'), effort=record.get('effort'),
                      routing_reason=record.get('routing_reason'), before=before, execution=execution, after=after,
                      functional=function_check, protected_files_unchanged=unchanged,
                      protected_file_sha256=protected,
                      exact_artifacts_match=disk_matches, diff=diff, untracked=generated,
                      evidence=[str(report_path)])
        (output / f'{case_id}-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        reports.append(report)
        (output / 'summary.json').write_text(json.dumps(reports, indent=2), encoding='utf-8')
        print(case_id, report['status'], f'{execution["seconds"]:.1f}s', flush=True)
    return 0 if all(r['status'] == 'PASS' for r in reports) else 1


if __name__ == '__main__':
    raise SystemExit(main())
