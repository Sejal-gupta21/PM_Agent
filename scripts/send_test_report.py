#!/usr/bin/env python3
"""Generate the iteration report and send it once to recipients in config.yaml.

Usage:
  source .venv/bin/activate
  python3 scripts/send_test_report.py

This script loads .env (if present), generates the iteration report using the
existing generator, finds the most recent generated CSV/HTML, builds a short
summary, and sends an email with the CSV attached to the recipients listed in
`config.yaml` under `reportEmailRecipients`.
"""
import os
import sys
import glob
import csv
import subprocess
from datetime import datetime
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None

def run_report():
    print("Generating iteration report (this may contact Azure DevOps)...")
    # Use the script entrypoint directly; it writes into outputs/
    subprocess.run([sys.executable, "scripts/generate_iteration_report.py"], check=True)
    print("Report generation finished.")

def find_latest_report():
    csvs = sorted(glob.glob("outputs/iteration_report*.csv"), key=os.path.getmtime, reverse=True)
    htmls = sorted(glob.glob("outputs/iteration_report*.html"), key=os.path.getmtime, reverse=True)
    return (csvs[0] if csvs else None, htmls[0] if htmls else None)

def build_summary(csv_path):
    if not csv_path:
        return "No report CSV found."
    try:
        with open(csv_path, newline='', encoding='utf-8') as fh:
            reader = csv.reader(fh)
            rows = list(reader)
            count = max(0, len(rows) - 1)
    except Exception as e:
        return f"Could not read CSV: {e}"
    summary = (
        f"Iteration Report Summary\n\n"
        f"Generated: {datetime.utcnow().isoformat()} UTC\n"
        f"Items in report: {count}\n\n"
        "Please find the attached iteration report (CSV)."
    )
    return summary

def load_recipients():
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        raise SystemExit("config.yaml not found in repo root.")
    if yaml is None:
        raise SystemExit("PyYAML not installed in venv; run `pip install pyyaml` in .venv")
    cfg = yaml.safe_load(cfg_path.read_text(encoding='utf-8'))
    recipients = cfg.get("reportEmailRecipients") or []
    return [r for r in recipients if r]

def send_email(subject, body, to_addrs, csv_path=None, html_path=None, attach_html=False):
    import smtplib
    from email.message import EmailMessage
    from mimetypes import guess_type
    from config import config

    smtp_host = config.smtp_host
    smtp_port = config.smtp_port
    smtp_user = config.smtp_username
    smtp_pass = config.smtp_password

    if not smtp_user or not smtp_pass:
        raise SystemExit("SMTP credentials not set in config.yaml (smtp.username / smtp.password).")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

    if csv_path and Path(csv_path).exists():
        with open(csv_path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="text", subtype="csv", filename=Path(csv_path).name)

    if attach_html and html_path and Path(html_path).exists():
        with open(html_path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="text", subtype="html", filename=Path(html_path).name)

    print(f"Connecting to SMTP {smtp_host}:{smtp_port} as {smtp_user} ...")
    with smtplib.SMTP(smtp_host, smtp_port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    print("Email sent.")

def main():
    from config import config
    run_report()
    csv_path, html_path = find_latest_report()
    print("Found:", csv_path, html_path)
    summary = build_summary(csv_path)
    recipients = load_recipients()
    if not recipients:
        raise SystemExit("No recipients found in config.yaml (reportEmailRecipients).")

    attach_html = config.report_send_attach_html
    subject = "Test: Iteration report — " + datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print("Sending email to:", recipients)
    send_email(subject, summary, recipients, csv_path=csv_path, html_path=html_path, attach_html=attach_html)

if __name__ == "__main__":
    main()
