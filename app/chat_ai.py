# -*- coding: utf-8 -*-
"""
Streamlit app for PM Agent - Chat AI interface.

This is a THIN UI layer (~150 lines) that only handles:
- Page configuration
- Session state initialization  
- Rendering sidebar and main UI components
- Routing user prompts to chat_service

All business logic is delegated to app.chat_service.
"""
import os
import sys
import asyncio
from pathlib import Path
from datetime import datetime, timedelta

# Ensure project root is on PYTHONPATH
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import config

# Populate environment variables from config for backward compatibility
# This ensures code using os.getenv() still works after config.yaml migration
os.environ.setdefault("ADO_ORG_URL", config.ado_org_url or "")
os.environ.setdefault("ADO_ORG_NAME", config.ado_org_name or "")
os.environ.setdefault("ADO_PROJECT", config.ado_project or "")
os.environ.setdefault("ADO_TEAM", config.ado_team or "")
os.environ.setdefault("ADO_ITERATION", config.ado_iteration or "")
os.environ.setdefault("ADO_PAT", config.ado_pat or "")
os.environ.setdefault("ADO_MCP_AUTH_TOKEN", config.ado_mcp_auth_token or "")
os.environ.setdefault("OPENAI_API_KEY", config.openai_api_key or "")
# Capacity Triaging environment variables
os.environ.setdefault("CAPACITY_SOURCE_TYPE", config.capacity_source_type or "ado")
os.environ.setdefault("CAPACITY_SOURCE_URL", config.capacity_csv_file_path or config.capacity_google_sheets_url or "")
os.environ.setdefault("CAPACITY_GOOGLE_CREDS_PATH", config.capacity_google_credentials_path or "credentials/google_sheets_creds.json")
os.environ.setdefault("CAPACITY_DEVIATION_THRESHOLD", str(config.capacity_deviation_threshold))
os.environ.setdefault("SPRINT_PROGRESS_THRESHOLD", str(config.sprint_progress_threshold))
# Langfuse environment variables
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.langfuse_public_key or "")
os.environ.setdefault("LANGFUSE_SECRET_KEY", config.langfuse_secret_key or "")
os.environ.setdefault("LANGFUSE_BASE_URL", config.langfuse_base_url or "https://cloud.langfuse.com")

# Setup logging early
from utilities.logging_config import setup_logging, get_logger
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = get_logger(__name__)

# Import billing form renderer
from billing_deviation.chat_handler import render_billing_form_if_needed

# Ensure UTF-8 encoding for console output
if sys.stdout.encoding and 'utf' not in sys.stdout.encoding.lower():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import streamlit as st
import pandas as pd

# Configure page
st.set_page_config(
    page_title="PM Agent Chat",
    page_icon="💬",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# Load .env for development
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Import services
from config import config
from app import chat_service as svc
from app.sidebar_ui import (
    init_session_state,
    render_mcp_discovery_panel,
    render_discovered_servers,
    render_mcp_connector_panel,
    render_email_settings_panel,
)
from app.report_ui import render_iteration_report_section

# Initialize session state
init_session_state(st)

# Page title
st.title("💬 PM Agent Chat")

# =============================================================================
# SIDEBAR PANELS
# =============================================================================
render_mcp_discovery_panel(st)
render_discovered_servers(st)
render_mcp_connector_panel(st)
render_email_settings_panel(st)
render_iteration_report_section(st)

# Import chat extensions for dynamic section rendering
from app.chat_extensions import render_section_if_requested, detect_section_from_query

# =============================================================================
# CHAT INTERFACE
# =============================================================================
if st.button("Reset Chat", type="primary", use_container_width=True):
    st.session_state.messages.clear()
    # Clear any section display state
    if "show_section" in st.session_state:
        del st.session_state["show_section"]

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"], unsafe_allow_html=True)
        # Render inline section if this message triggered one
        if msg.get("_section"):
            render_section_if_requested(msg["_section"])


# Render billing deviation form if needed
render_billing_form_if_needed()

# If billing form was just submitted, trigger report generation
import streamlit as st
if st.session_state.get('billing_form_submitted', False):
    from billing_deviation.chat_handler import handle_billing_deviation
    handle_billing_deviation("")

if prompt := st.chat_input("Type your message...", key="prompt"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    
    # Check if user is asking for a UI section (non-chat operations)
    detected_section = detect_section_from_query(prompt)
    if detected_section:
        # Provide helpful response and render the section inline
        section_responses = {
            "developer_knowledge_base": "Here's the **Developer Knowledge Base** panel. You can refresh the knowledge base to see developer skills and expertise.",
            "sprint_plan": "Here's the **Sprint Plan Generator**. Configure sprint details and generate a plan.",
            "backlog_assignments": "Here's the **Backlog Assignments** panel. Assign backlog items to team members.",
            "capacity_check": "Here's the **Capacity Check** panel showing current team capacity.",
            "capacity_triaging": "Here's the **Capacity Triaging** panel for sprint risk analysis.",
        }
        response = section_responses.get(detected_section, f"Here's the **{detected_section.replace('_', ' ').title()}** panel.")
        st.session_state.messages.append({"role": "assistant", "content": response, "_section": prompt})
        with st.chat_message("assistant"):
            st.markdown(response)
            render_section_if_requested(prompt)
    else:
        # CANONICAL FLOW: ALL requests (including fixed skills) go through:
        # Controller → Orchestrator → LLM Planner → Agent (PM Agent or PM Skills Agent)
        # 
        # The orchestrator's router detects fixed skills via:
        # 1. Intent detection from utilities.query_normalizer
        # 2. Semantic skill matching from utilities.skill_registry
        # 3. Pattern matching in orchestrator.router.PM_SKILL_PATTERNS
        #
        # This ensures consistent tracing in Langfuse: controller_request → orchestrator_process → planner → agent
        svc.handle_chat_prompt(prompt, st, config)
        
        # Check if a UI form section needs to be rendered after the response
        # This happens when a skill like sprint_plan, backlog_triaging, etc. is detected
        pending_section = st.session_state.pop("_pending_section", None)
        if pending_section:
            from app.chat_extensions import SECTION_RENDERERS
            renderer = SECTION_RENDERERS.get(pending_section)
            if renderer:
                renderer()
