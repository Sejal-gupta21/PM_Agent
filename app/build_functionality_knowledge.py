#!/usr/bin/env python3
"""Build functionality knowledge artifacts from commits and files.

Outputs:
 - outputs/functionality_docs.json   (per-file docs)
 - outputs/dev_functionality_summary.csv
 - outputs/wi_functionality_map.json
"""
from __future__ import annotations

import json
from pathlib import Path
from utilities.commit_analyzer import parse_git_log, write_summary_outputs
from utilities.functionality_mapper import build_file_documents, aggregate_dev_functionality, map_wi_to_functionality

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / 'outputs'
OUT.mkdir(exist_ok=True)


def main():
    commits = parse_git_log(REPO, max_commits=500)
    # write commit summaries (reuse existing writer to ensure consistency)
    write_summary_outputs(REPO, commits, out_dir=OUT)
    # read the just-written commit summary to build docs
    # find the latest commit_summary file
    import glob
    sums = sorted(OUT.glob('commit_summary_*.json'))
    if not sums:
        print('No commit_summary found')
        return 2
    latest = sums[-1]
    commit_summary = json.loads(latest.read_text(encoding='utf-8'))

    docs = build_file_documents(commit_summary)
    docs_path = OUT / 'functionality_docs.json'
    docs_path.write_text(json.dumps(docs, indent=2), encoding='utf-8')

    dev_map = aggregate_dev_functionality(docs)
    # write CSV
    import csv
    csv_path = OUT / 'dev_functionality_summary.csv'
    with csv_path.open('w', encoding='utf-8', newline='') as fh:
        writer = csv.writer(fh)
        writer.writerow(['Member', 'Functionality', 'Files_Count', 'Example_Paths'])
        for dev, feats in dev_map.items():
            for func, info in feats.items():
                ex = '; '.join([e['path'] for e in info.get('evidence', [])][:5])
                writer.writerow([dev, func, info.get('files', 0), ex])

    # map WIs
    wi_map_files = sorted(OUT.glob('commit_wi_map_*.json'))
    if wi_map_files:
        wi_path = wi_map_files[-1]
        wi_func_map = map_wi_to_functionality(wi_path, docs)
        wi_out = OUT / 'wi_functionality_map.json'
        wi_out.write_text(json.dumps(wi_func_map, indent=2), encoding='utf-8')
        print('Wrote', wi_out)

    print('Wrote', docs_path, 'and', csv_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
