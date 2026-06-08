# Langfuse Debugging Guide for PM Agent

## Problem: Wrong Tool Selection

### Symptoms in Langfuse
- ❌ Multiple WARNING spans with 0.00s duration
- ❌ Tool calls that don't match the user query intent
- ❌ Missing required arguments errors
- ❌ Agent returns error responses instead of data

### Example from Your Trace
```
Query: "Compare current sprint with previous sprint"
Tool Called: pr_list_pull_requests (WRONG!)
Expected: wit_get_work_items_for_iteration (CORRECT)
```

---

## How to Identify Tool Selection Issues

### Step 1: Expand the Trace Hierarchy

```
controller_request (11.89s)
  └─ orchestrator_process (11.88s)
       └─ orchestrator_planner (2.67s)  ← CHECK THIS SPAN
            └─ llm_planner (2.66s)      ← AND THIS ONE
```

### Step 2: Check `orchestrator_planner` Span

**Input** (what should contain):
```json
{
  "query": "Compare current sprint with previous sprint",
  "available_tools": ["wit_get_work_items_for_iteration", "search_workitem", ...],
  "context": {"project": "FracPro-OPS", "iteration": "@CurrentIteration"}
}
```

**Output** (what the planner decided):
```json
{
  "action": "call_tool",
  "tool": "pr_list_pull_requests",  ← WRONG TOOL!
  "args": {...},
  "confidence": 0.85,
  "reasoning_summary": "..."
}
```

### Step 3: Check `llm_planner` Span Metadata

**Look for**:
- `model_input`: The exact prompt sent to the LLM
- `model_output`: The LLM's raw response
- `reasoning_summary`: Why the LLM chose this tool

**Red flags**:
- Tool name doesn't match query intent
- Missing tools in `available_tools` list
- Incorrect tool descriptions in prompt

### Step 4: Analyze Metadata Fields

**Critical fields to check**:
```json
{
  "tool_selected": "pr_list_pull_requests",  ← What was chosen
  "tool_expected": "wit_get_work_items_for_iteration",  ← What should have been chosen
  "confidence": 0.85,  ← High confidence in wrong choice = instruction problem
  "reasoning_summary": "..."  ← WHY did LLM choose this?
}
```

---

## Root Causes and Fixes

### Cause 1: Tool Registry Missing Sprint Tools

**Check**: Are sprint tools registered in the orchestrator?

**Fix**: Add to tool registry in `multi_tool_orchestrator.py`

```python
self.tool_metadata = {
    "wit_get_work_items_for_iteration": {
        "read_only": True, 
        "can_parallelize": True
    },
    # ... other tools
}
```

### Cause 2: LLM Instructions Unclear

**Check**: Do planner instructions clearly differentiate between:
- Sprint queries → `wit_get_work_items_for_iteration`
- PR queries → `pr_list_pull_requests`

**Fix**: Update planner instructions with explicit examples

### Cause 3: Tool Descriptions Ambiguous

**Check**: Are tool descriptions in the prompt clear?

**Current problem**: If prompt says:
- ❌ "pr_list_pull_requests - Get items for project"
- ❌ "wit_get_work_items_for_iteration - Get work items"

Both sound similar to LLM!

**Fix**: Make descriptions specific:
- ✓ "pr_list_pull_requests - Get pull requests (code reviews) ONLY"
- ✓ "wit_get_work_items_for_iteration - Get work items (bugs, tasks, stories) in a sprint/iteration"

### Cause 4: Available Tools List Wrong

**Check**: Is the planner receiving the correct list of available tools?

**Debug in code**:
```python
# In agents/pm_agent/llm.py or multi_tool_orchestrator.py
logger.debug(f"Available tools sent to LLM: {available_tools}")
```

---

## Debugging Workflow

### 1. Query Fails → Check Langfuse

**Navigate to**:
- Traces → Find your query by timestamp
- Expand trace hierarchy
- Look for WARNING/ERROR spans

### 2. Find the Planner Span

**Key spans**:
- `orchestrator_planner` - Orchestrator's tool selection
- `llm_planner` - LLM's raw tool selection
- `pm_agent_invoke` - Agent execution

### 3. Inspect Input/Output

**For `orchestrator_planner`**:
- **Input tab**: What context was provided?
- **Output tab**: What tool was selected?
- **Metadata tab**: Reasoning, confidence, available tools

**For `llm_planner`**:
- **Input tab**: The full system prompt + user query
- **Output tab**: LLM's raw JSON response
- **Metadata tab**: Model name, temperature, tokens used

### 4. Compare Expected vs Actual

**Create mental checklist**:
```
Query: "Compare current sprint with previous sprint"

Expected tool: wit_get_work_items_for_iteration
Actual tool: pr_list_pull_requests ❌

Why wrong?
- LLM confused "sprint" with "pull requests"
- Tool descriptions too generic
- Missing "iteration" keyword trigger in instructions
```

### 5. Fix and Verify

**After fix**:
1. Run same query again
2. Check Langfuse trace
3. Verify correct tool selected
4. Compare execution time (should be faster if using parallel execution)

---

## Advanced Langfuse Features

### 1. Trace Comparison

**Compare successful vs failed queries**:
- Open 2 traces side-by-side
- Compare tool selection paths
- Identify pattern differences

### 2. Search by Metadata

**Find all failures**:
```
Filter: status = "ERROR"
Metadata: tool_selected = "pr_list_pull_requests"
```

### 3. Annotation

**Mark problematic traces**:
- Click "Annotate" button
- Add label: "Wrong tool selection - PR instead of sprint"
- Track frequency of this issue

### 4. Custom Metadata

**Add to spans for better debugging**:
```python
langfuse_span.update(
    metadata={
        "query_intent": "sprint_comparison",
        "expected_tool": "wit_get_work_items_for_iteration",
        "actual_tool": selected_tool,
        "match": selected_tool == expected_tool,
        "confidence": confidence,
        "reasoning": reasoning_summary
    }
)
```

---

## Specific Fix for Your Issue

### Problem: Sprint queries selecting PR tools

**Location**: `agents/pm_agent/prompts/planner-instructions.md`

**Add explicit section**:

```markdown
## CRITICAL: Sprint vs Pull Request Queries

### Sprint/Iteration Queries (use wit_get_work_items_for_iteration)
- "compare sprint X with Y"
- "current sprint items"
- "previous iteration"
- "what's in sprint 25.22"
- Keywords: sprint, iteration, current, previous, @CurrentIteration

### Pull Request Queries (use pr_list_pull_requests)
- "list pull requests"
- "show PRs"
- "code reviews"
- "merged PRs"
- Keywords: pull request, PR, code review, merge

**NEVER use pr_list_pull_requests for sprint queries!**
```

### Implementation

**Step 1**: Check if planner instructions have clear sprint examples

**Step 2**: Add validation in orchestrator:
```python
def validate_tool_selection(self, query, selected_tool):
    # If query mentions sprint/iteration but tool is PR-related
    sprint_keywords = ['sprint', 'iteration', 'current', 'previous']
    if any(kw in query.lower() for kw in sprint_keywords):
        if selected_tool in ['pr_list_pull_requests', 'pr_get_pull_request']:
            logger.warning(f"Query mentions sprint but selected PR tool: {selected_tool}")
            # Auto-correct or ask for clarification
            return "wit_get_work_items_for_iteration"
    return selected_tool
```

**Step 3**: Test and verify in Langfuse

---

## Quick Reference: Langfuse Navigation

### For Tool Selection Issues

1. **Traces** → Filter by timestamp or query
2. Expand **controller_request**
3. Click **orchestrator_planner** span
4. Check **Input** tab: Available tools, context
5. Check **Output** tab: Selected tool, reasoning
6. Check **Metadata** tab: Confidence, tool descriptions

### For Performance Issues

1. Enable **Timeline** view (toggle at top)
2. Look for **overlapping spans** (parallel execution)
3. Check **span duration** (should be ~500ms for API calls)
4. Compare **total trace time** vs **sum of span times**

### For Error Debugging

1. Filter traces by **status = ERROR**
2. Check **error message** in span output
3. Trace back to **parent span** (usually planner)
4. Check **Input** that caused the error

---

## Success Metrics in Langfuse

### ✅ Correct Tool Selection
- Tool name matches query intent
- No WARNING spans
- Successful completion

### ✅ Parallel Execution Working
- Multiple tool spans with **overlapping timestamps**
- Total time ≈ max(individual_tool_times), not sum
- Logs show "Executing parallel batch"

### ✅ Performance Improvement
- Sprint comparison: < 2s (was 4-5s sequential)
- Multi-tool queries: ~500ms per parallel batch
- No timeout errors

---

## Common Patterns to Watch For

### ❌ Wrong Tool Pattern
```
Query: "sprint items"
Tool: pr_list_pull_requests
Status: ERROR/WARNING
Duration: 0.00s (immediate failure)
```

### ✅ Correct Tool Pattern
```
Query: "sprint items"
Tool: wit_get_work_items_for_iteration
Status: SUCCESS
Duration: 0.5-1s (actual API call)
```

### ❌ Sequential Execution Pattern
```
Tool 1: 0.5s (0.0 - 0.5s)
Tool 2: 0.5s (0.5 - 1.0s)  ← Starts after Tool 1 ends
Tool 3: 0.5s (1.0 - 1.5s)  ← Starts after Tool 2 ends
Total: 1.5s
```

### ✅ Parallel Execution Pattern
```
Tool 1: 0.5s (0.0 - 0.5s)
Tool 2: 0.5s (0.0 - 0.5s)  ← Overlaps with Tool 1!
Tool 3: 0.5s (0.0 - 0.5s)  ← All 3 running at same time!
Total: 0.5s
```

---

## Next Steps

1. ✅ Identify root cause in Langfuse (tool selection issue)
2. ⏳ Update planner instructions (add sprint vs PR clarity)
3. ⏳ Add validation logic (auto-correct common mistakes)
4. ⏳ Test with same query again
5. ⏳ Verify in Langfuse (correct tool + parallel execution)
