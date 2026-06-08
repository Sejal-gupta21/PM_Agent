"""Test skill query handling for the chatbot - verifies task queries are NOT intercepted."""
import sys
sys.path.insert(0, '.')

from app.chat_service import handle_skill_query, get_skill_help

print("=" * 60)
print("TEST 1: Task queries should NOT be intercepted (return None)")
print("=" * 60)

# These are TASK queries - should NOT be handled by skill query, should return None
task_queries = [
    'what is the derailing work item in the current sprint?',
    'show me bugs in the current sprint',
    'get all work items in this iteration',
    'what are the blocked tasks',
    'list the at risk items',
    'find overdue bugs',
    'send email to john@example.com',
    'generate sprint report for sprint 25.25',
    'what is the status of bug 12345',
    'show me the delayed stories',
]

passed = 0
failed = 0

for q in task_queries:
    result = handle_skill_query(q)
    if result is None:
        print(f'\n✅ Query: "{q}"')
        print(f'   Result: None (correctly NOT intercepted - will route to PM Agent)')
        passed += 1
    else:
        print(f'\n❌ Query: "{q}"')
        print(f'   FAILED: Was intercepted with {len(result)} chars (should be None)')
        failed += 1
    print("-" * 60)

print("\n" + "=" * 60)
print("TEST 2: Help queries SHOULD be intercepted (return help text)")
print("=" * 60)

# These are HELP queries - should be handled
help_queries = [
    'what can you do',
    'show me your capabilities',
    'list skills',
    'what skills do you have',
    '/help',
    'help menu',
]

for q in help_queries:
    result = handle_skill_query(q)
    if result:
        print(f'\n✅ Query: "{q}"')
        print(f'   Result length: {len(result)} chars (correctly intercepted)')
        passed += 1
    else:
        print(f'\n❌ Query: "{q}" -> No match (should be intercepted)')
        failed += 1
    print("-" * 60)

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"Passed: {passed}, Failed: {failed}")
print(f"Success rate: {passed/(passed+failed)*100:.1f}%")
if failed == 0:
    print("[OK] All tests passed!")
else:
    print(f"[WARN] {failed} test(s) failed")
