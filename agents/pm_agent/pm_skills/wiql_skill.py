"""
WIQL Skill — Direct WIQL execution against Azure DevOps REST API.

CANONICAL TOOL NAME: execute_wiql

This is the SINGLE entry point for all WIQL execution in the system.
The LLM planner generates the WIQL string and passes it here unchanged.

Architecture:
    User → LLM Planner → execute_wiql → _apis/wit/wiql → hydrate → respond

NO regex routing. NO NL→WIQL conversion. NO WIQL rebuilding.
The LLM is responsible for generating valid WIQL.

This skill provides lightweight validation (not correction):
- Checks for SELECT and FROM keywords
- Warns about known incorrect field names
- Returns transparent errors — never silently modifies the query

Usage:
    from agents.pm_agent.pm_skills.wiql_skill import execute_wiql

    result = await execute_wiql(
        project="MyProject",
        wiql="SELECT [System.Id], [System.Title] FROM WorkItems WHERE ..."
    )
"""

import logging
import requests
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# KNOWN INCORRECT ADO FIELD NAMES — Auto-corrected before execution.
# LLMs frequently generate [System.ClosedDate] instead of the correct
# [Microsoft.VSTS.Common.ClosedDate]. Rather than failing at the ADO API,
# we silently fix these common mistakes and log warnings for observability.
# ═══════════════════════════════════════════════════════════════════════════════
_KNOWN_INVALID_FIELDS = {
    "[System.ClosedDate]": "[Microsoft.VSTS.Common.ClosedDate]",
    "[System.ResolvedDate]": "[Microsoft.VSTS.Common.ResolvedDate]",
    "[System.Priority]": "[Microsoft.VSTS.Common.Priority]",
    "[System.Severity]": "[Microsoft.VSTS.Common.Severity]",
    "[Microsoft.VSTS.Common.RemainingWork]": "[Microsoft.VSTS.Scheduling.RemainingWork]",
    "[Microsoft.VSTS.Common.OriginalEstimate]": "[Microsoft.VSTS.Scheduling.OriginalEstimate]",
    "[Microsoft.VSTS.Common.CompletedWork]": "[Microsoft.VSTS.Scheduling.CompletedWork]",
}

# Fields that do NOT exist in ADO WIQL at all. These are common LLM hallucinations.
# Iteration start/end dates are properties of the ITERATION OBJECT (via REST API),
# NOT fields on work items. Queries using these will always fail with TF51005.
_NON_EXISTENT_FIELDS = [
    "[System.IterationStartDate]",
    "[System.IterationEndDate]",
    "[System.IterationFinishDate]",
    "[System.SprintStartDate]",
    "[System.SprintEndDate]",
    "[Microsoft.VSTS.Common.IterationStartDate]",
    "[Microsoft.VSTS.Common.IterationEndDate]",
]


def _autocorrect_wiql_fields(wiql: str) -> tuple:
    """
    Auto-correct known invalid ADO field names in a WIQL query.
    
    Returns:
        (corrected_wiql, corrections_made: list[str])
    """
    corrections = []
    corrected = wiql
    for bad_field, correct_field in _KNOWN_INVALID_FIELDS.items():
        if bad_field in corrected:
            corrected = corrected.replace(bad_field, correct_field)
            corrections.append(f"{bad_field} → {correct_field}")
    return corrected, corrections


def _rewrite_aggregate_wiql(wiql: str) -> tuple:
    """
    Detect and rewrite WIQL queries that use aggregate functions (COUNT, SUM, AVG,
    GROUP BY, HAVING) which are NOT supported by the ADO WIQL REST API.

    Strategy:
        - Extract the GROUP BY field(s) — these are what the user wants to categorize by
        - Strip COUNT(...), SUM(...), AVG(...), AS [alias] from SELECT
        - Strip GROUP BY and HAVING clauses entirely
        - Build a flat SELECT with standard fields + the grouping field(s)
        - The synthesis LLM will handle the actual counting/grouping from flat results

    Returns:
        (rewritten_wiql_or_original, was_rewritten: bool, warning_message: str or None)
    """
    import re as _re

    wiql_upper = wiql.upper()

    # Detect aggregate patterns
    has_count = bool(_re.search(r'\bCOUNT\s*\(', wiql_upper))
    has_sum = bool(_re.search(r'\bSUM\s*\(', wiql_upper))
    has_avg = bool(_re.search(r'\bAVG\s*\(', wiql_upper))
    has_group_by = bool(_re.search(r'\bGROUP\s+BY\b', wiql_upper))
    has_having = bool(_re.search(r'\bHAVING\b', wiql_upper))

    if not (has_count or has_sum or has_avg or has_group_by or has_having):
        return wiql, False, None

    logger.warning(f"[validate_wiql] Detected unsupported aggregate WIQL: COUNT={has_count}, SUM={has_sum}, AVG={has_avg}, GROUP_BY={has_group_by}, HAVING={has_having}")

    # ── Extract GROUP BY fields ──────────────────────────────────────────
    group_fields = []
    group_by_match = _re.search(
        r'\bGROUP\s+BY\s+(.+?)(?:\bHAVING\b|\bORDER\s+BY\b|$)',
        wiql, _re.IGNORECASE
    )
    if group_by_match:
        group_clause = group_by_match.group(1).strip()
        # Extract [Field.Name] patterns from GROUP BY
        group_fields = _re.findall(r'\[([^\]]+)\]', group_clause)

    # ── Extract WHERE clause ─────────────────────────────────────────────
    where_match = _re.search(
        r'\bWHERE\b(.+?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|$)',
        wiql, _re.IGNORECASE | _re.DOTALL
    )
    where_clause = where_match.group(1).strip() if where_match else ""

    # ── Collect aggregate aliases from SELECT (e.g., COUNT(...) AS [TotalWorkItems]) ─
    select_for_aliases = _re.search(r'\bSELECT\b(.+?)\bFROM\b', wiql, _re.IGNORECASE | _re.DOTALL)
    aggregate_aliases = set()
    if select_for_aliases:
        select_text = select_for_aliases.group(1)
        # Find all AS [AliasName] patterns following aggregate functions
        alias_matches = _re.findall(
            r'\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\)\s+AS\s+\[([^\]]+)\]',
            select_text, _re.IGNORECASE
        )
        aggregate_aliases = {a.lower() for a in alias_matches}
        if aggregate_aliases:
            logger.info(f"[validate_wiql] Found aggregate aliases to strip from ORDER BY: {aggregate_aliases}")

    # ── Extract ORDER BY clause (before GROUP BY corrupts it) ────────────
    order_match = _re.search(
        r'\bORDER\s+BY\b(.+?)$',
        wiql, _re.IGNORECASE
    )
    order_clause = ""
    if order_match:
        raw_order = order_match.group(1).strip()
        # Strip any aggregate function references from ORDER BY
        raw_order = _re.sub(r'\bCOUNT\s*\([^)]*\)', '[System.Id]', raw_order, flags=_re.IGNORECASE)
        raw_order = _re.sub(r'\bSUM\s*\([^)]*\)', '[System.Id]', raw_order, flags=_re.IGNORECASE)
        raw_order = _re.sub(r'\bAVG\s*\([^)]*\)', '[System.Id]', raw_order, flags=_re.IGNORECASE)
        # Strip any aggregate alias references (e.g., [TotalWorkItems], [BugCount])
        # These are aliases from COUNT(...) AS [Alias] that no longer exist after rewriting
        if aggregate_aliases:
            def _replace_alias(m):
                field_name = m.group(1)
                if field_name.lower() in aggregate_aliases:
                    return '[System.Id]'
                return m.group(0)
            raw_order = _re.sub(r'\[([^\]]+)\]', _replace_alias, raw_order)
        # If ORDER BY now only references [System.Id] (after alias replacement),
        # keep it but it's effectively a no-op sort — still valid WIQL
        order_clause = f" ORDER BY {raw_order}"

    # ── Also try to extract fields mentioned in SELECT aggregates ────────
    # e.g., SELECT [System.AreaPath], COUNT([System.Id]) → extract System.AreaPath
    select_match = _re.search(r'\bSELECT\b(.+?)\bFROM\b', wiql, _re.IGNORECASE | _re.DOTALL)
    select_fields_from_original = []
    if select_match:
        select_part = select_match.group(1)
        # Get non-aggregate field references
        # First remove aggregate expressions like COUNT([...]) AS [...]
        cleaned_select = _re.sub(r'\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\)(?:\s+AS\s+\[[^\]]*\])?', '', select_part, flags=_re.IGNORECASE)
        select_fields_from_original = _re.findall(r'\[([^\]]+)\]', cleaned_select)

    # ── Build the rewritten flat SELECT ──────────────────────────────────
    # Standard fields that are always useful for synthesis
    standard_fields = [
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AssignedTo",
        "System.AreaPath",
    ]

    # Combine: standard + group-by fields + any non-aggregate SELECT fields
    all_fields = list(standard_fields)
    for f in group_fields + select_fields_from_original:
        if f not in all_fields:
            all_fields.append(f)

    select_str = ", ".join(f"[{f}]" for f in all_fields)

    # ── Reconstruct the query ────────────────────────────────────────────
    # Extract project reference from WHERE clause if present
    if where_clause:
        rewritten = f"SELECT {select_str} FROM WorkItems WHERE {where_clause}{order_clause}"
    else:
        rewritten = f"SELECT {select_str} FROM WorkItems{order_clause}"

    # Clean up any double spaces
    rewritten = _re.sub(r'\s+', ' ', rewritten).strip()

    aggregates_found = []
    if has_count: aggregates_found.append("COUNT")
    if has_sum: aggregates_found.append("SUM")
    if has_avg: aggregates_found.append("AVG")
    if has_group_by: aggregates_found.append("GROUP BY")
    if has_having: aggregates_found.append("HAVING")

    warning = (
        f"Auto-rewrote aggregate WIQL: removed {', '.join(aggregates_found)} "
        f"(not supported by ADO WIQL REST API). Flat results will be returned "
        f"for synthesis-layer grouping/counting."
    )

    logger.info(f"[validate_wiql] Rewrote aggregate WIQL:\n  BEFORE: {wiql[:200]}...\n  AFTER:  {rewritten[:200]}...")

    return rewritten, True, warning


def validate_wiql(wiql: str) -> Dict[str, Any]:
    """
    Lightweight WIQL validation. Auto-corrects known invalid fields.

    Returns:
        {
            "valid": bool,
            "error": str or None,        # hard error — query cannot execute
            "warnings": list[str],        # soft warnings — corrections made
            "corrected_wiql": str or None, # corrected WIQL (if any corrections)
        }
    """
    if not wiql or not isinstance(wiql, str):
        return {"valid": False, "error": "WIQL query is empty or not a string", "warnings": [], "corrected_wiql": None}

    # ── AGGREGATE REWRITING (must happen BEFORE structure checks) ─────────
    # ADO WIQL REST API does NOT support COUNT, SUM, AVG, GROUP BY, HAVING.
    # LLMs frequently generate these. We rewrite to flat SELECT and let
    # the synthesis layer handle grouping/counting from raw results.
    wiql, was_rewritten, aggregate_warning = _rewrite_aggregate_wiql(wiql)
    aggregate_warnings = [aggregate_warning] if aggregate_warning else []

    wiql_upper = wiql.upper().strip()

    if "SELECT" not in wiql_upper:
        return {"valid": False, "error": "WIQL missing SELECT clause", "warnings": aggregate_warnings, "corrected_wiql": None}

    if "FROM" not in wiql_upper:
        return {"valid": False, "error": "WIQL missing FROM clause", "warnings": aggregate_warnings, "corrected_wiql": None}

    # ── SUBQUERY REWRITING ───────────────────────────────────────────────
    # ADO WIQL does NOT support nested SELECT (subqueries) or step-variable references.
    # LLMs often generate:
    #   1. WHERE [field] IN (SELECT ... FROM WorkItems WHERE ...)
    #   2. WHERE [field] IN (SELECT ... FROM step_variable_name)
    # We detect both patterns and either flatten or strip the invalid clause.
    import re as _re_sub

    # Pattern 1: Standard subquery with FROM WorkItems WHERE ...
    _subquery_pat = _re_sub.compile(
        r'\[([^\]]+)\]\s+IN\s*\(\s*SELECT\s+\[([^\]]+)\]\s+FROM\s+WorkItems\s+WHERE\s+(.+?)\)',
        _re_sub.IGNORECASE | _re_sub.DOTALL,
    )
    # Pattern 2: Step variable reference (FROM step_name or FROM non_WorkItems)
    _step_ref_pat = _re_sub.compile(
        r'\s+AND\s+\[([^\]]+)\]\s+IN\s*\(\s*SELECT\s+[^)]+FROM\s+(?!WorkItems\b)\w+[^)]*\)',
        _re_sub.IGNORECASE | _re_sub.DOTALL,
    )
    # Pattern 3: Any remaining IN (SELECT ...) that wasn't caught
    _any_subquery_pat = _re_sub.compile(
        r'\s+AND\s+\[([^\]]+)\]\s+IN\s*\(\s*SELECT\s+[^)]+\)',
        _re_sub.IGNORECASE | _re_sub.DOTALL,
    )
    # Pattern 4: IN (variable_name) without SELECT — step variable reference like IN (current_sprint_items)
    _var_ref_pat = _re_sub.compile(
        r'\s+AND\s+\[([^\]]+)\]\s+IN\s*\(\s*(?!SELECT\b)[a-zA-Z_]\w*\s*\)',
        _re_sub.IGNORECASE,
    )

    # Check for variable references first (IN (variable_name) without SELECT)
    if _var_ref_pat.search(wiql):
        wiql = _var_ref_pat.sub('', wiql)
        wiql = _re_sub.sub(r'\s+', ' ', wiql).strip()
        aggregate_warnings.append(
            f"Removed invalid IN (variable) clause: ADO WIQL does not support "
            f"step variable references in IN clauses."
        )
        logger.warning(f"[validate_wiql] Removed variable reference IN clause: {wiql[:200]}...")
        wiql_upper = wiql.upper().strip()

    if wiql_upper.count("SELECT") > 1:
        if _subquery_pat.search(wiql):
            # Flatten: merge subquery WHERE into outer WHERE with AND
            match = _subquery_pat.search(wiql)
            outer_field = match.group(1)
            inner_field = match.group(2)
            inner_where = match.group(3).strip()

            # Get outer WHERE clause (everything before the IN subquery)
            outer_before = wiql[:match.start()].strip()
            outer_after = wiql[match.end():].strip()

            # Extract existing outer WHERE conditions
            outer_where_match = _re_sub.search(r'\bWHERE\b(.+?)$', outer_before, _re_sub.IGNORECASE | _re_sub.DOTALL)
            if outer_where_match:
                outer_conditions = outer_where_match.group(1).strip()
                # Remove trailing AND/OR
                outer_conditions = _re_sub.sub(r'\s+(AND|OR)\s*$', '', outer_conditions, flags=_re_sub.IGNORECASE).strip()
                # Merge: combine outer conditions with inner WHERE
                merged_where = f"{outer_conditions} AND {inner_where}"
            else:
                merged_where = inner_where

            # Extract SELECT fields from outer query
            select_match = _re_sub.search(r'\bSELECT\b(.+?)\bFROM\b', outer_before, _re_sub.IGNORECASE | _re_sub.DOTALL)
            if select_match:
                select_fields = select_match.group(1).strip()
            else:
                select_fields = "[System.Id], [System.Title], [System.State], [System.WorkItemType], [System.AssignedTo]"

            # Reconstruct flat query
            wiql = f"SELECT {select_fields} FROM WorkItems WHERE {merged_where} {outer_after}".strip()
            wiql = _re_sub.sub(r'\s+', ' ', wiql)  # Clean up whitespace
            aggregate_warnings.append(
                f"Auto-rewrote subquery WIQL: ADO does not support nested SELECT. "
                f"Flattened by merging WHERE clauses."
            )
            logger.warning(f"[validate_wiql] Flattened subquery WIQL: {wiql[:200]}...")

        elif _step_ref_pat.search(wiql) or _any_subquery_pat.search(wiql):
            # Strip the IN (SELECT ...) clause entirely — it references a step variable
            # or non-standard table that can't be resolved
            pat = _step_ref_pat if _step_ref_pat.search(wiql) else _any_subquery_pat
            wiql = pat.sub('', wiql)
            wiql = _re_sub.sub(r'\s+', ' ', wiql).strip()
            aggregate_warnings.append(
                f"Removed invalid IN (SELECT ...) clause: ADO WIQL does not support "
                f"subqueries or step variable references."
            )
            logger.warning(f"[validate_wiql] Removed invalid subquery clause: {wiql[:200]}...")

        wiql_upper = wiql.upper().strip()

    # Check for non-existent fields (common LLM hallucinations)
    for bad_field in _NON_EXISTENT_FIELDS:
        if bad_field.lower() in wiql.lower():
            return {
                "valid": False,
                "error": f"WIQL contains non-existent field {bad_field}. "
                         f"Iteration start/end dates are NOT work item fields in ADO. "
                         f"Use 'work_list_team_iterations' tool to get iteration dates instead.",
                "warnings": [],
                "corrected_wiql": None
            }

    # Strip @CurrentIteration / @PreviousIteration from flat WIQL — these macros
    # require a team context that the REST WIQL endpoint doesn't have.
    # The LLM sometimes injects them even when the user didn't ask for a sprint filter.
    import re as _re
    _iter_macro_pat = _re.compile(
        r"\s*AND\s+\[System\.IterationPath\]\s*(=|UNDER)\s*'?@(Current|Previous)Iteration'?",
        _re.IGNORECASE,
    )
    if _iter_macro_pat.search(wiql):
        wiql = _iter_macro_pat.sub("", wiql)
        logger.warning("[validate_wiql] Removed @CurrentIteration/@PreviousIteration clause — not supported in flat WIQL REST calls")

    # Check for invalid macros: @Yesterday, @Tomorrow
    _invalid_macro_pat = _re.compile(r"@(Yesterday|Tomorrow)\b", _re.IGNORECASE)
    _bad_macro = _invalid_macro_pat.search(wiql)
    if _bad_macro:
        macro_name = _bad_macro.group(0)
        replacement = "@Today - 1" if "yesterday" in macro_name.lower() else "@Today + 1"
        wiql = _invalid_macro_pat.sub(replacement, wiql)
        logger.warning(f"[validate_wiql] Replaced invalid macro {macro_name} with {replacement}")

    # Strip SELECT TOP N — ADO WIQL doesn't support TOP in SELECT clause.
    # The top parameter should be passed via the API's $top query param instead.
    _top_pat = _re.compile(r'\bSELECT\s+TOP\s+\d+\s+', _re.IGNORECASE)
    if _top_pat.search(wiql):
        wiql = _top_pat.sub('SELECT ', wiql)
        logger.warning("[validate_wiql] Removed 'TOP N' from SELECT clause — use API $top parameter instead")

    # Auto-correct known invalid fields (wiql may already be modified by macro fixes above)
    corrected_wiql, corrections = _autocorrect_wiql_fields(wiql)
    warnings = list(aggregate_warnings)  # Start with aggregate warnings if any
    for correction in corrections:
        warnings.append(f"Auto-corrected field: {correction}")

    # Always return corrected_wiql so macro fixes are picked up even without field corrections
    return {"valid": True, "error": None, "warnings": warnings, "corrected_wiql": corrected_wiql}


async def execute_wiql(
    project: str = None,
    wiql: str = None,
    top: int = 1000,
    **kwargs,
) -> Dict[str, Any]:
    """
    Execute a WIQL query against Azure DevOps REST API.

    This is the SINGLE canonical WIQL execution function.
    The LLM planner generates the WIQL — this function executes it as-is.

    Args:
        project: Azure DevOps project name.
        wiql:    Raw WIQL query string (generated by LLM planner).
        top:     Maximum results (default 1000).
        **kwargs: Extra keyword args (e.g. context) — accepted but unused.

    Returns:
        {
            "success": bool,
            "count":   int,
            "items":   list[dict],   # hydrated work item dicts
            "query":   str,          # the executed WIQL
            "error":   str or None,
            "warnings": list[str],   # validation warnings (if any)
        }
    """
    from config import config

    # ── resolve project ────────────────────────────────────────────────────
    project = project or config.ado_project
    org_url = config.ado_org_url

    # ── get PAT ────────────────────────────────────────────────────────────
    try:
        from utilities.mcp.pat import get_pat
        pat = get_pat()
    except Exception as e:
        logger.error(f"[execute_wiql] Failed to get PAT: {e}")
        return _error_response("Failed to get PAT token", wiql)

    if not org_url or not pat:
        return _error_response("ADO_ORG_URL or PAT not configured", wiql)

    # ── validate WIQL (lightweight — auto-corrects known field name issues) ──
    if not wiql:
        return _error_response("No WIQL query provided", wiql)

    validation = validate_wiql(wiql)
    if not validation["valid"]:
        return _error_response(f"WIQL validation failed: {validation['error']}", wiql)

    # Apply auto-corrected WIQL if any corrections were made
    if validation.get("corrected_wiql"):
        logger.info(f"[execute_wiql] Auto-corrected WIQL fields: {validation['warnings']}")
        wiql = validation["corrected_wiql"]

    if validation["warnings"]:
        for w in validation["warnings"]:
            logger.warning(f"[execute_wiql] VALIDATION WARNING: {w}")

    # ── execute WIQL against ADO REST API ─────────────────────────────────
    wiql_url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
    if top:
        wiql_url += f"&$top={top}"

    logger.info(f"[execute_wiql] project={project}, top={top}")
    logger.info(f"[execute_wiql] WIQL:\n{wiql}")

    try:
        resp = requests.post(
            wiql_url,
            auth=("", pat),
            headers={"Content-Type": "application/json"},
            json={"query": wiql},
            timeout=60,
        )
        resp.raise_for_status()
        wiql_result = resp.json()

        work_item_refs = wiql_result.get("workItems", [])
        if not work_item_refs:
            logger.info("[execute_wiql] Query returned 0 work items")
            return {
                "success": True,
                "count": 0,
                "items": [],
                "query": wiql,
                "error": None,
                "warnings": validation["warnings"],
            }

        ids = [wi["id"] for wi in work_item_refs if wi.get("id")]
        if top and len(ids) > top:
            ids = ids[:top]

        logger.info(f"[execute_wiql] WIQL returned {len(ids)} work item IDs")

        # ── hydrate work items ────────────────────────────────────────────
        items = await _fetch_work_item_details(org_url, pat, ids)

        return {
            "success": True,
            "count": len(items),
            "items": items,
            "query": wiql,
            "error": None,
            "warnings": validation["warnings"],
        }

    except requests.exceptions.HTTPError as e:
        error_msg = f"WIQL query failed: {e}"
        if hasattr(e, "response") and e.response is not None:
            try:
                detail = e.response.json()
                error_msg = f"WIQL error: {detail.get('message', str(e))}"
            except Exception:
                error_msg = f"WIQL error: {e.response.text[:500]}"
        logger.error(f"[execute_wiql] {error_msg}")
        return _error_response(error_msg, wiql, validation["warnings"])

    except Exception as e:
        logger.exception(f"[execute_wiql] Unexpected error: {e}")
        return _error_response(str(e), wiql, validation["warnings"])


# ═══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _error_response(
    error: str,
    wiql: str = None,
    warnings: List[str] = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "error": error,
        "count": 0,
        "items": [],
        "query": wiql,
        "warnings": warnings or [],
    }


async def _fetch_work_item_details(
    org_url: str,
    pat: str,
    ids: List[int],
) -> List[Dict[str, Any]]:
    """
    Fetch full work item details by IDs in batches of 200 (ADO limit).
    """
    if not ids:
        return []

    fields = [
        "System.Id",
        "System.Title",
        "System.State",
        "System.WorkItemType",
        "System.AreaPath",
        "System.IterationPath",
        "System.CreatedDate",
        "System.ChangedDate",
        "System.AssignedTo",
        "System.Tags",
        "System.Reason",
        "Microsoft.VSTS.Common.ClosedDate",
        "Microsoft.VSTS.Common.Priority",
        "Microsoft.VSTS.Common.Severity",
    ]

    work_items: List[Dict[str, Any]] = []
    batch_size = 200

    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        ids_str = ",".join(str(x) for x in batch)
        fields_str = ",".join(fields)
        url = f"{org_url}/_apis/wit/workitems?ids={ids_str}&fields={fields_str}&api-version=7.0"

        try:
            resp = requests.get(url, auth=("", pat), timeout=60)
            resp.raise_for_status()
            data = resp.json()

            for wi in data.get("value", []):
                flattened: Dict[str, Any] = {
                    "id": wi.get("id"),
                    "url": wi.get("url"),
                    "rev": wi.get("rev"),
                }
                fields_data = wi.get("fields", {})
                for field_name, field_value in fields_data.items():
                    simple_name = field_name.replace("System.", "").replace(
                        "Microsoft.VSTS.Common.", ""
                    )
                    if isinstance(field_value, dict) and "displayName" in field_value:
                        flattened[simple_name] = field_value.get("displayName")
                        flattened[f"{simple_name}_email"] = field_value.get("uniqueName")
                    else:
                        flattened[simple_name] = field_value
                flattened["fields"] = fields_data
                work_items.append(flattened)

        except Exception as e:
            logger.error(f"[execute_wiql] Failed to fetch batch: {e}")

    logger.info(f"[execute_wiql] Hydrated {len(work_items)} work items")
    return work_items


# ═══════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY ALIAS (deprecated — will be removed)
# ═══════════════════════════════════════════════════════════════════════════════
async def run_wiql_query(project=None, wiql=None, top=1000, natural_language_query=None):
    """DEPRECATED: Use execute_wiql() instead. This alias exists only for transition."""
    logger.warning("[DEPRECATED] run_wiql_query() called — use execute_wiql() instead")
    return await execute_wiql(project=project, wiql=wiql, top=top)
