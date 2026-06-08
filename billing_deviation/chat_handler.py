"""
Billing deviation chat handler moved out of `app/chat_ai.py`.
This module exposes `handle_billing_deviation(prompt)` which performs the
same behavior previously in `chat_ai.py` (generate report, append session
messages, show toasts) without changing the logic.
"""
from __future__ import annotations
import os
import re
import subprocess
from glob import glob
from pathlib import Path
from datetime import datetime
import logging
import csv
import streamlit as st

logger = logging.getLogger(__name__)


def handle_billing_deviation(prompt: str) -> None:
    """Handle a billing deviation prompt with UI form for user inputs.

    NEW BEHAVIOR: Shows a form for Area Path & Target Hours before generating report.
    User must fill the form and click Generate to create the report.
    """
    try:
        # Check if we're processing a submitted form (MUST be first, before prompt validation)
        if st.session_state.get('billing_form_submitted', False):
            # If the UI flow originated from a controller/orchestrator trace, attempt
            # to re-join that trace so the generated task is linked for observability.
            try:
                pending_tid = st.session_state.get('_pending_trace_id')
                if pending_tid:
                    from utilities.langfuse_client import join_trace
                    join_trace(
                        pending_tid,
                        name=f"joined_billing_deviation_task",
                        session_id=st.session_state.get('session_id')
                    )
                    logger.debug(f"Joined pending trace for billing submission: {pending_tid}")
            except Exception:
                logger.exception("Failed to join pending trace before billing task")

            from billing_deviation.billing_orchestrator import run_billing_deviation_report

            area_paths = st.session_state.get('billing_area_paths', [])
            target_hours = st.session_state.get('billing_target_hours', 2000.0)
            iteration_path = st.session_state.get('billing_iteration_path')
            recipient_email = st.session_state.get('billing_recipient_email')
            filter_current_month = st.session_state.get('billing_filter_current_month', False)
            month = st.session_state.get('billing_month')
            year = st.session_state.get('billing_year')

            # Show spinner while generating report
            st.session_state.billing_generating = True
            with st.spinner("Generating billing deviation report — this may take a minute..."):
                response = run_billing_deviation_report(
                    iteration_path=iteration_path,
                    recipient_email=recipient_email,
                    area_paths=area_paths,
                    user_target_hours=target_hours,
                    filter_current_month=filter_current_month,
                    month=month,
                    year=year
                )
            st.session_state.billing_generating = False

            # Reset form state
            st.session_state.billing_form_submitted = False
            st.session_state.billing_waiting_for_form = False
            # Clear any pending trace id now that the task has run (avoid reuse)
            try:
                st.session_state['_pending_trace_id'] = None
            except Exception:
                pass

            # Append and render response
            st.session_state.messages.append({"role": "assistant", "content": response})
            with st.chat_message("assistant"):
                st.markdown(response.replace('\n', '  \n'))

            # Show success/warning toast
            if recipient_email:
                if "✅" in response:
                    st.success(f"Billing deviation report sent to {recipient_email}")
                elif "⚠️" in response:
                    st.warning(f"Email {recipient_email} not in allowed recipients list")
            return
        
        # Now check prompt for initial request
        # Accept any billing-related query (over-billing, under-billing, billing target, etc.)
        qlow = prompt.lower()
        billing_keywords = ["billing", "over-billing", "under-billing", "overbilling", "underbilling"]
        if not any(keyword in qlow for keyword in billing_keywords):
            return

        # If we're already waiting for form input, don't show the prompt again
        if st.session_state.get('billing_waiting_for_form', False):
            return

        emails_in_prompt = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", prompt)
        recipient_email = emails_in_prompt[0] if emails_in_prompt else None

        # Parse iteration intent from prompt
        iteration_path = None
        m = re.search(r"sprint\s*#?\s*(\d+)", prompt, re.IGNORECASE)
        if m:
            iteration_path = f"Sprint {m.group(1)}"
        elif re.search(r"\b(this sprint|current sprint|this iteration|current iteration|@currentiteration|recent sprint)\b", qlow):
            from config import config as app_config
            iteration_path = app_config.query_iteration_path
        elif re.search(r"\b(last sprint|previous sprint|previous iteration)\b", qlow):
            from config import config as app_config
            iteration_path = app_config.query_iteration_path
        else:
            from config import config as app_config
            iteration_path = app_config.query_iteration_path

        # Check if user wants current month filtering
        # NOTE: Billing deviation ALWAYS uses current month (closed items only) as per requirements
        # This flag is kept for backward compatibility but is effectively always True
        filter_current_month = True  # Always use current month for billing deviation
        
        # Store iteration, email, and month filter flag in session for later use
        st.session_state.billing_iteration_path = iteration_path
        st.session_state.billing_recipient_email = recipient_email
        st.session_state.billing_filter_current_month = filter_current_month
        st.session_state.billing_waiting_for_form = True

        # Show form prompt message
        form_prompt = "📋 **Please provide the following information to generate the Billing Deviation Report:**\n\n*Note: Report will include only Closed/Completed work items from current month up to today ({}).*".format(datetime.now().strftime('%B %d, %Y'))
        st.session_state.messages.append({"role": "assistant", "content": form_prompt})
        with st.chat_message("assistant"):
            st.markdown(form_prompt)
            # Render the form immediately so it appears with the message
            render_billing_form_if_needed()

    except Exception as e:
        response = f"❌ Error generating billing deviation report: {e}"
        try:
            # Reset form state on error
            st.session_state.billing_waiting_for_form = False
            st.session_state.billing_form_submitted = False
            
            st.session_state.messages.append({"role": "assistant", "content": response})
            with st.chat_message("assistant"):
                st.markdown(response)
        except Exception:
            logger.exception("Failed to append billing deviation error response to Streamlit session")


def render_billing_form_if_needed() -> None:
    """Render the billing deviation form if we're waiting for user input.
    
    This should be called from the main chat UI after messages are displayed.
    """
    if not st.session_state.get('billing_waiting_for_form', False):
        return

    try:
        # Display the form. Keep inputs persistent across reruns so user can edit before submit.
        with st.form(key="billing_deviation_form", clear_on_submit=False):
            st.markdown("### Billing Deviation Report Input")

            # Try to fetch available area paths for the configured iteration so we can offer a multi-select.
            available_areas = []
            try:
                from billing_deviation.ado_fetcher import ADOEffortFetcher
                fetcher = ADOEffortFetcher()
                # Use completed items from current month to populate dropdown (matches what report will use)
                work_items = fetcher.fetch_completed_work_items_current_month()
                effort_data = fetcher.extract_effort_data(work_items)
                available_areas = sorted(list(effort_data.get('by_area', {}).keys()))
            except Exception:
                # If ADO is not reachable or misconfigured, leave available_areas empty
                available_areas = []

            selected_areas: list[str] = []
            if available_areas:
                st.markdown("**Select Area Path(s)**")
                selected_areas = st.multiselect(
                    "Choose one or more area paths (checkboxes)",
                    options=available_areas,
                    default=None,
                    help="Select area paths from the project."
                )
                st.markdown("---")
            else:
                st.info("No area paths fetched for the selected iteration — you can type comma-separated area paths manually below.")
                manual_areas = st.text_input(
                    "Enter Area Path(s) (comma-separated)",
                    value="",
                    placeholder="Area1, Area2, Area3",
                    help="If ADO is unavailable, type area paths manually separated by commas.",
                    key="billing_manual_areas"
                )
                # Parse manual input into list
                selected_areas = [a.strip() for a in (manual_areas or "").split(',') if a.strip()]

            target_hours_input = st.number_input(
                "Target Hours",
                min_value=0.0,
                value=2000.0,
                step=100.0,
                help="Enter the target hours for billing"
            )
            
            st.markdown("---")
            
            # Month and Year dropdowns
            col1, col2 = st.columns(2)
            
            with col1:
                month_names = ["January", "February", "March", "April", "May", "June", 
                              "July", "August", "September", "October", "November", "December"]
                current_month_idx = datetime.now().month - 1  # 0-indexed
                selected_month_name = st.selectbox(
                    "Month",
                    options=month_names,
                    index=current_month_idx,
                    help="Select the month for the billing report"
                )
                selected_month = month_names.index(selected_month_name) + 1  # Convert to 1-12
            
            with col2:
                current_year = datetime.now().year
                # Provide years from 2 years ago to current year
                year_options = list(range(current_year - 2, current_year + 1))
                selected_year = st.selectbox(
                    "Year",
                    options=year_options,
                    index=len(year_options) - 1,  # Default to current year
                    help="Select the year for the billing report"
                )

            # Show submit button (allow manual entry when ADO not available)
            submit_button = st.form_submit_button("Generate Report")

            if submit_button:
                # Accept either selected areas (from dropdown) or manual areas text input
                if not selected_areas:
                    st.error("❌ Please select at least one area path or enter manual area paths in the text input")
                    return

                if target_hours_input <= 0:
                    st.error("❌ Target hours must be greater than 0")
                    return

                # Store form data in session
                st.session_state.billing_area_paths = selected_areas
                st.session_state.billing_target_hours = target_hours_input
                st.session_state.billing_month = selected_month
                st.session_state.billing_year = selected_year
                st.session_state.billing_form_submitted = True
                # Rerun to let the handler observe the submitted state
                st.rerun()
    except Exception:
        logger.exception("Failed to render billing deviation form")
