from __future__ import annotations

import subprocess
from pathlib import Path


path = Path(".github/workflows/ci.yml")
source = path.read_text(encoding="utf-8")
old = '''      - name: Publish coverage ratchet status
        if: always()
        env:
          COVERAGE_SUMMARY_PATH: ${{ runner.temp }}/coverage/coverage-summary.json
        uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1
        with:
          script: |
            const fs = require('fs');
            const summaryPath = process.env.COVERAGE_SUMMARY_PATH;
            let state = 'failure';
            let description = 'Coverage report unavailable';
            if (fs.existsSync(summaryPath)) {
              const summary = JSON.parse(fs.readFileSync(summaryPath, 'utf8'));
              state = summary.passed ? 'success' : 'failure';
              const total = Number(summary.total_percent);
              const baseline = Number(summary.baseline_percent);
              if (Number.isFinite(total) && Number.isFinite(baseline)) {
                description = `Coverage ${total.toFixed(2)}% / baseline ${baseline.toFixed(2)}%`;
              } else {
                description = summary.passed ? 'Coverage ratchet passed' : 'Coverage ratchet failed';
              }
            }
            await github.rest.repos.createCommitStatus({
              owner: context.repo.owner,
              repo: context.repo.repo,
              sha: context.sha,
              state,
              context: 'ci/coverage-ratchet',
              description,
              target_url: `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`,
            });
'''
new = '''      - name: Publish coverage ratchet status
        if: always()
        env:
          COVERAGE_SUMMARY_PATH: ${{ runner.temp }}/coverage/coverage-summary.json
        uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1
        with:
          script: |
            const fs = require('fs');
            const summaryPath = process.env.COVERAGE_SUMMARY_PATH;
            let state = 'failure';
            let description = 'Coverage report unavailable';
            if (fs.existsSync(summaryPath)) {
              const summary = JSON.parse(fs.readFileSync(summaryPath, 'utf8'));
              state = summary.combined_passed ? 'success' : 'failure';
              const total = Number(summary.total_percent);
              const baseline = Number(summary.baseline_percent);
              if (Number.isFinite(total) && Number.isFinite(baseline)) {
                description = `Combined coverage ${total.toFixed(2)}% / baseline ${baseline.toFixed(2)}%`;
              } else {
                description = summary.combined_passed
                  ? 'Combined coverage ratchet passed'
                  : 'Combined coverage ratchet failed';
              }
            }
            await github.rest.repos.createCommitStatus({
              owner: context.repo.owner,
              repo: context.repo.repo,
              sha: context.sha,
              state,
              context: 'ci/coverage-ratchet',
              description,
              target_url: `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`,
            });

      - name: Publish branch coverage ratchet status
        if: always()
        env:
          COVERAGE_SUMMARY_PATH: ${{ runner.temp }}/coverage/coverage-summary.json
        uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1
        with:
          script: |
            const fs = require('fs');
            const summaryPath = process.env.COVERAGE_SUMMARY_PATH;
            let state = 'failure';
            let description = 'Branch coverage report unavailable';
            if (fs.existsSync(summaryPath)) {
              const summary = JSON.parse(fs.readFileSync(summaryPath, 'utf8'));
              state = summary.branch_passed ? 'success' : 'failure';
              const branch = Number(summary.branch_percent);
              const baseline = Number(summary.branch_baseline_percent);
              if (Number.isFinite(branch) && Number.isFinite(baseline)) {
                description = `Branch coverage ${branch.toFixed(2)}% / baseline ${baseline.toFixed(2)}%`;
              } else {
                description = summary.branch_passed
                  ? 'Branch coverage ratchet passed'
                  : 'Branch coverage ratchet failed';
              }
            }
            await github.rest.repos.createCommitStatus({
              owner: context.repo.owner,
              repo: context.repo.repo,
              sha: context.sha,
              state,
              context: 'ci/branch-coverage-ratchet',
              description,
              target_url: `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`,
            });
'''
if source.count(old) != 1:
    raise SystemExit(f"coverage status anchor mismatch: found {source.count(old)}")
path.write_text(source.replace(old, new, 1), encoding="utf-8")

subprocess.run(
    ["python", "-m", "py_compile", "scripts/coverage_gate.py"],
    check=True,
)
subprocess.run(
    ["python", "-m", "pytest", "-q", "tests/test_coverage_ratchet_gate.py"],
    check=True,
)
