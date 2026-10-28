"""Check scaffold integrity, not application quality."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
required = ['README.md', 'AGENTS.md', 'agent.md', 'docs/rules.md', 'docs/roadmap.md',
            'docs/devlog.md', 'tools/inspect_zip.py', 'prompts/extract-slide.v1.md',
            'prompts/summarize.v1.md', 'evals/golden/cases.json',
            'docs/skill-templates/run-evaluation.md.template',
            'docs/skill-templates/implement-change.md.template']
errors = [f'Missing: {p}' for p in required if not (root / p).is_file()]
if not errors:
    data = json.loads((root / 'evals/golden/cases.json').read_text())
    ids = [case['id'] for case in data['cases']]
    if len(ids) != len(set(ids)):
        errors.append('Duplicate case IDs')
    for case in data['cases']:
        if case['reviewed'] and not case['required_facts']:
            errors.append(f"Reviewed case has no facts: {case['id']}")
if errors:
    raise SystemExit('\n'.join(errors))
print('Harness structure OK. Application tests and live model evaluation have NOT run.')
