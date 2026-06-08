# 3-Step Intent Deciphering Flow Implementation

## Overview

This document describes the implementation of a proper 3-step cascading fallback approach for query intent deciphering in the PM Agent orchestrator's router. The flow ensures that queries are processed through increasingly sophisticated matching techniques, with each step only invoked if the previous steps fail.

## Architecture Flow

```
User Query
    ↓
Controller (chatbot_controller.py)
    ↓
Orchestrator (orchestrator/router.py)
    ↓
┌─────────────────────────────────────────────────────────────────┐
│ STEP 1: REGEX PATTERN MATCHING (Primary - Fastest)              │
│   - Explicit skill patterns (PM Skills Agent)                   │
│   - ADO patterns (PM Agent)                                     │
│   - Hybrid flow patterns                                        │
│   → If matched: Return RouteDecision immediately                │
│   → If not matched: Proceed to Step 2                           │
├─────────────────────────────────────────────────────────────────┤
│ STEP 2: SEMANTIC MATCHING (Fallback - Embedding-based)          │
│   - Compute query embedding (OpenAI or TF-IDF fallback)         │
│   - Compare against skill embeddings                            │
│   - Apply heuristic boosts for keyword overlap                  │
│   → High confidence (>=0.65): Route directly                    │
│   → Medium confidence (>=0.45): Store hint, proceed to Step 3   │
│   → Low confidence (<0.45): Proceed to Step 3                   │
├─────────────────────────────────────────────────────────────────┤
│ STEP 3: LIGHT LLM PLANNER (Final Fallback - Intelligent)        │
│   - ONLY invoked when Steps 1 & 2 fail                          │
│   - LLM receives context about failed attempts                  │
│   - Uses gpt-3.5-turbo for cost efficiency                      │
│   → High confidence (>=0.80): Accept routing hint               │
│   → Medium confidence (>=0.55): Pass to deep planner            │
│   → Low confidence (<0.55): Fall through to PM Agent            │
└─────────────────────────────────────────────────────────────────┘
    ↓
PM Agent Deep Planner (if all steps fail or low confidence)
```

## Key Implementation Details

### 1. Router Tracking of Attempts

The router now tracks which matching steps were attempted in a `routing_attempts` dictionary:

```python
routing_attempts = {
    "regex_matched": False,
    "regex_skill": None,
    "semantic_matched": False,
    "semantic_skill": None,
    "semantic_score": 0.0,
    "normalized_intent": None,
    "normalized_confidence": 0.0,
}
```

This context is passed to:
- Light LLM Planner (Step 3)
- PM Agent deep planner (final fallback)
- Stored in `RouteDecision.params` for observability

### 2. Light LLM Planner Context Awareness

The Light LLM Planner now receives enhanced context about what was tried:

```python
light_planner_context = {
    **context,
    "routing_attempts": routing_attempts,
    "regex_failed": not routing_attempts["regex_matched"],
    "semantic_failed": not routing_attempts["semantic_matched"],
    "semantic_best_skill": routing_attempts.get("semantic_skill"),
    "semantic_best_score": routing_attempts.get("semantic_score", 0.0),
    "normalized_intent": routing_attempts.get("normalized_intent"),
    "normalized_confidence": routing_attempts.get("normalized_confidence", 0.0),
}
```

### 3. Enhanced System Prompt

The Light LLM Planner's system prompt now explicitly states its role:

```
You are the THIRD step in a 3-step intent deciphering fallback chain:
1. Regex pattern matching - ALREADY TRIED AND FAILED
2. Semantic embedding matching - ALREADY TRIED AND FAILED (or low confidence)
3. Light LLM Planner (YOU) - Make a smart routing decision

Since regex and semantic matching already failed, you should:
- Consider the query holistically, not just keywords
- Look for implicit intent that pattern matching might miss
- If semantic matching found a partial match, consider if that's correct
- Be conservative - if unsure, route to pm_agent with low confidence
```

### 4. Enhanced User Prompt

The user prompt now includes information about prior attempts:

```
Query: [user query]

Context:
- Project: [project name]
- Team: [team name]
- Skill hint: [from context]
- Intent hint: [from context]

PRIOR MATCHING ATTEMPTS (already tried, failed):
- Regex pattern matching: FAILED (no explicit patterns matched)
- Semantic matching: LOW CONFIDENCE (best match: 'developer_skills' with score 0.52)

Since both regex and semantic matching failed, use your understanding of the query to determine intent.

Classify this query and return the routing decision as JSON.
```

## Modified Files

### 1. `orchestrator/router.py`

**Changes:**
- Updated module docstring to describe 3-step flow
- Added `routing_attempts` tracking dictionary
- Restructured `route()` method to follow strict 3-step fallback
- Updated logging to clearly indicate which step is being executed
- Modified `_try_light_planner()` to accept and use routing attempts context
- Updated all `RouteDecision` reason messages to indicate which step matched

**Key Methods:**
- `route()`: Main routing logic with 3-step fallback
- `_try_light_planner()`: Invokes light LLM planner with context
- `_match_skill()`: Step 1a - PM skill pattern matching
- `_matches_pm_agent()`: Step 1b - PM agent pattern matching
- `_match_hybrid()`: Step 1c - Hybrid flow pattern matching
- `_semantic_match_skill()`: Step 2 - Semantic embedding matching

### 2. `utilities/llm/light_planner.py`

**Changes:**
- Updated module docstring to describe position in 3-step flow
- Modified `LIGHT_PLANNER_SYSTEM_PROMPT` to include context about failed attempts
- Updated `call_light_planner_for_routing()` to extract routing attempts from context
- Built enhanced user prompt with "PRIOR MATCHING ATTEMPTS" section
- Added logging to show which prior steps failed

**Key Functions:**
- `call_light_planner_for_routing()`: Main entry point, builds enhanced prompt
- `_fallback_heuristic_routing()`: Pattern-based fallback (unchanged)
- `should_invoke_light_planner()`: Determines if planner should run

## Configuration

The light planner can be configured in `config.yaml`:

```yaml
orchestrator:
  light_planner:
    enabled: true                    # Enable/disable light planner
    model: "gpt-3.5-turbo"          # LLM model to use
    max_tokens: 150                 # Max output tokens
    temperature: 0.0                # Temperature (0.0 for deterministic)
    accept_threshold: 0.80          # Accept hint if confidence >= this
    escalate_threshold: 0.55        # Escalate to deep planner if >= this
    cache_ttl_seconds: 3600         # Cache TTL
    use_fallback_heuristic: true    # Use pattern fallback if LLM fails
```

## Benefits

### 1. **Proper Separation of Concerns**
- Regex matching stays in router.py (not in skill_registry or tool_registry)
- Semantic matching uses skill_registry for embeddings
- Light LLM planner is separate, focused module

### 2. **Context-Aware Decision Making**
- Light LLM planner knows that prior steps failed
- Can make smarter decisions with this context
- Avoids repeating the same logic regex/semantic already tried

### 3. **Cost & Performance Optimization**
- Regex is fastest (microseconds)
- Semantic matching is fast (embedding lookup + cosine similarity)
- Light LLM only called when needed (cheaper model, gpt-3.5-turbo)
- Deep planner is absolute last resort (expensive model)

### 4. **Observability**
- Clear logging at each step: `[ROUTER] Step 1:`, `[ROUTER] Step 2:`, etc.
- Routing attempts tracked and passed to agents
- Langfuse spans created for all LLM calls
- `RouteDecision.reason` indicates which step matched

### 5. **Fallback Safety**
- If light planner disabled, falls back to PM Agent
- If OpenAI unavailable, uses pattern-based heuristic
- If all steps fail, PM Agent deep planner handles it

## Example Execution Flow

### Example 1: Regex Match (Step 1 Success)

```
User: "show billing deviation"
  ↓ Step 1: Regex matching
    ✓ Matched pattern: r"billing\s+deviation"
    → RouteDecision(agent=PM_SKILL_AGENT, skill="billing_deviation", confidence=0.95)
    [Steps 2 & 3 not invoked]
```

### Example 2: Semantic Match (Step 1 Fail, Step 2 Success)

```
User: "who has Angular experience"
  ↓ Step 1: Regex matching
    ✗ No patterns matched
  ↓ Step 2: Semantic matching
    ✓ High confidence match: skill="developer_skills", score=0.78
    → RouteDecision(agent=PM_SKILL_AGENT, skill="developer_skills", confidence=0.78)
    [Step 3 not invoked]
```

### Example 3: Light LLM Planner (Steps 1 & 2 Fail, Step 3 Success)

```
User: "which tasks are causing problems"
  ↓ Step 1: Regex matching
    ✗ No patterns matched
  ↓ Step 2: Semantic matching
    ✗ Low confidence (best: "overlooked_stories", score=0.42)
  ↓ Step 3: Light LLM Planner
    ✓ LLM decision: route=pm_agent, confidence=0.75
    → RouteDecision(agent=PM_AGENT, confidence=0.75)
```

### Example 4: All Steps Fail (Deep Planner)

```
User: "xyz abc 123"  (nonsense query)
  ↓ Step 1: Regex matching
    ✗ No patterns matched
  ↓ Step 2: Semantic matching
    ✗ No match above threshold
  ↓ Step 3: Light LLM Planner
    ✗ Low confidence (0.30)
  ↓ Final Fallback: PM Agent Deep Planner
    → RouteDecision(agent=PM_AGENT, needs_planner=True, confidence=0.3)
```

## Testing

To test the implementation:

1. **Test Regex Matching:**
   ```
   "show billing deviation"
   "recurring bugs"
   "list all work items"
   ```
   Expected: Step 1 match, high confidence

2. **Test Semantic Matching:**
   ```
   "who knows Angular"
   "developer expertise with React"
   "tech stack of the team"
   ```
   Expected: Step 1 fail, Step 2 high confidence match

3. **Test Light LLM Planner:**
   ```
   "what's slowing us down"
   "which tasks are problematic"
   "any blockers"
   ```
   Expected: Steps 1 & 2 fail, Step 3 LLM decision

4. **Test Fallback:**
   ```
   "asdf qwer zxcv"  (nonsense)
   ```
   Expected: All steps fail, PM Agent deep planner

## Observability

Check logs for step-by-step execution:

```
[ROUTER] Step 1: Trying regex pattern matching for: 'show billing...'
[ROUTER] Step 1 SUCCESS: Regex matched billing_deviation

[ROUTER] Step 1: Trying regex pattern matching for: 'who knows Angular...'
[ROUTER] Step 1 FAILED: No regex patterns matched, proceeding to semantic matching
[ROUTER] Step 2: Trying semantic matching for: 'who knows Angular...'
[ROUTER] Step 2 SUCCESS: Semantic match (HIGH confidence): skill=developer_skills, score=0.782

[ROUTER] Step 1 FAILED: No regex patterns matched, proceeding to semantic matching
[ROUTER] Step 2 FAILED: No semantic match above threshold
[ROUTER] Step 3: Invoking Light LLM Planner (regex_matched=False, semantic_matched=False)
[ROUTER] Step 3 result: route=pm_agent, skill=None, confidence=0.75, hint=Query about work items...
[ROUTER] Step 3 SUCCESS: Light LLM Planner returned decision
```

## Conclusion

The implementation properly separates the 3 intent deciphering steps in the router.py file, ensuring that:

1. **Regex patterns** are tried first (fastest, most precise)
2. **Semantic matching** is used as fallback (embedding-based, fuzzy)
3. **Light LLM planner** is the final intelligent fallback (context-aware, knows prior steps failed)

The Light LLM planner now understands that it's being invoked because regex and semantic matching already failed, allowing it to make more informed routing decisions based on this context.
