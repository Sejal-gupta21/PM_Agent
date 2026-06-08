# -*- coding: utf-8 -*-
"""
Chat Extensions - Inline UI renderers for chat-triggered sections.

These functions render UI components inline within the chat interface
when triggered by user queries.
"""
import os
import sys
import asyncio
import tempfile
import csv
import random
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import streamlit as st
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utilities.logging_config import get_logger
from app import chat_service as svc

logger = get_logger(__name__)


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _generate_dummy_attendance_csv() -> str:
    """Generate a dummy attendance CSV file for capacity triaging testing.
    
    Creates realistic sample data matching the standard team capacity report format.
    Based on sample data: 6 team members, 34.0 hrs/day total, 3 total days off.
    
    Returns:
        Path to the generated temporary CSV file.
    """
    # Sample team members with capacity matching typical report format
    # Total: 6 members, 34.0 hrs/day, 3 days off
    sample_data = [
        {"name": "Ankur Kumar", "email": "ankur.kumar@example.com", "capacity": 6.0, "days_off": 1, "activity": "Development"},
        {"name": "Sejal Gupta", "email": "sejal.gupta@example.com", "capacity": 6.0, "days_off": 0, "activity": "Development"},
        {"name": "Yali Gautam", "email": "yali.gautam@example.com", "capacity": 6.0, "days_off": 0, "activity": "Development"},
        {"name": "Sarthak Singh", "email": "sarthak.singh@example.com", "capacity": 5.0, "days_off": 1, "activity": "Testing"},
        {"name": "Dhruv Singh", "email": "dhruv.singh@example.com", "capacity": 6.0, "days_off": 1, "activity": "Development"},
        {"name": "Rishabh Suri", "email": "rishabh.suri@example.com", "capacity": 5.0, "days_off": 0, "activity": "Code Review"},
    ]
    
    # Generate capacity data rows
    rows = []
    for member in sample_data:
        rows.append({
            "Name": member["name"],
            "Email": member["email"],
            "CapacityPerDay": member["capacity"],
            "DaysOff": member["days_off"],
            "Activity": member["activity"]
        })
    
    # Write to temporary CSV file
    temp_path = os.path.join(tempfile.gettempdir(), "dummy_attendance_data.csv")
    with open(temp_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=["Name", "Email", "CapacityPerDay", "DaysOff", "Activity"])
        writer.writeheader()
        writer.writerows(rows)
    
    logger.info(f"Generated dummy attendance CSV at: {temp_path}")
    return temp_path


# =============================================================================
# SECTION DETECTION FROM QUERY
# =============================================================================

# UI Section keywords - ONLY match explicit requests for UI tools/forms
# Do NOT match skill-related queries here - those should go through the controller
# for proper tracing (controller → orchestrator → agent flow)
SECTION_KEYWORDS = {
    "developer_knowledge_base": [
        # Only match explicit UI requests, not skill queries
        "show knowledge base form", "open knowledge base tool", "knowledge base panel",
        "show developer skills form", "open skill matrix tool"
    ],
    "sprint_plan": [
        # Only match explicit UI requests for the sprint planning form
        "show sprint plan form", "open sprint plan tool", "sprint plan generator form",
        "open sprint generator", "show sprint planning form"
    ],
    "backlog_assignments": [
        # Only match explicit UI requests for backlog assignment form
        "show backlog assignment form", "open backlog assignment tool",
        "backlog assignment panel", "open assignment form"
    ],
    "capacity_check": [
        # Only match explicit UI requests for capacity check form
        "show capacity check form", "open capacity check tool",
        "capacity check panel", "open capacity form"
    ],
    "capacity_triaging": [
        # Only match explicit UI requests for capacity triaging form
        "show capacity triaging form", "open capacity triaging tool",
        "capacity triaging panel", "open capacity triage form"
    ],
    "backlog_triaging": [
        # Only match explicit UI requests for backlog triaging form
        "show backlog triaging form", "open backlog triaging tool",
        "backlog triaging panel", "open backlog triage form"
    ],
}


def detect_section_from_query(query: str) -> Optional[str]:
    """Detect which UI section the user is asking about.
    
    Returns section key or None if no section detected.
    """
    query_lower = query.lower()
    
    for section_key, keywords in SECTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in query_lower:
                return section_key
    
    return None


# =============================================================================
# INLINE UI RENDERERS
# =============================================================================

def render_developer_knowledge_base_inline():
    """Render Developer Knowledge Base section inline in chat."""
    with st.container():
        st.markdown("---")
        st.markdown("### 📚 Developer Knowledge Base")
        
        if st.button("Refresh Knowledge Base", key="inline_refresh_kb_btn"):
            with st.spinner("Building knowledge base (analyzing last 30 days, ~2-3 min)..."):
                ok, msg = svc.run_knowledge_base_refresh(days=30, max_wi=50)
                st.success(msg) if ok else st.error(msg)
                if ok: 
                    st.rerun()
        
        df = svc.get_developer_skills_df()
        if df is not None:
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download CSV", 
                df.to_csv(index=False), 
                "developer_skills.csv", 
                "text/csv", 
                key="inline_dl_skills"
            )
        else:
            st.info("No developer skills data. Click 'Refresh Knowledge Base' to generate.")


def render_sprint_plan_inline():
    """Render Sprint Plan Generator section inline in chat."""
    with st.container():
        st.markdown("---")
        st.markdown("### 📅 Sprint Plan Generator")
        
        c1, c2, c3 = st.columns(3)
        sprint_name = c1.text_input(
            "Sprint Name", 
            f"Sprint {datetime.now():%Y-%m-%d}", 
            key="inline_sp_name"
        )
        sprint_start = c2.date_input("Start", datetime.now(), key="inline_sp_start")
        sprint_end = c3.date_input(
            "End", 
            datetime.now() + timedelta(days=10), 
            key="inline_sp_end"
        )
        
        if st.button("Generate Sprint Plan", key="inline_gen_sprint_btn"):
            with st.spinner("Generating sprint plan..."):
                ok, msg = svc.run_generate_sprint_plan(
                    sprint_name, 
                    sprint_start.strftime("%Y-%m-%d"), 
                    sprint_end.strftime("%Y-%m-%d")
                )
                st.success(msg) if ok else st.error(msg)
                if ok: 
                    st.rerun()
        
        csv_path = svc.load_latest_sprint_plan_csv()
        if csv_path and csv_path.exists():
            df = pd.read_csv(csv_path, encoding='utf-8')
            cols = [c for c in [
                "Sprint", "Feature / User Story", "Task Name", 
                "Start Date", "End Date", "Duration (days)", 
                "Estimated Hours", "Responsible - Frontend", 
                "Responsible - Backend", "Status"
            ] if c in df.columns]
            st.dataframe(df[cols], use_container_width=True, hide_index=True)
            st.download_button(
                "Download Sprint CSV", 
                csv_path.read_text(encoding='utf-8'), 
                csv_path.name, 
                "text/csv", 
                key="inline_dl_sprint"
            )


def render_backlog_assignments_inline():
    """Render Backlog Assignments section inline in chat."""
    with st.container():
        st.markdown("---")
        st.markdown("### 📋 Backlog Assignments")
        
        c1, c2 = st.columns(2)
        bl_sprint = c1.text_input(
            "Sprint Name", 
            f"Backlog {datetime.now():%Y-%m-%d}", 
            key="inline_bl_name"
        )
        bl_days = c2.number_input("Sprint Days", 5, 20, 10, key="inline_bl_days")
        
        d1, d2 = st.columns(2)
        bl_start = d1.date_input("Start Date", datetime.now(), key="inline_bl_start")
        bl_end = d2.date_input(
            "End Date", 
            datetime.now() + timedelta(days=bl_days), 
            key="inline_bl_end"
        )
        
        if st.button("Assign Backlog Items", key="inline_assign_bl_btn"):
            with st.spinner("Assigning backlog..."):
                ok, msg = svc.run_backlog_assignments(
                    bl_sprint, 
                    bl_start.strftime("%Y-%m-%d"), 
                    bl_end.strftime("%Y-%m-%d"), 
                    bl_days
                )
                st.success(msg) if ok else st.error(msg)
                if ok: 
                    st.rerun()
        
        csv_path = svc.load_latest_backlog_assignments_csv()
        if csv_path and csv_path.exists():
            df = pd.read_csv(csv_path, encoding='utf-8')
            summary = svc.get_backlog_summary(df)
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Work Items", summary.get("parent_items", summary["total"]))
            m2.metric("Subtasks", summary["total"])
            m3.metric("FE Assigned", summary["fe_assigned"])
            m4.metric("BE Assigned", summary["be_assigned"])
            m5.metric("Cross-Role", summary["cross_role"])
            
            # Display columns matching sprint plan format
            cols = [c for c in [
                "Sprint", "Feature / User Story", "Task Name",
                "Start Date", "End Date", "Duration (days)", 
                "Estimated Hours", "Responsible - Frontend",
                "Responsible - Backend", "Status"
            ] if c in df.columns]
            st.dataframe(df[cols], use_container_width=True, hide_index=True)
            st.download_button(
                "Download CSV", 
                df.to_csv(index=False), 
                "backlog_assignments.csv", 
                "text/csv", 
                key="inline_dl_backlog"
            )


def render_capacity_check_inline():
    """Render Capacity Check section inline in chat."""
    with st.container():
        st.markdown("---")
        st.markdown("### 📊 Capacity Check")
        
        report = svc.load_latest_capacity_report()
        if report:
            data = svc.get_capacity_summary(report)
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Developers", data["total_devs"])
            m2.metric("Assigned", f"{data['total_assigned']:.0f}h")
            m3.metric("Capacity", f"{data['total_capacity']:.0f}h")
            m4.metric("Utilization", f"{data['overall_util']:.0f}%")
            
            if data["dev_data"]:
                st.dataframe(
                    pd.DataFrame(data["dev_data"]), 
                    use_container_width=True, 
                    hide_index=True
                )
            
            if data["overloaded_developers"]:
                st.warning(f"Overloaded: {', '.join(data['overloaded_developers'])}")
        else:
            st.info("No capacity report. Generate a sprint plan first.")


def render_capacity_triaging_inline():
    """Render Capacity Triaging section inline in chat."""
    with st.container():
        st.markdown("---")
        st.markdown("### ⚠️ Capacity Triaging")
        st.markdown("**Configure capacity data source for sprint monitoring**")
        
        # Data source selection
        st.markdown("#### 📊 Capacity Data Source")
        source_type = st.radio(
            "Select attendance/capacity data source:",
            options=["Azure DevOps", "CSV File", "Google Sheets"],
            index=0,
            key="inline_triaging_source_type",
            horizontal=True
        )
        
        # CSV/Excel file upload option
        uploaded_file = None
        use_dummy_data = False
        if source_type == "CSV File":
            st.markdown("**Attendance/Capacity Data:**")
            
            # Option to use dummy data or upload custom file
            data_option = st.radio(
                "Data source:",
                options=["Use sample/dummy data", "Upload custom CSV file"],
                index=0,
                key="inline_triaging_csv_option",
                horizontal=True
            )
            
            if data_option == "Use sample/dummy data":
                use_dummy_data = True
                st.info("📋 Will generate sample attendance data for testing. This creates realistic dummy capacity data for your team members.")
            else:
                uploaded_file = st.file_uploader(
                    "Choose CSV file with team attendance data",
                    type=["csv", "xlsx", "xls"],
                    key="inline_triaging_csv_upload",
                    help="CSV should have columns: Name, Email, Date, Hours, DaysOff"
                )
                if uploaded_file:
                    st.success(f"✓ File uploaded: {uploaded_file.name}")
                    # Preview the data
                    try:
                        import pandas as pd
                        if uploaded_file.name.endswith('.csv'):
                            df = pd.read_csv(uploaded_file)
                        else:
                            df = pd.read_excel(uploaded_file)
                        st.markdown("**Preview (first 5 rows):**")
                        st.dataframe(df.head(), use_container_width=True)
                        uploaded_file.seek(0)  # Reset file pointer for later use
                    except Exception as e:
                        st.warning(f"Could not preview file: {e}")
        
        # Google Sheets option
        google_sheet_url = None
        if source_type == "Google Sheets":
            st.markdown("**Google Sheets Configuration:**")
            google_sheet_url = st.text_input(
                "Google Sheet URL:",
                placeholder="https://docs.google.com/spreadsheets/d/...",
                key="inline_triaging_google_url"
            )
            st.info("💡 Make sure to share the sheet with your service account email")
        
        st.markdown("---")
        
        # Team/Project configuration
        col1, col2 = st.columns(2)
        with col1:
            team_input = st.text_input("Team (leave empty for all):", key="inline_triaging_team")
        with col2:
            project_input = st.text_input(
                "Project:", 
                value=os.getenv("ADO_PROJECT", "FracPro-OPS"), 
                key="inline_triaging_project"
            )
        
        if not team_input:
            st.warning("No team specified - will analyze ALL teams. This may take several minutes.")
        
        # Validate source selection
        can_run = True
        if source_type == "CSV File" and not use_dummy_data and not uploaded_file:
            st.warning("⚠️ Please upload a CSV file or select 'Use sample/dummy data'.")
            can_run = False
        if source_type == "Google Sheets" and not google_sheet_url:
            st.warning("⚠️ Please enter a Google Sheets URL or select a different data source.")
            can_run = False
        
        if st.button("Run Capacity Triaging", type="primary", key="inline_run_triaging_btn", disabled=not can_run):
            with st.spinner("Analyzing sprint capacity and risks..."):
                try:
                    from scripts.capacity_triaging import CapacityTriaging
                    from utilities.mcp.mcp_ado_connector import MCPConnector
                    from utilities.mcp.pat import get_pat
                    from utilities.capacity_data_sources import create_capacity_source
                    
                    # Handle CSV data source
                    temp_csv_path = None
                    if source_type == "CSV File":
                        import tempfile
                        if use_dummy_data:
                            # Generate dummy attendance data
                            temp_csv_path = _generate_dummy_attendance_csv()
                            st.info("📋 Generated sample attendance data for testing")
                        elif uploaded_file:
                            temp_csv_path = os.path.join(tempfile.gettempdir(), uploaded_file.name)
                            with open(temp_csv_path, 'wb') as f:
                                f.write(uploaded_file.getvalue())
                            st.info(f"📂 Using uploaded file: {uploaded_file.name}")
                    
                    # Map source type to config value
                    source_type_map = {
                        "Azure DevOps": "ado",
                        "CSV File": "csv",
                        "Google Sheets": "google-sheets"
                    }
                    selected_source = source_type_map.get(source_type, "ado")
                    
                    async def run_triaging():
                        pat = get_pat()
                        mcp = MCPConnector(
                            org_name=os.getenv("ADO_ORG_NAME", "Stratagen"), 
                            pat_token=pat
                        )
                        await mcp.initialize()
                        
                        # Create capacity source based on selection
                        capacity_source = None
                        if selected_source == "csv" and temp_csv_path:
                            capacity_source = create_capacity_source(
                                "csv",
                                {"csv_file_path": temp_csv_path}
                            )
                        elif selected_source == "google-sheets" and google_sheet_url:
                            from config import config as app_config
                            capacity_source = create_capacity_source(
                                "google-sheets",
                                {
                                    "sheet_url": google_sheet_url,
                                    "credentials_path": app_config.capacity_google_creds_path
                                }
                            )
                        # else: use ADO (default)
                        
                        triaging = CapacityTriaging(mcp, capacity_source)
                        teams = [team_input] if team_input else None
                        result = await triaging.run(project_input, teams)
                        return result
                    
                    result = asyncio.run(run_triaging())
                    
                    if result.get("success"):
                        st.success(f"Analysis complete! {result['teamsAnalyzed']} team(s) analyzed.")
                        
                        for team_result in result.get("results", []):
                            risk_level = team_result.get("riskAnalysis", {}).get("overallSeverity", "LOW")
                            risk_color = "red" if risk_level == "HIGH" else "orange" if risk_level == "MEDIUM" else "green"
                            
                            st.markdown(f"### {team_result['team']} - {team_result['iteration']}")
                            st.markdown(f"**Risk Level:** :{risk_color}[{risk_level}]")
                            
                            sprint = team_result.get("sprintAnalysis", {})
                            m1, m2, m3 = st.columns(3)
                            m1.metric("Sprint Elapsed", f"{sprint.get('sprintElapsedPct', 0):.0f}%")
                            m2.metric("Work Done", f"{sprint.get('donePct', 0):.0f}%")
                            m3.metric("Deviation", f"{sprint.get('deviationFromIdeal', 0):.1f}%")
                            
                            risks = team_result.get("riskAnalysis", {}).get("risks", [])
                            if risks:
                                st.markdown("**Identified Risks:**")
                                for risk in risks:
                                    st.warning(f"**{risk['type']}** ({risk['severity']}): {risk['message']}")
                        
                        html_files = list((ROOT / "outputs").glob("capacity_triaging_*.html"))
                        if html_files:
                            latest = max(html_files, key=lambda f: f.stat().st_mtime)
                            st.download_button(
                                "Download Full Report",
                                latest.read_text(encoding='utf-8'),
                                latest.name,
                                "text/html",
                                key="inline_dl_triaging_report"
                            )
                    else:
                        st.error(f"Analysis failed: {result.get('error', 'Unknown error')}")
                        
                except Exception as e:
                    st.error(f"Error running capacity triaging: {e}")
                    logger.exception("Capacity triaging error")


def render_backlog_triaging_inline():
    """Render Backlog Triaging section inline in chat."""
    with st.container():
        st.markdown("---")
        st.markdown("### 📊 Backlog Triaging")
        st.markdown("**Monitor backlog health and alert when backlog is running thin**")
        
        # Configuration inputs
        col1, col2 = st.columns(2)
        with col1:
            bt_project = st.text_input(
                "Project:", 
                value=os.getenv("ADO_PROJECT", "FracPro-OPS"), 
                key="inline_bt_project"
            )
        with col2:
            bt_team = st.text_input(
                "Team (optional):", 
                value=os.getenv("ADO_TEAM", ""), 
                key="inline_bt_team",
                placeholder="Leave empty to use project name"
            )
        
        # Threshold configuration
        st.markdown("**Threshold Settings:**")
        t1, t2 = st.columns(2)
        with t1:
            min_items = st.number_input(
                "Min Backlog Items:", 
                min_value=1, 
                max_value=100, 
                value=10, 
                key="inline_bt_min_items",
                help="Alert if backlog has fewer items than this"
            )
        with t2:
            min_story_points = st.number_input(
                "Min Story Points:", 
                min_value=0, 
                max_value=500, 
                value=0, 
                key="inline_bt_min_sp",
                help="Alert if total story points is below this (0 to disable)"
            )
        
        # Options
        force_send = st.checkbox(
            "Send email even if backlog is healthy", 
            value=False, 
            key="inline_bt_force_send"
        )
        
        st.divider()
        
        if st.button("Run Backlog Triaging", type="primary", key="inline_run_bt_btn"):
            with st.spinner("Analyzing backlog health..."):
                try:
                    from scripts.backlog_triaging import run_backlog_triaging, load_config
                    from utilities.mcp.pat import get_pat
                    
                    async def run_bt():
                        # Load and update config with UI values
                        cfg = load_config()
                        bt_cfg = cfg.setdefault("backlog_triaging", {})
                        thresholds = bt_cfg.setdefault("thresholds", {})
                        thresholds["min_items"] = min_items
                        thresholds["min_story_points"] = min_story_points
                        
                        # Set environment variables
                        os.environ["ADO_PROJECT"] = bt_project
                        if bt_team:
                            os.environ["ADO_TEAM"] = bt_team
                        else:
                            os.environ["ADO_TEAM"] = bt_project
                        
                        # Get org URL from PAT helper
                        org_name = os.getenv("ADO_ORG_NAME", "Stratagen")
                        os.environ["ADO_ORG_URL"] = f"https://dev.azure.com/{org_name}"
                        
                        result = await run_backlog_triaging(
                            config=cfg,
                            options={"force_send": force_send},
                            recipients=cfg.get("reportEmailRecipients", [])
                        )
                        return result
                    
                    result = asyncio.run(run_bt())
                    
                    if result.get("success"):
                        health = result.get("health_analysis", {})
                        is_thin = result.get("is_thin", False)
                        
                        # Display result metrics
                        status_icon = "🔴" if is_thin else "🟢"
                        status_text = "THIN - Action Required!" if is_thin else "Healthy"
                        st.markdown(f"### {status_icon} Backlog Status: {status_text}")
                        
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("Backlog Items", health.get("backlog_items", 0))
                        m2.metric("Story Points", health.get("total_story_points", 0))
                        m3.metric("Sprints Coverage", f"{health.get('sprints_coverage', 0):.1f}")
                        m4.metric("Health Score", f"{health.get('health_score', 0):.0f}%")
                        
                        # Show reasons if thin
                        reasons = health.get("thin_reasons", [])
                        if reasons:
                            st.warning("**Issues detected:**")
                            for reason in reasons:
                                st.markdown(f"- {reason}")
                        
                        # Email status
                        if result.get("email_sent"):
                            st.success("✉️ Alert email sent to recipients")
                        elif is_thin:
                            st.info("Email not sent (check notification settings)")
                        
                        # Download report
                        html_files = list((ROOT / "outputs").glob("backlog_triaging_*.html"))
                        if html_files:
                            latest = max(html_files, key=lambda f: f.stat().st_mtime)
                            st.download_button(
                                "Download Full Report",
                                latest.read_text(encoding='utf-8'),
                                latest.name,
                                "text/html",
                                key="inline_dl_bt_report"
                            )
                    else:
                        st.error(f"Backlog triaging failed: {result.get('message', 'Unknown error')}")
                        
                except Exception as e:
                    st.error(f"Error running backlog triaging: {e}")
                    logger.exception("Backlog triaging error")


# =============================================================================
# MAIN HANDLER
# =============================================================================

SECTION_RENDERERS = {
    "developer_knowledge_base": render_developer_knowledge_base_inline,
    "sprint_plan": render_sprint_plan_inline,
    "backlog_assignments": render_backlog_assignments_inline,
    "capacity_check": render_capacity_check_inline,
    "capacity_triaging": render_capacity_triaging_inline,
    "backlog_triaging": render_backlog_triaging_inline,
}


def render_section_if_requested(query_or_key: str) -> bool:
    """Detect and render a section based on a user query or a section key.

    Accepts either a raw user query (e.g. "show sprint plan form") or a
    section key (e.g. "sprint_plan"). Returns True if a section was rendered.
    """
    # If caller passed a section key directly, render it
    if query_or_key in SECTION_RENDERERS:
        SECTION_RENDERERS[query_or_key]()
        return True

    # Otherwise, try to detect the section from the user's query text
    section = detect_section_from_query(query_or_key)
    if section and section in SECTION_RENDERERS:
        SECTION_RENDERERS[section]()
        return True
    return False
