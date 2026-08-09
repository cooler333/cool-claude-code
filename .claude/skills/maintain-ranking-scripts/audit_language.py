"""CLASSIFICATION AUDIT — exclusion category 5 (non-English).

Reports, for every repo in the published set, the share of non-Latin letters in
(a) its GitHub description and (b) its primary README. A repo is a hit when
*either* is >= THRESHOLD; hits go to `helpers/filtered.json` by hand.

Prose only — fenced/inline code, HTML tags, URLs and link targets are stripped
before measuring, because badge URLs and code blocks dilute the real prose and
would hide a fully non-English README.

Needs the network (`gh api`), so it lives here and not in the pipeline:
`fetch.py` fetches metadata only and `render.py` never networks.

Usage (from the repo root):  python3 .claude/skills/maintain-ranking-scripts/audit_language.py
"""

import base64
import json
import re
import subprocess
import sys
import unicodedata

THRESHOLD = 0.5
NON_LATIN = ('CJK', 'HIRAGANA', 'KATAKANA', 'HANGUL', 'CYRILLIC',
             'ARABIC', 'HEBREW', 'DEVANAGARI', 'THAI')


def prose(md):
    """Strip everything that isn't human prose, so the ratio reflects language."""
    md = re.sub(r'```.*?```', ' ', md, flags=re.S)
    md = re.sub(r'~~~.*?~~~', ' ', md, flags=re.S)
    md = re.sub(r'`[^`]*`', ' ', md)
    md = re.sub(r'<[^>]+>', ' ', md)
    md = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r' \1 ', md)
    md = re.sub(r'https?://\S+', ' ', md)
    return md


def share(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    n = sum(1 for c in letters
            if any(k in unicodedata.name(c, '') for k in NON_LATIN))
    return n / len(letters)


def readme(repo_id):
    r = subprocess.run(['gh', 'api', f'repos/{repo_id}/readme', '--jq', '.content'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return base64.b64decode(r.stdout).decode('utf-8', 'replace')
    except Exception:
        return None


def main():
    meta = {r['full_name']: r
            for r in json.load(open('helpers/repos.json'))['repos']}
    raw = json.load(open('helpers/repos_to_render.json'))
    ids = raw['repos'] if isinstance(raw, dict) else raw

    hits = []
    for n, rid in enumerate(ids, 1):
        desc = share(meta.get(rid, {}).get('description') or '')
        md = readme(rid)
        if md is None:
            print(f'!! no README: {rid}', file=sys.stderr)
        rd = share(prose(md)) if md else 0.0
        print(f'{n:3d}/{len(ids)} desc {desc:.2f}  readme {rd:.2f}  {rid}',
              file=sys.stderr)
        if max(desc, rd) >= THRESHOLD:
            hits.append({'repo_id': rid, 'description': round(desc, 2),
                         'readme': round(rd, 2)})

    print(json.dumps(hits, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
