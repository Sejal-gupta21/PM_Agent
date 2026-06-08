import asyncio
import streamlit as st
import os
from config import config
from utilities.mcp.mcp_ado_connector import get_mcp_connector
from utilities.mcp.email_utils import format_deployment_email, send_email_smtp, evaluate_schedule


st.title("📧 Deployment Email Generator")
st.write("Generate a deployment-schedule email draft from an Azure DevOps work item.")

wi_input = st.text_input("Work Item ID (numeric or US-123)")
recipient = st.text_input("Recipient email (optional)")
include_ext = st.checkbox("Try Time Log extension for extra context (may require special PAT scopes)", value=True)

st.markdown("---")
st.subheader("SMTP (optional)")
use_smtp = st.checkbox("Enable SMTP send (use env vars if left blank)")
smtp_host = st.text_input("SMTP host (optional, will use env SMTP_HOST if empty)")
smtp_port = st.number_input("SMTP port", value=587)
smtp_user = st.text_input("SMTP username (optional)")
smtp_pass = st.text_input("SMTP password (optional)", type="password")
from_addr = st.text_input("From address (optional)")


if st.button("Generate Email"):
    if not wi_input:
        st.warning("Please enter a Work Item ID first.")
    else:
        try:
            mcp = asyncio.run(get_mcp_connector())
            # Use the programmatic helper available on the connector
            data = asyncio.run(mcp.timelog_get_time_and_comments(wi_input, includeTimeLogExtension=include_ext))
        except Exception as e:
            st.error(f"Error calling MCP connector: {e}")
            data = None

        if data is None:
            st.error("No data returned from MCP connector.")
        elif isinstance(data, dict) and data.get("error"):
            st.error(f"Connector error: {data.get('error')}\nRaw: {data.get('raw') if 'raw' in data else ''}")
        else:
            subj, body = format_deployment_email(data, recipient or None, include_status=True)
            st.subheader("Generated Email Draft")
            st.code(body)

            eval_res = evaluate_schedule(data.get('deploymentSchedule', {}) or data.get('deployments', {}), data.get('timeSummary', {}))
            st.info(f"Schedule evaluation: {eval_res.get('status')} — {', '.join(eval_res.get('reasons', []))}")

            if use_smtp:
                host = smtp_host or config.smtp_host
                port = int(smtp_port or config.smtp_port or 587)
                user = smtp_user or config.smtp_username
                pwd = smtp_pass or config.smtp_password
                sender = from_addr or config.from_email
                if not host or not sender:
                    st.error('SMTP host and From address required to send email')
                else:
                    to_list = [recipient] if recipient else [sender]
                    send_res = send_email_smtp(host, port, user, pwd, sender, to_list, subj, body, use_tls=True)
                    if send_res.get('ok'):
                        st.success('Email sent successfully (SMTP)')
                    else:
                        st.error(f"Failed to send email: {send_res.get('error')}")
            else:
                if st.button("Simulate Send (log only)"):
                    st.success("Email simulated (logged to server console). Check server logs for output.")
                    print("--- Simulated Email Send ---")
                    print(subj)
                    print(body)
                    print("--- End Email ---")
