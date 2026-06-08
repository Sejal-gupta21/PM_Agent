# KB Section 1 — PM Terminology & Project Conventions

## PM Terminology Glossary

| Term | Definition (ADO-grounded) |
|---|---|
| **Sprint / Iteration** | A fixed time-box for delivering work. In ADO, represented as an Iteration Path (e.g. `FracPro-OPS\2026\Q1\26.01`). Sprint name format: `YY.SS` where YY = 2-digit year, SS = sprint number (e.g. `26.01` = Sprint 1 of 2026). |
| **@CurrentIteration** | ADO macro for the iteration whose date range includes today. Team-scoped. |
| **@PreviousIteration** | ADO macro for the iteration immediately before `@CurrentIteration`. Team-scoped. Equivalent to `@CurrentIteration - 1`. |
| **@CurrentIteration - N** | Offset macro for N sprints before current. `@CurrentIteration - 1` = previous sprint, `@CurrentIteration - 2` = 2 sprints back, etc. Format: spaces around minus sign required. Use for multi-sprint queries (e.g. "last 3 sprints" → offsets -1, -2, -3). |
| **Story Points / Effort** | Estimated size of a work item, stored in `Microsoft.VSTS.Scheduling.StoryPoints`. Used to compute capacity and velocity. May be null (not estimated). |
| **Velocity** | Story points completed (state = Done/Closed/Resolved) in a sprint. Measured per team per sprint. Use completed story points from the previous sprint as the baseline. |
| **Burndown** | Reduction in remaining work (story points or item count) over a sprint. Ideal burndown = linear decrease from total to zero by `finishDate`. |
| **Burndown Rate** | Story points or item count completed per working day. `Rate = completed / working_days_elapsed`. |
| **Completion Rate** | `(closed_items / total_items) × 100`. Use Done, Closed, Resolved, UAT Complete, Completed, Removed states as "closed". |
| **Story Points Completion** | `(completed_story_points / total_story_points) × 100`. Only meaningful when most items have story points. |
| **Days Remaining** | Calendar days from today to the sprint `finishDate`. Negative = sprint overdue. |
| **WIP (Work in Progress)** | Items currently in Active, Design, Code Review, QA, UAT, PRE-PROD states. High WIP with few completions signals bottleneck. |
| **Throughput** | Number of work items completed (closed) per time period. Count-based; does not require story points. |
| **Cycle Time** | Days from when an item enters "Active" to when it reaches "Done/Closed". `days_in_state` is a proxy for this. |
| **Lead Time** | Days from item creation (`System.CreatedDate`) to "Done/Closed". |
| **Done / Definition of Done** | An item is "done" when its `System.State` is one of: `Done`, `Closed`, `Resolved`, `UAT Complete`, `Completed`, `Removed`. |
| **Blocked** | An item is blocked when its state is `On Hold` or `Issues Found`, or it carries a `Blocked` tag. Blocking items need immediate PM attention. |
| **At-Risk** | An item is at-risk when: it has been in its current state > 3 working days without progress, OR the sprint has ≤ 2 days remaining with < 70% completion, OR it is assigned to an overloaded team member. |
| **Scope Change** | Addition or removal of work items after sprint start. Detected by comparing initial committed count vs current total. |
| **Capacity** | Team member's available hours in an iteration. Stored via `work_get_team_capacity` / `work_get_iteration_capacities` MCP tools. |
| **Bug-to-Story Ratio** | `bug_count / story_count`. A ratio > 0.5 signals quality debt. A ratio > 1.0 is a critical quality signal. |
| **Backlog Runway** | `total_backlog_effort / average_velocity` = number of sprints of work available. < 3 sprints = low runway. |
| **Spillover** | Work items not completed in a sprint that move to the next sprint. Indicated by `System.IterationPath` changing after sprint end. |

---

## Project-Specific Conventions

| Convention | Value |
|---|---|
| **ADO Organization** | `Stratagen` |
| **ADO Project** | `FracPro-OPS` |
| **Default Team** | `XOPS 25` (used when no team is specified in query) |
| **Sprint naming format** | `YY.SS` (e.g. `26.01` = year 2026, sprint 01) |
| **Iteration path root** | `FracPro-OPS\<year>\<quarter>\<sprint>` |
| **Work item link format** | `https://dev.azure.com/Stratagen/FracPro-OPS/_workitems/edit/{ID}` |
| **Active work item types** | Bug, User Story, Task, Enhancement, Feature |
| **Priority field** | `Microsoft.VSTS.Common.Priority` — values 1 (highest) to 4 (lowest) |
| **Story points field** | `Microsoft.VSTS.Scheduling.StoryPoints` |
| **Tags field** | `System.Tags` — semicolon-separated |
| **Created date field** | `System.CreatedDate` |
| **Changed date field** | `System.ChangedDate` |

### Valid Teams (do not invent team names)

XOPS 25, XOPS 24, XOPS 23, XOPS 22, XOPS 21, XOPS 20, XOPS 19, XOPS 18, XOPS 17, XOPS 16,
XOPS 15, XOPS 14, XOPS 13, XOPS 12, XOPS 11, XOPS 10, XOPS 9, XOPS 8, XOPS 7, XOPS 6
