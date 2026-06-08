# KB Section 2 — Metric Thresholds & Sprint Health Classification

Use these exact thresholds when classifying sprint health. Do not invent new categories.

## Completion Rate (item count)

| Rate | Classification | Action |
|---|---|---|
| ≥ 80% | ✅ On Track | Normal reporting |
| 70–79% | 🟡 Needs Attention | Flag to PM if < 3 days remain |
| 50–69% | 🟠 At Risk | Flag immediately; list unfinished items |
| < 50% | 🔴 Critical | Escalate; recommend scope reduction |

## Story Points Completion

| Rate | Classification |
|---|---|
| ≥ 75% | Healthy |
| 50–74% | Needs Attention |
| < 50% | At Risk |

## Days Remaining vs Completion

| Days Remaining | Completion Rate | Signal |
|---|---|---|
| > 5 | Any | Normal — time to course-correct |
| 3–5 | < 70% | 🟠 At Risk — active mitigation needed |
| 1–2 | < 70% | 🔴 Escalate immediately |
| 0 or negative | < 100% | 🔴 Sprint overdue |

## Blocked Items

| Blocked Count | Signal |
|---|---|
| 0 | Healthy |
| 1–2 | Note |
| 3–5 | 🟠 Flag to PM |
| > 5 | 🔴 Escalate — systemic blocker |

## Off-Track Items (items stuck in a state > 3 working days)

| Off-Track Count | Signal |
|---|---|
| 0–2 | Normal |
| 3–5 | 🟡 Monitor |
| > 5 | 🔴 Flag — delivery at risk |

## Bug-to-Story Ratio

| Ratio | Signal |
|---|---|
| < 0.3 | Healthy |
| 0.3–0.5 | Acceptable |
| 0.5–1.0 | 🟡 Quality debt growing |
| > 1.0 | 🔴 Critical quality issue |
