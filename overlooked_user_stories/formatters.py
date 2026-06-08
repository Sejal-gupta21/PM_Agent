"""
Formatters Module - CSV and HTML Formatting with Hierarchy Support

This module provides functions to write CSV files and generate HTML with
Epic and Feature columns prepended.

Architectural Rule: This module ONLY contains formatting business logic.
It does NOT import from utilities/emailer.py or app/chat_ai.py (common files).
"""
from __future__ import annotations
import csv
import html
from pathlib import Path
from typing import List, Dict


def csv_write_with_hierarchy(rows: List[Dict[str, str]], out_path: Path):
    """
    Write enriched rows to CSV with Epic and Feature columns prepended.
    
    Args:
        rows: List of row dictionaries (must contain EpicTitle, FeatureTitle)
        out_path: Output file path
    """
    headers = [
        "EpicTitle", "FeatureTitle",
        "ID", "Title", "State", "Priority",
        "CreatedDate", "ChangedDate", "StateChangeDate", "DaysStale",
        "AssignedTo", "AssignedEmail",
        "AreaPath", "IterationPath", "Tags", "Link"
    ]
    
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])


def html_for_rows_flat(rows: List[Dict[str, str]], title: str) -> str:
    """
    Generate flat HTML table with Epic and Feature columns.
    
    Args:
        rows: List of row dictionaries
        title: HTML document title
    
    Returns:
        Complete HTML document string
    """
    rows_html = []
    for r in rows:
        link = r.get("Link", "")
        id_cell = f'<a href="{html.escape(link)}">{html.escape(r.get("ID",""))}</a>'
        rows_html.append(
            "<tr>"
            + f"<td>{html.escape(r.get('EpicTitle',''))}</td>"
            + f"<td>{html.escape(r.get('FeatureTitle',''))}</td>"
            + f"<td>{id_cell}</td>"
            + f"<td>{html.escape(r.get('Title',''))}</td>"
            + f"<td>{html.escape(r.get('State',''))}</td>"
            + f"<td>{html.escape(r.get('Priority',''))}</td>"
            + f"<td>{html.escape(r.get('CreatedDate',''))}</td>"
            + f"<td>{html.escape(r.get('ChangedDate',''))}</td>"
            + f"<td>{html.escape(r.get('StateChangeDate',''))}</td>"
            + f"<td>{html.escape(r.get('DaysStale',''))}</td>"
            + f"<td>{html.escape(r.get('AssignedTo',''))}</td>"
            + f"<td>{html.escape(r.get('AssignedEmail',''))}</td>"
            + f"<td>{html.escape(r.get('AreaPath',''))}</td>"
            + f"<td>{html.escape(r.get('IterationPath',''))}</td>"
            + f"<td>{html.escape(r.get('Tags',''))}</td>"
            + "</tr>"
        )
    
    html_doc = (
        '<!doctype html><html><head><meta charset="utf-8"/>'
        f"<title>{html.escape(title)}</title>"
        '<style>table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:6px;text-align:left;font-size:13px}th{background:#f2f2f2;font-weight:bold}</style>'
        '</head><body>'
        f"<h2>{html.escape(title)}</h2>"
        "<table><thead><tr>"
        "<th>Epic</th><th>Feature</th><th>ID</th><th>Title</th><th>State</th><th>Priority</th>"
        "<th>Created</th><th>Changed</th><th>StateChange</th><th>DaysStale</th>"
        "<th>AssignedTo</th><th>AssignedEmail</th><th>Area</th><th>Iteration</th><th>Tags</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table></body></html>"
    )
    return html_doc
