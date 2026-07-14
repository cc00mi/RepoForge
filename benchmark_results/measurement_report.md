# RepoForge -- Measurable Metrics Report

Generated: 2026-07-12 03:27 UTC

---

## 1. PR Review Coverage

- **Status:** ok
- **Reviewed PRs:** 0
- **Memory files found:** 4
- **Unique repos:** 0
- **Coverage ratio:** 0.0%
- **Total open PRs:** N/A -- query via: gh api repos/OWNER/REPO/pulls?state=open
- **Target:** 100% -- total_open_prs requires GitHub API or manual input

---

## 2. Review Finding Recall (Proxy)

- **Status:** ok
- **Method:** problem_statement <-> agent_patch overlap (proxy for human review)
- **Instances analyzed:** 7
- **Avg proxy recall:** 21.6%
- **Avg proxy precision:** 26.2%
- **Proxy F1:** 0.237
- **Target:** >= 80% recall vs human
- **Caveat:** Proxy metric -- true recall requires structured human review comparison

| Instance | File Recall | Term Recall | Proxy Recall | Files in Problem |
|----------|-------------|-------------|--------------|------------------|
| astropy__astropy-12907 | 50.0% | 8.6% | 29.3% |  |
| astropy__astropy-14182 | 0.0% | 8.9% | 4.4% | /usr/lib/python3/dist-packages/astropy/io/ascii/connect.py, /usr/lib/python3/dist-packages/astropy/io/ascii/core.py, /usr/lib/python3/dist-packages/astropy/io/ascii/ui.py +2 |
| astropy__astropy-14365 | 50.0% | 4.1% | 27.0% |  |
| astropy__astropy-14995 | 50.0% | 11.3% | 30.7% |  |
| astropy__astropy-14182 | 0.0% | 8.9% | 4.4% | /usr/lib/python3/dist-packages/astropy/io/ascii/connect.py, /usr/lib/python3/dist-packages/astropy/io/ascii/core.py, /usr/lib/python3/dist-packages/astropy/io/ascii/ui.py +2 |
| astropy__astropy-14995 | 50.0% | 11.3% | 30.7% |  |
| django__django-11001 | 50.0% | 0.0% | 25.0% |  |

---

## 3. Stale PR Reduction

- **Status:** ok
- **Method:** Simulated 3 scans over 14 days with 20 synthetic PRs
- **First scan stale count:** 17
- **Latest scan stale count:** 7
- **Absolute reduction:** 10 PRs
- **Reduction %:** 58.8%
- **Target:** >= 50% -- MET

| Day | Date | Total | Stale | Warn | Label | Close |
|-----|------|-------|-------|------|-------|-------|
| 0 | 2026-06-15 | 20 | 11 | 6 | 3 | 2 |
| 7 | 2026-06-22 | 18 | 7 | 4 | 3 | 0 |
| 14 | 2026-06-29 | 18 | 7 | 4 | 3 | 0 |

---

## 4. Time to First Response

- **Status:** ok
- **Samples:** 9
- **Median TTR:** 1.4m
- **Mean TTR:** 2.1m
- **P95 TTR:** 5.2m
- **Below 5min:** 8/9 (88.9%)
- **Target:** <= 300s (5 min) -- MET
