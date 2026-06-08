# -*- coding: utf-8 -*-
"""
Iteration Report UI Module.

Extracted from chat_ai.py to keep the main chat file thin.
Provides UI components for ADO iteration report generation.
"""
import os
from pathlib import Path
from typing import Tuple, Optional, List, Any

import pandas as pd

from config import config
from utilities.emailer import send_report_attachment
from utilities.mcp.pat import get_pat_with_source


def render_iteration_report_section(st) -> None:
    """
    Render the Iteration Report UI section in Streamlit.
    
    This includes:
    - Input fields for ADO org URL, PAT, project, team, iteration
    - WIQL query editor
    - Generate report button
    - Download buttons for CSV and HTML reports
    - Email report button
    
    Args:
        st: The Streamlit module instance
    """
    st.header("Iteration Report")
    with st.expander("Generate ADO iteration report", expanded=False):
        org_url = st.text_input(
            "ADO Org URL", 
            value=config.ado_org_url, 
            key="ado_org_url"
        )
        
        resolved_pat, pat_source = get_pat_with_source()
        pat = st.text_input(
            "ADO PAT", 
            value=resolved_pat or "", 
            type="password", 
            key="ado_pat"
        )
        if pat_source:
            st.caption(f"Defaulted from {pat_source} environment variable.")
        
        project = st.text_input(
            "Project", 
            value=config.ado_project, 
            key="ado_project"
        )
        team = st.text_input(
            "Team (optional, required for @CurrentIteration)", 
            value=config.ado_team, 
            key="ado_team"
        )
        
        iteration_default = config.ado_iteration
        iteration = st.text_input(
            "Iteration Path (ignored if WIQL provided)", 
            value=iteration_default, 
            key="ado_iteration"
        )
        
        areas_text = st.text_input(
            "Areas (comma-separated)", 
            value=", ".join(config.query_areas) if config.query_areas else "", 
            key="ado_areas"
        )
        wi_types_text = st.text_input(
            "Work Item Types", 
            value=", ".join(config.query_wi_types) if config.query_wi_types else "User Story, Bug", 
            key="ado_wi_types"
        )
        
        # Build WIQL default
        generated_wiql = _get_default_wiql()
        
        st.subheader('WIQL (edit as needed)')
        wiql_override = st.text_area(
            "WIQL (edit/override)", 
            value=(generated_wiql or ""), 
            height=200, 
            key="ado_wiql_override"
        )
        
        recipient = st.text_input(
            "Email recipient", 
            value=config.default_pm_email, 
            key="ado_email_to"
        )

        # Generate report button
        if st.button(
            "Generate report", 
            type="primary", 
            use_container_width=True, 
            key="generate_report_btn"
        ):
            _handle_generate_report(
                st, org_url, pat, project, team, iteration,
                areas_text, wi_types_text, wiql_override
            )

        # Download buttons
        _render_download_buttons(st)
        
        # Email button
        _render_email_button(st, recipient)


def _get_default_wiql() -> str:
    """Get the default WIQL query from environment or file."""
    env_wiql = config.query_wiql_text
    wiql_file_env = config.query_wiql_file
    
    try:
        from scripts.generate_iteration_report import DEFAULT_WIQL
    except Exception:
        DEFAULT_WIQL = ""
    
    generated_wiql = DEFAULT_WIQL
    
    if env_wiql:
        generated_wiql = env_wiql
    elif wiql_file_env and os.path.exists(wiql_file_env):
        try:
            with open(wiql_file_env, 'r', encoding='utf-8') as _f:
                generated_wiql = _f.read()
        except Exception:
            pass
    
    return generated_wiql


def _handle_generate_report(
    st, 
    org_url: str, 
    pat: str, 
    project: str, 
    team: str,
    iteration: str, 
    areas_text: str, 
    wi_types_text: str, 
    wiql_override: str
) -> None:
    """Handle the Generate Report button click."""
    if not org_url or not pat:
        st.error("ADO_ORG_URL and ADO_PAT are required.")
        return

    try:
        try:
            from scripts.generate_iteration_report import generate_report
        except Exception as _e:
            raise RuntimeError(f"Report generator not available: {_e}")

        areas = [a.strip() for a in areas_text.split(",") if a.strip()]
        wi_types = [t.strip() for t in wi_types_text.split(",") if t.strip()]
        
        out_file, filtered_file, rows, filtered_rows, html_file = generate_report(
            org_url=org_url,
            pat=pat,
            project=project or "FracPro-OPS",
            team=team or None,
            iteration=iteration if not wiql_override.strip() else None,
            areas=areas,
            wi_types=wi_types,
            wiql_text=(wiql_override.strip() or None),
            outputs_dir="outputs",
            areas_filter=areas,
            types_filter=wi_types,
        )
        
        st.session_state.last_report_file = out_file
        st.session_state.last_filtered_file = filtered_file
        st.session_state.last_html_file = html_file
        
        st.success(f"Report generated: {out_file}")
        st.write(f"Rows: {len(rows)} (filtered: {len(filtered_rows)})")

        # Show preview with color-coding
        _render_report_preview(st, rows)
    except Exception as e:
        st.error(f"Failed to generate report: {e}")


def _render_report_preview(st, rows: List[Any]) -> None:
    """Render a preview of the report with color-coded fields."""
    try:
        df = pd.DataFrame(rows)
        if not df.empty:
            def highlight_row(r):
                styles = []
                for col in df.columns:
                    if col == 'UAT Scheduled Deployment Date':
                        s = r.get('UAT Status', '')
                        if s == 'yellow':
                            styles.append('background-color: #fff3cd')
                        elif s == 'red':
                            styles.append('background-color: #f8d7da')
                        else:
                            styles.append('')
                    elif col == 'PROD Scheduled Deployment':
                        s = r.get('PROD Status', '')
                        if s == 'yellow':
                            styles.append('background-color: #fff3cd')
                        elif s == 'red':
                            styles.append('background-color: #f8d7da')
                        else:
                            styles.append('')
                    else:
                        styles.append('')
                return styles

            styled = df.head(200).style.apply(highlight_row, axis=1)
            st.write('Preview (first 200 rows):')
            st.write(styled)
    except Exception as e:
        st.write(f"Could not render preview: {e}")


def _render_download_buttons(st) -> None:
    """Render download buttons for generated reports."""
    report_file = st.session_state.get("last_report_file")
    filtered_file = st.session_state.get("last_filtered_file")
    
    if report_file and os.path.exists(report_file):
        with open(report_file, "rb") as f:
            st.download_button(
                "Download full CSV", 
                f, 
                file_name=os.path.basename(report_file), 
                mime="text/csv", 
                key="dl_full_csv"
            )
    
    if filtered_file and os.path.exists(filtered_file):
        with open(filtered_file, "rb") as f:
            st.download_button(
                "Download filtered CSV", 
                f, 
                file_name=os.path.basename(filtered_file), 
                mime="text/csv", 
                key="dl_filtered_csv"
            )


def _render_email_button(st, recipient: str) -> None:
    """Render the email report button and handle click."""
    report_file = st.session_state.get("last_report_file")
    filtered_file = st.session_state.get("last_filtered_file")
    html_file = st.session_state.get("last_html_file")
    
    send_file = filtered_file or report_file
    
    attach_html = False
    if html_file and os.path.exists(html_file):
        attach_html = st.checkbox("Attach HTML report", value=True, key="attach_html")

    if send_file and os.path.exists(send_file):
        if st.button("Email report (SMTP)", use_container_width=True, key="email_report_btn"):
            if not recipient:
                st.error("Provide recipient email(s). Use comma to separate multiple addresses.")
            else:
                try:
                    recipients = [r.strip() for r in recipient.split(",") if r.strip()]
                    attachments = [Path(send_file)]
                    if attach_html and html_file and os.path.exists(html_file):
                        attachments.append(Path(html_file))
                    ok, msg = send_report_attachment(
                        recipients,
                        "Iteration report",
                        "Iteration report attached.",
                        attachments,
                    )
                    if ok:
                        st.success("Report emailed")
                    else:
                        st.warning(f"Email sent with status: {msg}")
                except Exception as e:
                    st.error(f"Error sending email: {e}")
