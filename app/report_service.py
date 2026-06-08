# -*- coding: utf-8 -*-
"""
Lightweight wrapper around `scripts.generate_iteration_report.generate_report`.

This module keeps imports lazy so the Streamlit UI can import the module
without pulling heavy build-time dependencies at startup.
"""
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("pm_agent.report_service")


def generate_report_from_ui(
    org_url: str,
    pat: str,
    project: str,
    team: Optional[str],
    iteration: Optional[str],
    areas: list,
    wi_types: list,
    wiql_text: Optional[str],
    outputs_dir: str = "outputs",
    areas_filter: Optional[list] = None,
    types_filter: Optional[list] = None,
) -> Tuple[str, str, list, list, str]:
    """Call into the repo's report generator and return its outputs.

    Returns: (out_file, filtered_file, rows, filtered_rows, html_file)
    Raises RuntimeError when the underlying generator cannot be imported.
    """
    try:
        from scripts.generate_iteration_report import generate_report
    except Exception as e:
        logger.exception("Could not import generate_report: %s", e)
        raise RuntimeError(f"Report generator not available: {e}")

    # Delegate directly to the existing generator; keep the same signature.
    return generate_report(
        org_url=org_url,
        pat=pat,
        project=project or "FracPro-OPS",
        team=team or None,
        iteration=iteration if not wiql_text else None,
        areas=areas,
        wi_types=wi_types,
        wiql_text=(wiql_text or None),
        outputs_dir=outputs_dir,
        areas_filter=areas_filter or areas,
        types_filter=types_filter or wi_types,
    )
