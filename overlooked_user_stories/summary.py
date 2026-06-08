"""
Summary Module - Generate Categorized Summaries for Hierarchical Reports

This module provides functions to generate and format hierarchical summaries
from Epic → Feature → User Story structures.

Architectural Rule: This module ONLY contains summary-generation business logic.
It does NOT import from utilities/emailer.py or app/chat_ai.py (common files).
"""
from __future__ import annotations
import html
from typing import Dict, List, Any


def generate_categorized_summary(hierarchy: Dict[str, Dict[str, List[Dict[str, str]]]]) -> Dict[str, Any]:
    """
    Generate a categorized summary from hierarchical work item structure.
    
    Args:
        hierarchy: Nested dict {epic_title: {feature_title: [rows]}}
    
    Returns:
        Dictionary containing summary statistics:
        - total_stories: Total number of user stories
        - epic_count: Number of unique epics
        - feature_count: Number of unique features
        - epics: List of epic summaries with feature counts
    """
    summary = {
        "total_stories": 0,
        "epic_count": len(hierarchy),
        "feature_count": 0,
        "epics": []
    }
    
    for epic_title, features in sorted(hierarchy.items()):
        epic_summary = {
            "epic_title": epic_title,
            "story_count": 0,
            "features": []
        }
        
        for feature_title, stories in sorted(features.items()):
            story_count = len(stories)
            epic_summary["story_count"] += story_count
            summary["total_stories"] += story_count
            summary["feature_count"] += 1
            
            epic_summary["features"].append({
                "feature_title": feature_title,
                "story_count": story_count
            })
        
        summary["epics"].append(epic_summary)
    
    return summary


def format_summary_text(summary: Dict[str, Any], project: str) -> str:
    """
    Format summary as plain text for console output.
    
    Args:
        summary: Summary dictionary from generate_categorized_summary()
        project: Azure DevOps project name
    
    Returns:
        Formatted plain text summary
    """
    lines = []
    lines.append("=" * 80)
    lines.append(f"OVERLOOKED USER STORIES SUMMARY — {project}")
    lines.append("=" * 80)
    lines.append(f"Total Stories: {summary['total_stories']}")
    lines.append(f"Epics: {summary['epic_count']}")
    lines.append(f"Features: {summary['feature_count']}")
    lines.append("")
    
    for epic in summary["epics"]:
        # Show epic title and story count (0 stories if no direct stories, only through features)
        epic_direct_stories = epic.get('story_count', 0)
        lines.append(f"Epic: {epic['epic_title']} ({epic_direct_stories} stories)")
        for feature in epic["features"]:
            lines.append(f"   Feature: {feature['feature_title']} → {feature['story_count']} User Stories")
        lines.append("")
    
    lines.append("=" * 80)
    return "\n".join(lines)


def format_summary_html(summary: Dict[str, Any], project: str) -> str:
    """
    Format summary as HTML for email body.
    
    Args:
        summary: Summary dictionary from generate_categorized_summary()
        project: Azure DevOps project name
    
    Returns:
        Formatted HTML summary (styled, hierarchical)
    """
    # Build summary styled similarly to the provided screenshot: header area
    parts: List[str] = []
    parts.append('<div style="font-family: Arial, sans-serif; margin: 10px 0;">')
    parts.append(f'<h3 style="color: #0078D4; margin-top:0;">Overlooked User Stories Summary</h3>')
    parts.append(f'<p style="font-weight:600; margin:8px 0;">Total User Stories: {summary["total_stories"]}</p>')

    # Epic/feature list - styled like the screenshot
    parts.append('<div style="margin-top:12px;">')
    for epic in summary["epics"]:
        # Epic line with story count (0 stories means stories are in features only)
        epic_direct_stories = epic.get('story_count', 0)
        parts.append(f'<p style="margin:8px 0; font-weight:600;">Epic: {html.escape(epic["epic_title"])} <span style="color:#666; font-weight:normal;">({epic_direct_stories} stories)</span></p>')
        # Features as indented list
        if epic["features"]:
            parts.append('<ul style="margin:6px 0 12px 20px; padding-left:0; list-style:none;">')
            for feature in epic["features"]:
                parts.append(f'<li style="margin:4px 0;"><strong>Feature:</strong> {html.escape(feature["feature_title"])} → {feature["story_count"]} User Stories</li>')
            parts.append('</ul>')
    parts.append('</div>')

    parts.append('</div>')
    return "\n".join(parts)


def format_summary_for_ui(summary: Dict[str, Any], project: str) -> str:
    """
    Format summary for Streamlit UI display (Markdown-friendly).
    
    Args:
        summary: Summary dictionary from generate_categorized_summary()
        project: Azure DevOps project name
    
    Returns:
        Formatted text summary for UI (with markdown bullets)
    """
    lines = []
    lines.append(f"**Overlooked User Stories Summary — {project}**\n")
    lines.append(f"- **Total Stories:** {summary['total_stories']}")
    lines.append(f"- **Epics:** {summary['epic_count']}")
    lines.append(f"- **Features:** {summary['feature_count']}\n")
    
    for epic in summary["epics"]:
        epic_direct_stories = epic.get('story_count', 0)
        lines.append(f"**Epic:** {epic['epic_title']} ({epic_direct_stories} stories)")
        for feature in epic["features"]:
            lines.append(f"   **Feature:** {feature['feature_title']} → {feature['story_count']} User Stories")
        lines.append("")
    
    return "\n".join(lines)


def generate_hierarchical_html(
    hierarchy: Dict[str, Dict[str, List[Dict[str, str]]]],
    title: str,
    summary_html: str
) -> str:
    """
    Generate full hierarchical HTML report with summary and detailed table.
    
    Args:
        hierarchy: Nested dict {epic_title: {feature_title: [rows]}}
        title: Report title
        summary_html: Pre-formatted summary HTML
    
    Returns:
        Complete HTML document with hierarchical structure
    """
    html_parts = []
    html_parts.append('<!doctype html><html><head><meta charset="utf-8"/>')
    html_parts.append(f'<title>{html.escape(title)}</title>')
    html_parts.append('<style>')
    html_parts.append('body { font-family: Arial, sans-serif; margin: 20px; }')
    html_parts.append('table { border-collapse: collapse; width: 100%; margin-top: 10px; }')
    html_parts.append('th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }')
    html_parts.append('th { background: #f2f2f2; font-weight: bold; }')
    html_parts.append('.wi-table { font-size: 13px; }')
    html_parts.append('.wi-table th { background:#f8f9fb; }')
    html_parts.append('.row-epic td { background: #E9F5FF; font-weight:600; border-top:2px solid #cfe8ff; }')
    html_parts.append('.row-feature td { background: #FBFBFB; border-top:1px solid #eee; }')
    html_parts.append('.row-story td { background: #fff; }')
    html_parts.append('.wi-type { color:#444; font-weight:600; }')
    html_parts.append('.wi-title .indent-feature { padding-left:12px; font-weight:600; }')
    html_parts.append('.wi-title .indent-story { padding-left:28px; }')
    html_parts.append('.muted { color:#666; font-size:12px; margin-left:8px; }')
    html_parts.append('h2 { color: #0078D4; }')
    html_parts.append('h3 { color: #0078D4; margin-top: 20px; }')
    html_parts.append('h4 { color: #333; margin-top: 15px; }')
    html_parts.append('</style>')
    html_parts.append('</head><body>')
    html_parts.append(f'<h2>{html.escape(title)}</h2>')
    
    # Add summary section
    html_parts.append(summary_html)
    html_parts.append('<hr style="margin: 30px 0;"/>')
    html_parts.append('<h3>Detailed Work Items by Epic and Feature</h3>')
    
    # Add hierarchical sections as a table-style backlog view (Epic -> Feature -> User Story)
    html_parts.append('<table class="wi-table">')
    html_parts.append('<thead>')
    html_parts.append('<tr>')
    html_parts.append('<th style="width:12%;">Work Item Type</th>')
    html_parts.append('<th>Title</th>')
    html_parts.append('<th style="width:12%;">State</th>')
    html_parts.append('<th style="width:10%;">Effort</th>')
    html_parts.append('</tr>')
    html_parts.append('</thead>')
    html_parts.append('<tbody>')

    for epic_title, features in sorted(hierarchy.items()):
        # Epic row with actual epic title
        html_parts.append('<tr class="row-epic">')
        html_parts.append(f'<td class="wi-type">Epic</td>')
        html_parts.append(f'<td class="wi-title"><strong>👑 {html.escape(epic_title)}</strong></td>')
        html_parts.append('<td class="wi-state"></td>')
        html_parts.append('<td class="wi-effort"></td>')
        html_parts.append('</tr>')

        for feature_title, stories in sorted(features.items()):
            # Feature row (indented) with actual feature title
            html_parts.append('<tr class="row-feature">')
            html_parts.append(f'<td class="wi-type">Feature</td>')
            html_parts.append(f'<td class="wi-title"><span class="indent-feature">🏆 {html.escape(feature_title)}</span> <span class="muted">({len(stories)} stories)</span></td>')
            html_parts.append('<td class="wi-state"></td>')
            html_parts.append('<td class="wi-effort"></td>')
            html_parts.append('</tr>')

            # Story rows (further indented)
            for story in stories:
                link = html.escape(story.get("Link", ""))
                id_text = html.escape(story.get("ID", ""))
                title_text = html.escape(story.get("Title", ""))
                state_text = html.escape(story.get("State", ""))
                effort_text = html.escape(str(story.get("Effort", ""))) if story.get("Effort") is not None else ""
                html_parts.append('<tr class="row-story">')
                html_parts.append(f'<td class="wi-type">📘 User Story</td>')
                # Title with link and small meta
                story_title_html = f'<a href="{link}">{id_text}</a> — {title_text}' if link else f'{id_text} — {title_text}'
                meta_pieces = []
                if story.get("DaysStale"):
                    meta_pieces.append(f"{html.escape(str(story.get('DaysStale')))}d stale")
                if story.get("AssignedTo"):
                    meta_pieces.append(html.escape(story.get("AssignedTo")))
                meta_html = f' <span class="muted">({", ".join(meta_pieces)})</span>' if meta_pieces else ""
                html_parts.append(f'<td class="wi-title"><span class="indent-story">{story_title_html}</span>{meta_html}</td>')
                html_parts.append(f'<td class="wi-state">{state_text}</td>')
                html_parts.append(f'<td class="wi-effort">{effort_text}</td>')
                html_parts.append('</tr>')

    html_parts.append('</tbody>')
    html_parts.append('</table>')
    
    html_parts.append('</body></html>')
    return "".join(html_parts)
