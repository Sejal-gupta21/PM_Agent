# Confidence System Reference

## Quick Guide for Understanding Chatbot Confidence Levels

### How It Works

The PM Agent chatbot uses a confidence-based routing system to determine how to handle your queries:

1. **Query Analysis**: When you ask a question, the system normalizes it and detects your intent
2. **Confidence Calculation**: Based on pattern matching and semantic analysis, a confidence score (0.0-1.0) is assigned
3. **Smart Routing**: Depending on confidence, the system routes to the appropriate handler

---

## Confidence Levels & Behavior

| Confidence Range | System Behavior | User Experience |
|-----------------|----------------|----------------|
| **≥ 0.85 (High)** | Direct routing to appropriate agent/skill | Clean response, no indicators |
| **0.70 - 0.84 (Medium-High)** | Route with confidence indicator | "✓ **Reasonably Confident**" prefix |
| **0.50 - 0.69 (Medium)** | Route with hint + indicator | "ℹ️ **Medium Confidence**" prefix + suggestion to verify |
| **< 0.50 (Low)** | **Request clarification** | "⚠️ I'm not very certain..." + clarification question |

---

## Examples

### High Confidence (≥ 0.85)
```
User: "show me recurring bugs"
Confidence: 0.95
Response: [Direct answer with bug list]
```

### Medium Confidence (0.50-0.69)
```
User: "anything broken?"
Confidence: 0.67
Response: 
ℹ️ **Medium Confidence**
I believe this is correct, but please double-check if critical.

Here are the potentially broken items:
[Results...]
```

### Low Confidence (< 0.50) - Clarification Requested
```
User: "stuff"
Confidence: 0.35
Response:
⚠️ **Low Confidence Response**
I'm not very certain about this answer. Please verify the results.

I think you might be asking about work items, but I'm not sure. 
Could you please rephrase your question? For example:
- "show me work items in the current sprint"
- "what are the blocked items?"
- "show me my assigned tasks"
```

---

## Tips for Getting High-Confidence Responses

### Good Queries (High Confidence)
✅ "show me recurring bugs"  
✅ "what's the current sprint status?"  
✅ "work items assigned to john.doe@company.com"  
✅ "show me blocked items in the current iteration"  
✅ "generate the iteration report"  

### Vague Queries (Low Confidence - Will Trigger Clarification)
❌ "stuff"  
❌ "things"  
❌ "what's up"  
❌ "anything?"  

### How to Improve Vague Queries
1. **Add specificity**: Instead of "stuff", say "work items"
2. **Add context**: Instead of "status", say "sprint status"
3. **Use names**: Instead of "john", use "john.doe@company.com"
4. **Use keywords**: "blocked", "recurring bugs", "iteration report", etc.

---

## Clarification Flow

When the system requests clarification:

1. **System detects low confidence** (< 0.50)
2. **System stores your original query** in conversation memory
3. **System asks for clarification** with specific examples
4. **You respond** with more details or select from options
5. **System reconstructs the query** using your clarification
6. **System executes** the resolved query with high confidence

### Example Clarification Flow

```
User: "work items for richa"
System: I found multiple people matching 'richa'. Which one do you mean?
        1. Richa Rathore (richa.rathore@company.com)
        2. Richa Sharma (richa.sharma@company.com)
        
        Please reply with the number or full email/name.

User: "1"
System: [Reconstructs query as "work items for Richa Rathore"]
        [Executes query with high confidence]
        
        Here are the work items assigned to Richa Rathore:
        [Results...]
```

---

## Confidence Indicators

### Meaning of Each Indicator

**⚠️ Low Confidence Response**
- System is **not confident** about its understanding
- **Verify the results** before taking action
- Consider rephrasing your question

**ℹ️ Medium Confidence**
- System **believes it's correct** but not certain
- **Double-check if critical** (e.g., before deleting items)
- Results are likely accurate but may need verification

**✓ Reasonably Confident**
- System is **reasonably confident** (70-85%)
- Results are **likely accurate**
- Usually safe to trust, but verify if making important decisions

**No Indicator (High Confidence)**
- System is **very confident** (≥85%)
- Results are **highly reliable**
- Safe to trust and act upon

---

## Performance Optimizations

### Query Caching

The system caches normalized queries for faster response times:

- **First Query**: "what's up with the sprint?" → Normalized and cached
- **Repeated Query**: "what's up with the sprint?" → Retrieved from cache (instant)

**Cache Size**: 256 most recent queries  
**Benefit**: 30-50% faster response for repeated queries

### When Cache is Cleared

- Automatically when cache reaches capacity (LRU eviction)
- Manually during testing/debugging
- Not cleared between conversation turns (persists for session)

---

## Troubleshooting

### "Why am I getting low-confidence responses?"

**Possible Reasons**:
1. Query is too vague (e.g., "stuff", "things")
2. Using abbreviations without context (e.g., "richa" instead of full name)
3. Novel query pattern not seen before
4. Ambiguous intent (could mean multiple things)

**Solutions**:
1. Add more specific keywords
2. Use full names/emails
3. Provide context (e.g., "in current sprint", "for project X")
4. Rephrase using examples from the clarification message

### "Why is the system asking for clarification?"

**This is intentional!** The system is designed to ask rather than guess when uncertain.

**Better to**:
- Ask for clarification and get the right answer
- Than guess and provide wrong information

**To avoid clarifications**:
- Use specific, detailed queries
- Follow the examples in "Good Queries" section above
- Respond to clarification prompts to help the system learn

---

## Advanced: Conversation Context

The system tracks conversation context to improve confidence over time:

**Example**:
```
User: "show me sprint items"
System: [Fetches sprint items with high confidence]

User: "what about blocked ones?"
System: [Uses context: knows you're still talking about sprint items]
        [Confidence boosted by conversation history]
        [Shows blocked sprint items]
```

This context-aware behavior improves confidence and reduces clarification requests in multi-turn conversations.

---

## Summary

- **High confidence (≥0.85)**: Direct answer, no indicator
- **Medium confidence (0.50-0.84)**: Answer with confidence indicator
- **Low confidence (<0.50)**: Clarification request
- **Clarification flow**: Stores context → asks for details → reconstructs query
- **Caching**: Repeated queries are faster
- **Context-aware**: Uses conversation history to improve confidence

For best results: **Be specific, use keywords, provide context**
