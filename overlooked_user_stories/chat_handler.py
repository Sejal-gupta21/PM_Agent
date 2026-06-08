"""
Handler for overlooked user stories trigger moved from `app/chat_ai.py`.

This mirrors the previous inlined logic: runs the reminder script,
parses outputs, appends summary to Streamlit session, and shows toasts.
"""
from __future__ import annotations
import os
import sys
import re
import subprocess
from glob import glob
from pathlib import Path
import logging
from typing import Optional
import streamlit as st

logger = logging.getLogger(__name__)


def handle_overlooked(prompt: str) -> None:
    """Handle an 'overlooked' prompt and run the overlooked-stories script.

    Appends a concise assistant response to `st.session_state.messages`, renders
    it, and shows success/warning toasts as before.
    """
    try:
        # Parse email in user's prompt. If none provided, we will run the report
        # in dry-run mode and show the summary in the UI (no emails sent).
        emails_in_prompt = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", prompt)
        consolidated = emails_in_prompt[0] if emails_in_prompt else None
        dry_run = False if consolidated else True

        cmd = [sys.executable, "overlooked_user_stories/overlooked_stories_reminder.py"]
        run_env = os.environ.copy()
        
        # Set up PYTHONPATH to ensure config module can be imported
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        run_env["PYTHONPATH"] = str(repo_root)
        
        if consolidated:
            run_env["OVERLOOKED_SEND_TO"] = consolidated
        # If no recipient was provided by the user, request a dry-run to avoid sending emails
        if dry_run:
            cmd.append("--dry-run")
            
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=run_env, cwd=str(repo_root))
        out = proc.stdout or ""
        err = proc.stderr or ""

        # parse script output (both stdout and stderr) for basic status and send result
        found_line = None
        consolidated_line = None
        consolidated_ok: Optional[bool] = None
        combined = (out or "") + "\n" + (err or "")
        for line in combined.splitlines():
            l = line.strip()
            if "Found" in l and "candidate work items" in l:
                found_line = l
            if "Consolidated report" in l or "CONSOLIDATED REPORT SUCCESS" in l:
                consolidated_line = l
            if "Consolidated report sent:" in l and "success=" in l:
                m = re.search(r"success=(True|False)", l)
                if m:
                    consolidated_ok = True if m.group(1) == "True" else False

        # Try to locate the generated summary/csv and read a compact preview
        preview_text = None
        try:
            from config import config as app_config
            project = app_config.ado_project
            pattern = f"overlooked_stories_{project}_ALL_"
            cand = sorted(glob(f"outputs/{pattern}*.csv"), reverse=True)
            if cand:
                csvp = cand[0]
                import csv as _csv
                with open(csvp, newline='', encoding='utf-8') as fh:
                    rdr = _csv.DictReader(fh)
                    rows = [r for _, r in zip(range(5), rdr)]
                count = 0
                with open(csvp, 'r', encoding='utf-8') as fh:
                    count = sum(1 for _ in fh) - 1
                preview_lines = [f"Items: {count}"]
                for r in rows:
                    epic = r.get('EpicTitle', '')[:30]
                    feature = r.get('FeatureTitle', '')[:30]
                    title = r.get('Title', '')[:50]
                    preview_lines.append(f"📘 {epic} → {feature}")
                    preview_lines.append(f"   {r.get('ID','')} — {title}")
                preview_text = "\n".join(preview_lines)
        except Exception:
            preview_text = None

        # Build concise response
        concise_parts = []
        if consolidated_line:
            concise_parts.append(consolidated_line)
        if found_line:
            concise_parts.append(found_line)
        if preview_text:
            concise_parts.append(preview_text)
        if not concise_parts:
            if proc.returncode == 0:
                concise_parts.append("Overlooked-stories script ran; no summary available.")
            else:
                error_msg = f"Script error (exit code {proc.returncode}):"
                if err.strip():
                    error_msg += f"\n{err.strip()}"
                if out.strip():
                    error_msg += f"\nOutput: {out.strip()}"
                concise_parts.append(error_msg)
        
        # Log detailed info for debugging
        logger.info(f"Overlooked script return code: {proc.returncode}")
        logger.info(f"Consolidated email validation result: {consolidated_ok}")
        if err:
            logger.info(f"Script stderr: {err[:500]}")

        response = "\n\n".join(concise_parts)
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        logger.error(f"Error in handle_overlooked: {error_detail}")
        response = f"❌ Error running overlooked-stories report: {e}"

    # append and render concise assistant response
    try:
        st.session_state.messages.append({"role": "assistant", "content": response})
        with st.chat_message("assistant"):
            st.markdown(response.replace('\n', '  \n'))
    except Exception:
        logger.exception("Failed to append/render overlooked-stories response")

        # Provide a clear UI confirmation toast when an email was requested.
    try:
        if consolidated:
            import re as _re
            m = _re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", consolidated)
            email_addr = m.group(0) if m else consolidated
            short_found = found_line
            if consolidated_ok is True:
                msg = f"Email sent to {email_addr}."
                if short_found:
                    msg = msg + " " + short_found
                st.success(msg)
            elif consolidated_ok is False:
                err_msg = consolidated_line or "Failed to send consolidated report."
                st.error(f"Failed to send email to {email_addr}: {err_msg}")
            else:
                notice = consolidated_line or "Report generated; send status unknown."
                st.warning(f"{notice} (Requested: {email_addr})")
        else:
            # No recipient provided: inform user we showed the summary (dry-run)
            st.info("Report summary displayed (no email requested).")
    except Exception:
        pass
