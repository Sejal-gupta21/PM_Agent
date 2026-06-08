"""Historical data fetcher and simple trend analysis.

This module is intentionally lightweight and uses the existing
`scripts/generate_iteration_report.generate_report` function if available.
It provides helpers to fetch past iteration reports and compute simple
workload trends and domain familiarity (by area path and owners).
"""
from __future__ import annotations

import os
from typing import List, Dict, Any, Tuple
from pathlib import Path
import logging

logger = logging.getLogger("pm_agent.historical")


def _call_generate_report_for_iteration(iteration: str, outputs_dir: str = "outputs", **kwargs) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Call the project's report generator for a given iteration path.

    Returns (csv_path, filtered_csv_path, rows) where rows is list of dicts.
    """
    try:
        # import lazily to avoid circular/time heavy imports
        from scripts.generate_iteration_report import generate_report
        from config import config as app_config
    except Exception as e:
        logger.exception("generate_report import failed: %s", e)
        raise

    out_file, filtered_file, rows, filtered_rows, html_file = generate_report(
        org_url=kwargs.get("org_url") or app_config.ado_org_url,
        pat=kwargs.get("pat") or kwargs.get("ADO_PAT") or app_config.ado_pat,
        project=kwargs.get("project") or app_config.ado_project,
        team=kwargs.get("team") or app_config.ado_team,
        iteration=iteration,
        areas=kwargs.get("areas"),
        wi_types=kwargs.get("wi_types"),
        wiql_text=kwargs.get("wiql_text"),
        wiql_file=kwargs.get("wiql_file"),
        outputs_dir=outputs_dir,
        areas_filter=kwargs.get("areas_filter"),
        types_filter=kwargs.get("types_filter"),
    )

    return out_file, filtered_file, rows


def fetch_past_iterations(iterations: List[str], **kwargs) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch reports for a list of iteration path strings.

    Returns mapping iteration -> rows (list of dicts).
    """
    results = {}
    for it in iterations:
        try:
            csv_path, filtered_csv, rows = _call_generate_report_for_iteration(it, **kwargs)
            results[it] = rows
        except Exception:
            logger.exception("Failed to fetch iteration %s", it)
            results[it] = []
    return results


def compute_trends(iteration_rows_map: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Compute simple trends: counts per iteration, owner workload and area familiarity.

    Returns a dict with keys: iterations, counts_by_iteration, owner_trends, area_trends
    """
    from collections import Counter, defaultdict

    counts = {}
    owner_trends = defaultdict(lambda: Counter())
    area_trends = defaultdict(lambda: Counter())

    for it, rows in iteration_rows_map.items():
        counts[it] = len(rows)
        for r in rows:
            owner = r.get("Assigned To") or r.get("AssignedTo") or "Unassigned"
            area = r.get("Area Path") or ""
            owner_trends[owner][it] += 1
            area_trends[area][it] += 1

    # Summarize familiarity: for each area, total occurrences across iterations
    area_summary = {a: sum(c.values()) for a, c in area_trends.items()}

    return {
        "iterations": list(iteration_rows_map.keys()),
        "counts_by_iteration": counts,
        "owner_trends": {k: dict(v) for k, v in owner_trends.items()},
        "area_summary": area_summary,
    }

