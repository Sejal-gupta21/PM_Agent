# KB Section 3 — Analysis Guidelines & Risk Interpretation

## Escalation Decision Rules

Use these decision rules when asked to identify risk or blocked items.

### Escalate (CRITICAL — act immediately)
- Completion rate < 50% AND days_remaining ≤ 2
- Blocked count > 5
- Sprint `finishDate` has passed with open items
- Bug-to-story ratio > 1.0
- A single assignee holds > 50% of open work items

### Flag to PM (AT RISK — needs attention today)
- Completion rate < 70% AND days_remaining ≤ 3
- Blocked count 3–5
- Off-track items > 5 (stuck in state > 3 working days)
- Items with Priority 1 still in `New` state with < 5 days remaining

### Note (MONITOR — document, no immediate action)
- Completion rate 70–79% with days_remaining ≤ 5
- Blocked count 1–2
- Unassigned high-priority items

### Ignore
- Items in `Done`, `Closed`, `Resolved`, `Removed` — these do not need PM attention
- Future sprint items accidentally returned in query — filter them out

---

## Reading `days_in_state` as WIP Age

| WIP Age | Interpretation |
|---|---|
| 0–2 days | Normal in-progress |
| 3–5 days | Slow — check if blocked |
| > 5 days | High WIP age — strong indicator of blocked or abandoned work |
| > 10 days | Flag as abandoned unless the item is explicitly large (story points > 8) |

---

## When Story Points Are Sparse

If fewer than 50% of items have story points, rely on item count for completion rate and throughput.
Say "story-point-based metrics are not available; using item count." Do not fabricate velocity numbers.

---

## Data Integrity Rules

- ❌ FORBIDDEN: Assuming completion rates without checking work item states
- ❌ FORBIDDEN: Guessing percentages when state information is available
- ❌ FORBIDDEN: Assuming 100% completion just because items exist
- ✅ REQUIRED: Analyse `System.State` for every item before computing any metric
- ✅ REQUIRED: Use the STATE CATEGORIES mapping (Not Started / In Progress / Completed / Blocked) to classify states — do NOT guess categories
- ✅ REQUIRED: Calculate metrics from actual data, not assumptions
- ✅ REQUIRED: When a GROUND TRUTH block is provided in the prompt, use those numbers exactly — never recount
