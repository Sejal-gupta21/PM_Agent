# -*- coding: utf-8 -*-
"""
Sidebar UI Module.

Extracted from chat_ai.py to keep the main chat file thin.
Provides UI components for sidebar panels.
"""
import os
import asyncio
import requests
from typing import Tuple, List, Dict, Any

from config import config
from app.chat_service import retrieve_all_tools


def render_mcp_discovery_panel(st) -> None:
    """
    Render the MCP Discovery panel in the sidebar.
    
    Args:
        st: The Streamlit module instance
    """
    with st.sidebar.expander('MCP Discovery', expanded=False):
        if st.button('Discover MCP servers and agents', key='discover_mcp_btn'):
            try:
                mcp_servers, agents = asyncio.run(retrieve_all_tools())
                st.session_state.mcp_servers = mcp_servers
                st.session_state.agents = agents
                st.success(f"Discovered {len(mcp_servers)} MCP servers and {len(agents)} agents.")
            except Exception as e:
                st.error(f"Discovery failed: {e}")


def render_discovered_servers(st) -> None:
    """
    Render discovered MCP servers and agents.
    
    Args:
        st: The Streamlit module instance
    """
    for server in st.session_state.get('mcp_servers', []):
        col1, col2, col3 = st.columns([2, 4, 2])
        col1.write(f"**{server['name']}**")
        col2.write(server["url"])
        col3.write(server["status"])

    for agent in st.session_state.get('agents', []):
        col1, col2, col3 = st.columns([2, 4, 2])
        col1.write(f"**{agent['name']}**")
        col2.write(agent["description"][:100] + "...")
        col3.write(agent["status"])


def render_mcp_connector_panel(st) -> None:
    """
    Render the MCP Connector panel in the sidebar.
    
    Args:
        st: The Streamlit module instance
    """
    with st.sidebar.expander('MCP Connector', expanded=False):
        st.write('Azure DevOps MCP Connector will be initialized on demand by the orchestrator.')
        
        # Test PM Agent button
        if st.button('Test PM Agent: List Area Paths', key='test_pm_area_paths'):
            _test_pm_agent_list_area_paths(st)


def _test_pm_agent_list_area_paths(st) -> None:
    """Handle the Test PM Agent button click."""
    pm_url = config.pm_agent_url
    payload = {
        "skill": "list_area_paths", 
        "project": config.ado_project
    }
    try:
        resp = requests.post(pm_url, json=payload, timeout=20)
        if resp.status_code == 200:
            result = resp.json().get('result')
            st.success('PM agent responded')
            st.code(result)
        else:
            st.error(f'PM agent returned HTTP {resp.status_code}')
    except Exception as e:
        st.error(f'Error calling PM agent: {e}')


def render_email_settings_panel(st) -> None:
    """
    Render the Email Settings panel in the sidebar.
    
    Args:
        st: The Streamlit module instance
    """
    with st.sidebar.expander('Email settings', expanded=False):
        st.write('SMTP (Gmail): set ALERTMANAGER_SMTP_FROM/USERNAME/PASSWORD/HOST/PORT in env (.env supported).')
        st.write('Optional SendGrid: SENDGRID_API_KEY and FROM_EMAIL.')


def init_session_state(st) -> None:
    """
    Initialize all required session state variables.
    
    Args:
        st: The Streamlit module instance
    """
    defaults = {
        "mcp_servers": [],
        "agents": [],
        "messages": [],
        "mcp_connector": None,
        "last_report_file": None,
        "last_filtered_file": None,
        "mcp_initialized": False,
    }
    
    for key, default_value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default_value
