# Capacity Triaging - Requirements Analysis

## Overview

This document analyzes the capacity triaging requirements against the current implementation status.

---

## Requirements from PM-Agent Specification

### Requirement 1: Capture Planned vs Current Capacity
> "Capture planned sprint capacity vs current available capacity from ADO (leave, unplanned events)"

| Feature | Status | Implementation Details |
|---------|--------|------------------------|
| Fetch planned capacity from ADO Sprint Capacity | ✅ Complete | `work_get_team_capacity` MCP tool |
| Fallback to work item CompletedWork when Sprint Capacity empty | ✅ Complete | `_get_capacity_from_work_items()` method |
| Track days off / leave | ⚠️ Partial | Works when teams configure capacity in ADO; fallback shows 0 days off |
| Track unplanned events | ❌ Not Implemented | Requires external attendance system integration |

**Technical Notes:**
- When ADO Sprint Capacity is empty, system falls back to extracting capacity from `Microsoft.VSTS.Scheduling.CompletedWork` field
- Each team member's capacity is estimated at 6.5 hrs/day based on completed work
- Days off detection requires ADO Sprint Capacity to be configured by team

---

### Requirement 2: Define Deviation Thresholds
> "Define deviation thresholds (e.g., >20% drop)"

| Threshold | Value | Purpose |
|-----------|-------|---------|
| BURNDOWN_DEVIATION_THRESHOLD_HIGH | 20% | Triggers HIGH severity alert |
| BURNDOWN_DEVIATION_THRESHOLD_MEDIUM | 10% | Triggers MEDIUM severity alert |
| TODO_THRESHOLD | 60% | Items still in To-Do after 30% sprint triggers warning |
| CAPACITY_DROP_THRESHOLD_HIGH | 30% | Capacity drop triggers HIGH alert |
| CAPACITY_DROP_THRESHOLD_MEDIUM | 15% | Capacity drop triggers MEDIUM alert |

**Status:** ✅ Complete

**Location:** [scripts/capacity_triaging.py](../scripts/capacity_triaging.py) lines 43-56

---

### Requirement 3: Detect Early Indicators of Sprint Slippage
> "Detect early indicators of sprint slippage (e.g., too many tasks still in To-Do after 30% of sprint OR deviation from ideal Sprint Burndown)"

| Indicator | Status | Implementation |
|-----------|--------|----------------|
| Sprint progress calculation | ✅ Complete | `_calculate_sprint_progress()` |
| Completion % vs ideal burndown | ✅ Complete | Compares actual completion to elapsed time |
| Too many tasks in To-Do | ✅ Complete | Checks if >60% items in To-Do after 30% sprint |
| Burndown deviation detection | ✅ Complete | `identify_risks()` method |
| Work distribution analysis | ✅ Complete | To-Do / In Progress / Done percentages |

**Current Risk Detection:**
- `CAPACITY_THINNING`: Team capacity dropped >15/30% from normal
- `SPRINT_SLIPPAGE`: >60% items still in To-Do after 30% sprint elapsed
- `BURNDOWN_DEVIATION`: Sprint behind ideal burndown by >10/20%

**Status:** ✅ Complete

---

### Requirement 4: Generate Automated Warning Email
> "Generate automated warning email to PM with detailed risk summary"

| Feature | Status | Implementation |
|---------|--------|----------------|
| HTML report generation | ✅ Complete | `generate_html_report()` method |
| Email sending via SMTP | ✅ Complete | Using `notification-wtt@walkingtree.tech` |
| Risk severity in report | ✅ Complete | HIGH / MEDIUM / LOW with color coding |
| Team member capacity details | ✅ Complete | Table showing each member's capacity |
| Work item distribution | ✅ Complete | To-Do / In Progress / Done counts |
| Sprint progress metrics | ✅ Complete | % elapsed vs % completed |

**Email Recipients Configuration:**
```python
DEV_EMAIL_RECIPIENTS = [
    "ankur.kumar@walkingtree.tech",
    "sejal.gupta@walkingtree.tech",
    "yati.gautam@walkingtree.tech",
    "sarthak.singh@walkingtree.tech"
]
```

**Known Limitation:** Cannot send to external Gmail addresses due to domain policy.

**Status:** ✅ Complete

---

### Requirement 5: Suggest Corrective Actions
> "Suggest corrective action (e.g., re-prioritize, descoping candidates)"

| Action Type | Status | Implementation |
|-------------|--------|----------------|
| Re-evaluate sprint commitment | ✅ Complete | For CAPACITY_THINNING |
| Identify descoping candidates | ✅ Complete | Lists low-priority (P3+) items in To-Do |
| Schedule mid-sprint retrospective | ✅ Complete | For BURNDOWN_DEVIATION |
| Review blockers/impediments | ✅ Complete | For BURNDOWN_DEVIATION |
| Accelerate work pickup | ✅ Complete | For SPRINT_SLIPPAGE |
| Daily standup focus areas | ✅ Complete | For SPRINT_SLIPPAGE |
| Unblock items | ✅ Complete | Lists items with "blocked" tag |

**Descoping Logic:**
- Identifies work items with Priority 3+ in "To Do", "New", or "Proposed" state
- Sorts by priority (lowest first)
- Shows top 5 candidates for descoping in HIGH severity situations

**Status:** ✅ Complete

---

## Data Flow Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                     CAPACITY TRIAGING FLOW                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. Get Current Iteration                                        │
│     └── work_list_team_iterations (timeframe=current)            │
│                                                                  │
│  2. Get Team Capacity                                            │
│     ├── Primary: work_get_team_capacity (ADO Sprint Capacity)    │
│     └── Fallback: wit_get_work_items_for_iteration               │
│         └── Extract CompletedWork from work items                │
│                                                                  │
│  3. Get Work Items                                               │
│     └── wit_get_work_items_for_iteration                         │
│         └── Batch fetch wit_get_work_items_batch_by_ids          │
│                                                                  │
│  4. Calculate Metrics                                            │
│     ├── Sprint progress (% elapsed)                              │
│     ├── Completion % (Done / Total)                              │
│     ├── State distribution (To-Do, In Progress, Done)            │
│     └── Burndown deviation                                       │
│                                                                  │
│  5. Identify Risks                                               │
│     ├── CAPACITY_THINNING (capacity drop)                        │
│     ├── SPRINT_SLIPPAGE (too many in To-Do)                      │
│     └── BURNDOWN_DEVIATION (behind ideal burndown)               │
│                                                                  │
│  6. Generate Actions                                             │
│     ├── Descoping candidates                                     │
│     ├── Blocked items                                            │
│     └── Specific recommendations                                 │
│                                                                  │
│  7. Generate HTML Report                                         │
│     └── Save to outputs/capacity_triaging_*.html                 │
│                                                                  │
│  8. Send Email Alert                                             │
│     └── SMTP via smtp.gmail.com:587                              │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Current Test Results (XOPS 25 Team)

| Metric | Value |
|--------|-------|
| Team Members Found | 8 |
| Total Capacity/Day | 52.0 hrs/day |
| Sprint Progress | 27.7% elapsed |
| Completion | 0.0% completed |
| Burndown Deviation | -27.7% (HIGH risk) |
| Work Items | 17 total |
| Email Status | ✅ Sent successfully |

---

## Remaining Enhancements

### Priority 1: Nice-to-Have
1. **Daily Hour Breakdown** - Currently aggregates; daily granularity requires `wit_get_work_item_updates` which returns empty data from MCP server
2. **Historical Capacity Trending** - Compare current sprint capacity to previous sprints
3. **Configurable Recipients** - Move email recipients to config.yaml

### Priority 2: Future Enhancements
1. **Integration with External Attendance** - Connect to HR systems for leave data
2. **Slack/Teams Notifications** - Alternative to email alerts
3. **Interactive Dashboard** - Real-time capacity monitoring UI

---

## Files Modified

| File | Changes |
|------|---------|
| `scripts/capacity_triaging.py` | Added fallback capacity extraction, fixed emojis, fixed email recipients |
| `utilities/emailer.py` | Replaced emojis with ASCII for Windows compatibility |
| `utilities/capacity_data_sources.py` | Added `ADOTimeLogCapacitySource` class |
| `agents/pm_agent/agent.py` | Fixed git merge conflict markers |
| `docs/CAPACITY_TRIAGING_MULTI_SOURCE.md` | Added Option 2 for ADO Time Logs |
| `docs/ADO_TIMELOG_IMPLEMENTATION.md` | Created implementation guide |

---

## Testing Checklist

- [x] Capacity data fetched from work items when ADO Sprint Capacity is empty
- [x] 8 team members detected with 52.0 hrs/day total capacity
- [x] HTML report generated with correct metrics
- [x] Email sent successfully to walkingtree.tech addresses
- [x] Risk detection working (BURNDOWN_DEVIATION at -27.7%)
- [x] Corrective actions suggested in report
- [x] No emoji encoding errors on Windows

---

*Last Updated: January 7, 2026*
