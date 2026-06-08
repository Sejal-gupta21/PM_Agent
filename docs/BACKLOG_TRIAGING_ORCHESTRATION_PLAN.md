# Backlog Triaging Orchestration - Implementation Plan

## 📋 Executive Summary

Transform backlog triaging from a direct script execution to an intelligent, multi-agent orchestrated workflow using LLM-based planning and modular tool composition.

**Goal**: Enable natural language queries like "give me the backlog triage report" or "how is my backlog health" to automatically:
1. Break down into smaller tasks via LLM Planner
2. Select appropriate tools from PM Skill Agent + MCP Tools
3. Execute in optimal sequence
4. Synthesize results into user-friendly response

---

## 🏗️ Current Architecture Analysis

### Existing Components (Assets We'll Leverage)

| Component | Location | Purpose | Status |
|-----------|----------|---------|--------|
| **MultiToolOrchestrator** | `agents/pm_agent/multi_tool_orchestrator.py` | Executes complex multi-tool queries | ✅ Exists |
| **ToolExecutor** | `utilities/mcp/tool_registry.py` | Executes MCP tools with pagination | ✅ Exists |
| **MCP_TOOL_REGISTRY** | `utilities/mcp/tool_registry.py` | 77 MCP tools (ADO API) | ✅ Exists |
| **PM Skill Agent** | `agents/pm_skill_agent/skills.py` | Business logic skills | ✅ Exists |
| **SKILL_DEFINITIONS** | `agents/pm_skill_agent/skills.py` | Skill metadata | ✅ Exists |
| **LLM Planner** | `agents/pm_agent/llm.py` (`plan_mcp_call`) | Creates execution plans | ✅ Exists |
| **Router** | `orchestrator/router.py` | Pattern-based routing | ✅ Exists |
| **Backlog Triaging Script** | `scripts/backlog_triaging.py` | Current direct implementation | ✅ Exists |

### Current Backlog Triaging Flow

```
User asks "backlog report"
    ↓
Router pattern match → PM Skill Agent (get_backlog_health)
    ↓
PM Skill Agent → Direct call to scripts/backlog_triaging.py
    ↓
Script executes all steps internally (hardcoded)
    ↓
Returns formatted report
```

**Limitations:**
- ❌ Monolithic - no task breakdown
- ❌ Hardcoded flow - no flexibility
- ❌ No LLM planning - can't adapt to variations
- ❌ Single tool calls - no orchestration

---

## 🎯 Target Architecture

### Desired Orchestrated Flow

```
User Query: "give me the backlog triage report for XOPS 25"
    ↓
┌─────────────────────────────────────────────────────────────┐
│ 1. ROUTER (Intent Detection)                                │
│    - Detects: backlog health query                          │
│    - Routes to: Backlog Triaging Orchestrator                │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. LLM PLANNER (Task Breakdown)                             │
│    Input: User query + Available tools                      │
│    Output: Execution plan with steps                        │
│                                                              │
│    Steps Generated:                                          │
│    1. Get team area path (for filtering)                    │
│    2. Fetch backlog work items (state = New/Proposed)       │
│    3. Get iterations for velocity calculation               │
│    4. Fetch completed work items (for velocity)             │
│    5. Estimate unestimated items (LLM)                       │
│    6. Calculate backlog health metrics                       │
│    7. Generate recommendations                               │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. TOOL SELECTOR (For Each Step)                            │
│    - Maps step to tool (MCP or PM Skill)                    │
│    - Resolves dependencies                                   │
│                                                              │
│    Step 1: core_get_team_area_path (MCP)                    │
│    Step 2: search_workitem (MCP)                            │
│    Step 3: work_list_team_iterations (MCP)                  │
│    Step 4: search_workitem (MCP)                            │
│    Step 5: estimate_work_items (PM Skill - LLM)             │
│    Step 6: calculate_backlog_metrics (PM Skill)             │
│    Step 7: generate_recommendations (PM Skill)               │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. ORCHESTRATOR (Execution)                                 │
│    - Executes tools in dependency order                     │
│    - Handles parallel execution where possible              │
│    - Manages context/data flow between steps                │
│    - Error handling with retries                            │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────┐
│ 5. SYNTHESIZER (Result Composition)                         │
│    - Combines results from all steps                        │
│    - Formats into user-friendly response                    │
│    - Uses LLM for natural language generation               │
└─────────────────────────────────────────────────────────────┘
    ↓
User-friendly backlog health report
```

---

## 📝 Detailed Implementation Plan

### Phase 1: Foundation - Tool Definitions & Registry Enhancement

**Goal**: Make backlog triaging steps discoverable as individual tools

#### Task 1.1: Create Backlog Triaging Tool Definitions
**File**: `agents/pm_skill_agent/backlog_tools.py` (NEW)

```python
# Define granular tools for each backlog triaging step
BACKLOG_TOOL_DEFINITIONS = {
    "get_backlog_items": {
        "description": "Fetch unrefined backlog work items (New/Proposed state)",
        "required_args": ["project", "team"],
        "optional_args": ["areaPath"],
        "output": "List of work items with effort estimates",
        "dependencies": ["core_get_team_area_path"]  # Needs area path first
    },
    "calculate_team_velocity": {
        "description": "Calculate team velocity from recent completed sprints",
        "required_args": ["project", "team"],
        "optional_args": ["sprintCount"],
        "output": "Average velocity in story points/sprint",
        "dependencies": ["work_list_team_iterations", "search_workitem"]
    },
    "estimate_backlog_items": {
        "description": "Use LLM to estimate unestimated work items",
        "required_args": ["workItems", "velocity"],
        "output": "Work items with estimated effort",
        "is_llm_tool": True  # Indicator for LLM-based tool
    },
    "calculate_backlog_health": {
        "description": "Calculate backlog health metrics (thin/healthy)",
        "required_args": ["backlogItems", "velocity"],
        "output": "Health status, metrics, recommendations"
    }
}
```

**Testing Strategy**:
- ✅ Verify tool definitions have all required fields
- ✅ Check dependency graph is acyclic
- ✅ Validate arg types match expected inputs

---

#### Task 1.2: Extend PM Skill Agent SKILL_DEFINITIONS
**File**: `agents/pm_skill_agent/skills.py`

Add backlog triaging skills:
```python
"get_backlog_items": SkillDefinition(
    name="get_backlog_items",
    handler=get_backlog_items_handler,
    description="Fetch backlog work items for triaging",
    # ... metadata
),
"calculate_team_velocity": SkillDefinition(
    name="calculate_team_velocity",
    handler=calculate_velocity_handler,
    # ...
),
# ... etc
```

**Testing Strategy**:
- ✅ Test each skill handler independently
- ✅ Verify skill returns SkillResult with success/error
- ✅ Mock MCP calls, test with sample data

---

### Phase 2: LLM Planner Enhancement

**Goal**: Teach planner to break down backlog queries into steps

#### Task 2.1: Create Backlog Query Planning Prompt
**File**: `agents/pm_agent/prompts/backlog_planning_prompt.md` (NEW)

```markdown
# Backlog Triaging Query Planning

You are an intelligent planner for backlog health analysis.

Available Tools:
- MCP Tools: {{MCP_TOOL_REGISTRY}}
- PM Skills: {{SKILL_DEFINITIONS}}

User Query: {{user_query}}

Break this query into executable steps:
1. Identify required data (backlog items, velocity, etc.)
2. Map each data need to a tool
3. Determine execution order (dependencies)
4. Specify output format

Output JSON:
{
  "steps": [
    {
      "step_id": "1",
      "tool": "core_get_team_area_path",
      "args": {"project": "...", "team": "..."},
      "purpose": "Get area path for filtering",
      "dependencies": []
    },
    // ... more steps
  ],
  "final_output": "backlog_health_report"
}
```

**Testing Strategy**:
- ✅ Test with variations: "backlog report", "how is my backlog", "is backlog thin"
- ✅ Verify planner generates correct step sequence
- ✅ Check dependencies are correctly identified

---

#### Task 2.2: Implement Backlog-Specific Planner Function
**File**: `agents/pm_agent/llm.py`

```python
async def plan_backlog_triaging(
    query: str,
    context: Dict[str, Any],
    tool_registry: Dict[str, Dict[str, Any]],
    skill_definitions: Dict[str, SkillDefinition]
) -> Dict[str, Any]:
    """
    Generate execution plan for backlog triaging queries.
    
    Returns:
        {
            "steps": [...],
            "execution_mode": "sequential" | "parallel",
            "expected_duration": "estimate",
            "confidence": 0.0-1.0
        }
    """
    # 1. Load planning prompt
    # 2. Inject available tools
    # 3. Call LLM (GPT-4)
    # 4. Parse & validate plan
    # 5. Return structured plan
```

**Testing Strategy**:
- ✅ Unit test with mock LLM responses
- ✅ Integration test with real LLM (GPT-4)
- ✅ Verify plan validation catches malformed outputs

---

### Phase 3: Orchestrator Integration

**Goal**: Execute the generated plan using existing orchestrator

#### Task 3.1: Extend MultiToolOrchestrator for Backlog Plans
**File**: `agents/pm_agent/multi_tool_orchestrator.py`

Add method:
```python
async def execute_backlog_plan(
    self,
    plan: Dict[str, Any],
    context: Dict[str, Any]
) -> ExecutionResult:
    """
    Execute backlog triaging plan with:
    - MCP tool calls via ToolExecutor
    - PM Skill calls via PM Skill Agent
    - Data flow management between steps
    - Result aggregation
    """
```

**Testing Strategy**:
- ✅ Test with synthetic plan (hardcoded steps)
- ✅ Test with LLM-generated plan
- ✅ Verify data flows correctly between steps
- ✅ Test error handling (MCP failure, LLM timeout)

---

#### Task 3.2: Create Tool Execution Dispatcher
**File**: `agents/pm_agent/tool_dispatcher.py` (NEW)

```python
class ToolDispatcher:
    """Route tool execution to appropriate handler."""
    
    def __init__(self, tool_executor, pm_skill_agent):
        self.tool_executor = tool_executor  # For MCP tools
        self.pm_skill_agent = pm_skill_agent  # For PM skills
        
    async def dispatch(self, tool_name: str, args: Dict) -> Any:
        """Execute tool via appropriate handler."""
        if tool_name in MCP_TOOL_REGISTRY:
            return await self._execute_mcp_tool(tool_name, args)
        elif tool_name in SKILL_DEFINITIONS:
            return await self._execute_pm_skill(tool_name, args)
        else:
            raise ValueError(f"Unknown tool: {tool_name}")
```

**Testing Strategy**:
- ✅ Test MCP tool dispatch
- ✅ Test PM Skill dispatch
- ✅ Test error for unknown tool
- ✅ Verify correct handler selected for each tool type

---

### Phase 4: Result Synthesis

**Goal**: Convert raw tool outputs into user-friendly reports

#### Task 4.1: Create Backlog Health Synthesizer
**File**: `agents/pm_agent/synthesizers/backlog_synthesizer.py` (NEW)

```python
class BacklogHealthSynthesizer:
    """Convert backlog metrics into user-friendly report."""
    
    async def synthesize(
        self,
        execution_results: List[Dict[str, Any]],
        original_query: str
    ) -> str:
        """
        Generate natural language report from execution results.
        
        Uses LLM to:
        - Summarize key metrics
        - Highlight risks
        - Provide recommendations
        - Format in readable structure
        """
```

**Testing Strategy**:
- ✅ Test with sample execution results
- ✅ Verify report includes all key sections
- ✅ Check formatting is readable
- ✅ Test with different health statuses (thin/healthy/critical)

---

### Phase 5: Router Integration

**Goal**: Connect backlog queries to orchestrated flow

#### Task 5.1: Update Router Patterns
**File**: `orchestrator/router.py`

Add pattern:
```python
# Backlog Triaging (orchestrated)
(r"backlog\s+(health|status|report|triage)", "backlog_triaging_orchestrated", {
    "use_orchestrator": True,
    "planner": "backlog_planner"
}),
```

**Testing Strategy**:
- ✅ Test pattern matching for various queries
- ✅ Verify routes to orchestrator, not direct skill
- ✅ Check context is preserved

---

#### Task 5.2: Create Backlog Orchestration Handler
**File**: `agents/pm_agent/handlers/backlog_handler.py` (NEW)

```python
async def handle_backlog_query(
    query: str,
    context: Dict[str, Any],
    orchestrator: MultiToolOrchestrator
) -> str:
    """
    Main entry point for orchestrated backlog queries.
    
    Flow:
    1. Call LLM planner to break down query
    2. Validate plan
    3. Execute via orchestrator
    4. Synthesize results
    5. Return formatted report
    """
```

**Testing Strategy**:
- ✅ End-to-end test with real query
- ✅ Verify all phases execute correctly
- ✅ Check final output format
- ✅ Test error scenarios

---

### Phase 6: Integration & End-to-End Testing

#### Task 6.1: Integration Test Suite
**File**: `tests/integration/test_backlog_orchestration.py` (NEW)

Test cases:
```python
async def test_backlog_health_query():
    """Test: 'how is my backlog health for XOPS 25'"""
    
async def test_backlog_triage_report():
    """Test: 'give me the backlog triage report'"""
    
async def test_backlog_thin_detection():
    """Test: 'is my backlog running thin?'"""
    
async def test_error_handling():
    """Test: MCP failure, LLM timeout scenarios"""
```

**Testing Strategy**:
- ✅ Test with real ADO data
- ✅ Verify report accuracy vs direct script
- ✅ Check performance (should be < 30s)
- ✅ Test with different teams/projects

---

#### Task 6.2: Performance Benchmarking
**File**: `tests/performance/benchmark_backlog.py` (NEW)

Metrics:
- Planning time (LLM call)
- Tool execution time (sequential vs parallel)
- Synthesis time
- Total end-to-end time

**Testing Strategy**:
- ✅ Compare orchestrated vs direct script
- ✅ Identify bottlenecks
- ✅ Optimize slow steps

---

### Phase 7: Migration & Backwards Compatibility

#### Task 7.1: Gradual Migration Strategy

**Option A: Feature Flag**
```python
USE_ORCHESTRATED_BACKLOG = os.getenv("USE_ORCHESTRATED_BACKLOG", "false") == "true"

if USE_ORCHESTRATED_BACKLOG:
    result = await handle_backlog_query(query, context, orchestrator)
else:
    result = await run_backlog_triaging_script(query, context)
```

**Option B: Router-based**
```python
# Old pattern (direct)
(r"backlog\s+report", "get_backlog_health", {}),

# New pattern (orchestrated)
(r"(smart|detailed)\s+backlog\s+report", "backlog_triaging_orchestrated", {}),
```

**Testing Strategy**:
- ✅ Test both paths work
- ✅ Verify feature flag toggles correctly
- ✅ Check no regressions in direct path

---

## 🔧 Technical Implementation Details

### Data Flow Example

```
User: "give me backlog triage report for XOPS 25"
    ↓
LLM Planner Output:
{
  "steps": [
    {
      "step_id": "1",
      "tool": "core_get_team_area_path",
      "args": {"project": "FracPro-OPS", "team": "XOPS 25"},
      "purpose": "Get area path",
      "output_var": "area_path"
    },
    {
      "step_id": "2", 
      "tool": "search_workitem",
      "args": {
        "searchText": "*",
        "project": ["FracPro-OPS"],
        "areaPath": ["${area_path}"],  # Reference step 1 output
        "state": ["New", "Proposed"],
        "workItemType": ["User Story", "Bug"]
      },
      "purpose": "Fetch backlog items",
      "output_var": "backlog_items",
      "dependencies": ["1"]
    },
    {
      "step_id": "3",
      "tool": "calculate_team_velocity",
      "args": {"project": "FracPro-OPS", "team": "XOPS 25"},
      "purpose": "Get velocity",
      "output_var": "velocity",
      "dependencies": []  # Can run parallel with step 2
    },
    {
      "step_id": "4",
      "tool": "estimate_backlog_items",
      "args": {
        "workItems": "${backlog_items}",
        "velocity": "${velocity}"
      },
      "purpose": "Estimate unestimated items",
      "dependencies": ["2", "3"]
    },
    {
      "step_id": "5",
      "tool": "calculate_backlog_health",
      "args": {
        "backlogItems": "${backlog_items}",
        "velocity": "${velocity}"
      },
      "purpose": "Calculate health metrics",
      "dependencies": ["4"]
    }
  ]
}
    ↓
Orchestrator executes:
- Steps 1, 3 in parallel (no dependencies)
- Step 2 after step 1 completes
- Step 4 after steps 2, 3 complete
- Step 5 after step 4 completes
    ↓
Synthesizer formats:
"Backlog Health Report for XOPS 25:
- Total backlog items: 11
- Unestimated items: 0
- Total effort: 308 points
- Team velocity: 200 points/sprint
- Backlog depth: 1.54 sprints
- Status: THIN ⚠️
- Recommendation: Add more refined items..."
```

---

## 📊 Testing Matrix

| Test Type | Scope | Tools | Success Criteria |
|-----------|-------|-------|------------------|
| **Unit Tests** | Individual tools | pytest | All tool handlers pass |
| **Integration Tests** | Tool → Tool flow | pytest + mock MCP | Data flows correctly |
| **E2E Tests** | Full orchestration | Real ADO data | Accurate reports |
| **Performance Tests** | Speed benchmarks | pytest-benchmark | < 30s end-to-end |
| **Regression Tests** | Old vs new | Comparison | Same results |

---

## 📁 File Structure (New Files)

```
pm-agent/
├── agents/
│   ├── pm_agent/
│   │   ├── handlers/
│   │   │   └── backlog_handler.py         [NEW] Entry point
│   │   ├── planners/
│   │   │   └── backlog_planner.py         [NEW] LLM planning
│   │   ├── prompts/
│   │   │   └── backlog_planning.md        [NEW] Planning prompt
│   │   └── synthesizers/
│   │       └── backlog_synthesizer.py     [NEW] Result formatting
│   └── pm_skill_agent/
│       └── backlog_tools.py               [NEW] Tool definitions
├── tests/
│   ├── integration/
│   │   └── test_backlog_orchestration.py  [NEW] Integration tests
│   ├── performance/
│   │   └── benchmark_backlog.py           [NEW] Performance tests
│   └── unit/
│       └── test_backlog_tools.py          [NEW] Unit tests
└── docs/
    └── BACKLOG_TRIAGING_ORCHESTRATION.md  [NEW] This document
```

---

## 🚀 Implementation Phases & Timeline

### Phase 1: Foundation (Week 1)
- [ ] Task 1.1: Create backlog tool definitions
- [ ] Task 1.2: Extend PM Skill Agent
- [ ] Testing: Unit tests for tools

### Phase 2: Planning (Week 2)
- [ ] Task 2.1: Create planning prompt
- [ ] Task 2.2: Implement planner function
- [ ] Testing: Planner output validation

### Phase 3: Orchestration (Week 2-3)
- [ ] Task 3.1: Extend orchestrator
- [ ] Task 3.2: Create tool dispatcher
- [ ] Testing: Orchestration flow

### Phase 4: Synthesis (Week 3)
- [ ] Task 4.1: Create synthesizer
- [ ] Testing: Report formatting

### Phase 5: Integration (Week 4)
- [ ] Task 5.1: Update router
- [ ] Task 5.2: Create handler
- [ ] Testing: End-to-end

### Phase 6: Testing (Week 4-5)
- [ ] Task 6.1: Integration tests
- [ ] Task 6.2: Performance benchmarks

### Phase 7: Migration (Week 5)
- [ ] Task 7.1: Feature flag
- [ ] Testing: Backwards compatibility

---

## ⚠️ Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM planning inconsistency | High | Validate plans, use few-shot examples |
| Performance degradation | Medium | Parallel execution, caching |
| Breaking existing flows | High | Feature flag, gradual rollout |
| Tool selection errors | Medium | Strict validation, fallbacks |
| MCP API failures | Medium | Retry logic, timeouts |

---

## 🎯 Success Metrics

- ✅ Backlog queries work via orchestration
- ✅ Reports match direct script accuracy
- ✅ End-to-end time < 30 seconds
- ✅ No regressions in existing features
- ✅ 95%+ test coverage for new code

---

## 🔄 Future Enhancements

1. **Multi-team Backlog Analysis**: Compare backlogs across teams
2. **Trend Analysis**: Track backlog health over time
3. **Proactive Alerts**: Notify when backlog becomes thin
4. **Custom Metrics**: User-defined health thresholds
5. **Interactive Reports**: Drill-down capabilities

---

*This plan follows industry best practices for agentic AI systems and leverages existing PM Agent architecture for minimal disruption.*
