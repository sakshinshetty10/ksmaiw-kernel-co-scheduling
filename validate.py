#!/usr/bin/env python3
"""Structural validation and SHA-256 manifest for a generated KSMAIW run."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description='Validate a KSMAIW experiment run and write a SHA-256 manifest.')
    ap.add_argument('--results', default='data/experiment_run', help='experiment result directory, relative to project root or absolute')
    args = ap.parse_args()
    results_dir = Path(args.results)
    RESULTS = results_dir if results_dir.is_absolute() else ROOT / results_dir
    errors = []
    tests = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(ROOT / 'tests'), '-v'],
                           cwd=ROOT, text=True, capture_output=True)
    print(tests.stdout); print(tests.stderr)
    if tests.returncode:
        errors.append('unit tests failed')
    py_compile = subprocess.run([sys.executable, '-m', 'py_compile', *map(str, (ROOT / 'src').glob('*.py'))],
                                text=True, capture_output=True)
    if py_compile.returncode:
        errors.append('py_compile failed: ' + py_compile.stderr)
    summary_file = RESULTS / 'trial_summary.csv'
    if not summary_file.exists():
        errors.append('trial_summary.csv missing')
    else:
        rows = list(csv.DictReader(summary_file.open(newline='', encoding='utf-8')))
        expected_cells = {(s, p, str(t)) for s in ('light', 'contended', 'bursty')
                          for p in ('B0_DEFAULT', 'B1_STATIC', 'B3_KSMAIW') for t in (1, 2, 3)}
        actual_cells = {(r['scenario'], r['policy'], r['trial']) for r in rows}
        if actual_cells != expected_cells:
            errors.append(f'expected 27 scenario/policy/trial cells; got {len(actual_cells)}')
        for r in rows:
            if float(r['requests_issued']) <= 0:
                errors.append(f'no requests issued in {r["scenario"]}/{r["policy"]}/{r["trial"]}')
            if not all(float(r[k]) >= 0 for k in ('p50_ms','p95_ms','p99_ms','inference_items_per_s')):
                errors.append(f'negative metric in {r["scenario"]}/{r["policy"]}/{r["trial"]}')
            stem = f"{r['scenario']}__{r['policy']}__trial_{r['trial']}"
            for suffix in ('_requests.jsonl', '_summary.json'):
                p = RESULTS / 'raw' / f'{stem}{suffix}'
                if not p.exists(): errors.append(f'missing raw file: {p.name}')
            cp = RESULTS / 'raw' / f'{stem}_controller.jsonl'
            if r['policy'] == 'B3_KSMAIW' and not cp.exists():
                errors.append(f'missing controller log: {cp.name}')
        print(f'Found {len(rows)} trial summaries; expected 27.')
    # Every raw JSONL line must parse.
    raw_dir = RESULTS / 'raw'
    if raw_dir.exists():
        for path in raw_dir.glob('*.jsonl'):
            for line_no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
                try: json.loads(line)
                except json.JSONDecodeError: errors.append(f'invalid JSON: {path.name}:{line_no}')
    required_artifacts = [
        ROOT / 'manuscript' / 'KSMAIW_Revised_Full_Manuscript.docx',
        ROOT / 'manuscript' / 'KSMAIW_Revised_Full_Manuscript.pdf',
        ROOT / 'presentation' / 'KSMAIW_Research_Presentation.pptx',
        ROOT / 'RESULTS.md', ROOT / 'SUBMISSION_CHECKLIST.md',
    ]
    for artifact in required_artifacts:
        if not artifact.exists() or artifact.stat().st_size == 0:
            errors.append(f'missing/empty deliverable artifact: {artifact.relative_to(ROOT)}')
    for figure in (ROOT / 'data' / 'processed' / 'figures').glob('figure_*.png'):
        if figure.stat().st_size == 0:
            errors.append(f'empty figure: {figure.name}')
    if len(list((ROOT / 'data' / 'processed' / 'figures').glob('figure_*.png'))) < 6:
        errors.append('expected at least six analysis figures')
    if errors:
        print('\nVALIDATION FAILED')
        for e in errors: print('-', e)
        return 1
    # Immutable manifest for code, data and manuscript artifacts (generated at validation time).
    manifest = ROOT / 'SHA256SUMS.txt'
    exclude_parts = {'__pycache__', '.venv'}
    files = [p for p in ROOT.rglob('*') if p.is_file() and not any(part in exclude_parts for part in p.parts)
             and p.name != manifest.name and p.suffix not in {'.zip'}]
    with manifest.open('w', encoding='utf-8') as f:
        for path in sorted(files):
            f.write(f'{sha256(path)}  {path.relative_to(ROOT).as_posix()}\n')
    print(f'VALIDATION PASSED; wrote {manifest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
