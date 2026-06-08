#!/usr/bin/env python3
"""
Validation script for Daily Report email output.
Verifies that the generated .eml file contains expected content.

Usage:
    python scripts/validate_daily_report.py outputs/dryrun_email_*.eml
    python scripts/validate_daily_report.py --latest
"""
import argparse
import email
from email.header import decode_header
import os
import re
import sys
from glob import glob
from pathlib import Path


def decode_email_header(header_value: str) -> str:
    """Decode an email header that may be encoded (e.g., =?utf-8?q?...?=)."""
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="ignore"))
        else:
            result.append(part)
    return " ".join(result)


def find_latest_eml() -> Path:
    """Find the most recent .eml file in outputs/."""
    outputs_dir = Path(__file__).resolve().parent.parent / "outputs"
    files = sorted(glob(str(outputs_dir / "dryrun_email_*.eml")), key=os.path.getmtime, reverse=True)
    if not files:
        raise FileNotFoundError("No dryrun_email_*.eml files found in outputs/")
    return Path(files[0])


def validate_email(eml_path: Path) -> dict:
    """Validate the email file and return results."""
    results = {
        "file": str(eml_path),
        "valid": True,
        "errors": [],
        "warnings": [],
        "checks": {},
    }
    
    if not eml_path.exists():
        results["valid"] = False
        results["errors"].append(f"File not found: {eml_path}")
        return results
    
    # Parse email
    with open(eml_path, "r", encoding="utf-8") as f:
        msg = email.message_from_file(f)
    
    # Check 1: Subject contains date
    subject_raw = msg.get("Subject", "")
    subject = decode_email_header(subject_raw)  # Decode encoded subject
    date_pattern = r"\d{4}-\d{2}-\d{2}"
    has_date = bool(re.search(date_pattern, subject))
    results["checks"]["subject_has_date"] = has_date
    if not has_date:
        results["errors"].append("Subject does not contain a date (YYYY-MM-DD)")
        results["valid"] = False
    
    # Check 2: Subject contains "Daily Report"
    has_daily_report = "Daily Report" in subject or "daily report" in subject.lower()
    results["checks"]["subject_has_daily_report"] = has_daily_report
    if not has_daily_report:
        results["errors"].append("Subject does not contain 'Daily Report'")
        results["valid"] = False
    
    # Check 3: Has recipients
    to = msg.get("To", "")
    has_recipients = bool(to.strip())
    results["checks"]["has_recipients"] = has_recipients
    if not has_recipients:
        results["errors"].append("No recipients in 'To' field")
        results["valid"] = False
    
    # Check 4: Has From
    from_addr = msg.get("From", "")
    has_from = bool(from_addr.strip())
    results["checks"]["has_from"] = has_from
    if not has_from:
        results["warnings"].append("No 'From' address")
    
    # Check 5: Has attachment
    has_attachment = False
    attachment_names = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            has_attachment = True
            filename = part.get_filename()
            if filename:
                attachment_names.append(filename)
    
    results["checks"]["has_attachment"] = has_attachment
    results["checks"]["attachments"] = attachment_names
    if not has_attachment:
        results["warnings"].append("No attachment found")
    
    # Check 6: Has multipart content
    is_multipart = msg.is_multipart()
    results["checks"]["is_multipart"] = is_multipart
    if not is_multipart:
        results["warnings"].append("Email is not multipart (should have text and HTML versions)")
    
    # Check 7: Has HTML content
    has_html = False
    has_plain = False
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type == "text/html":
            has_html = True
        if content_type == "text/plain":
            has_plain = True
    
    results["checks"]["has_html"] = has_html
    results["checks"]["has_plain_text"] = has_plain
    
    if not has_html:
        results["warnings"].append("No HTML content found")
    if not has_plain:
        results["warnings"].append("No plain text content found")
    
    # Check 8: Get body content to check for off-track table
    body_content = ""
    for part in msg.walk():
        if part.get_content_type() in ["text/plain", "text/html"]:
            payload = part.get_payload(decode=True)
            if payload:
                try:
                    body_content += payload.decode("utf-8", errors="ignore")
                except:
                    pass
    
    # Check for Off-track or iteration summary
    has_iteration_summary = "iteration" in body_content.lower() or "summary" in body_content.lower()
    has_off_track = "off-track" in body_content.lower() or "off track" in body_content.lower()
    
    results["checks"]["has_iteration_summary"] = has_iteration_summary
    results["checks"]["has_off_track_section"] = has_off_track
    
    if not has_iteration_summary:
        results["warnings"].append("Body does not contain 'iteration summary'")
    if not has_off_track:
        results["warnings"].append("Body does not contain 'off-track' section")
    
    # Overall summary
    results["subject"] = subject
    results["to"] = to
    results["from"] = from_addr
    
    return results


def print_results(results: dict):
    """Print validation results in a readable format."""
    print("\n" + "=" * 60)
    print("📧 DAILY REPORT EMAIL VALIDATION")
    print("=" * 60)
    print(f"File: {results['file']}")
    print()
    
    print("📋 Header Checks:")
    print(f"   Subject: {results.get('subject', 'N/A')}")
    print(f"   To: {results.get('to', 'N/A')}")
    print(f"   From: {results.get('from', 'N/A')}")
    print()
    
    print("✓ Validation Checks:")
    for check, passed in results.get("checks", {}).items():
        if check == "attachments":
            print(f"   {check}: {passed}")
        else:
            status = "✅" if passed else "❌"
            print(f"   {status} {check}: {passed}")
    print()
    
    if results.get("errors"):
        print("❌ Errors:")
        for err in results["errors"]:
            print(f"   - {err}")
        print()
    
    if results.get("warnings"):
        print("⚠️ Warnings:")
        for warn in results["warnings"]:
            print(f"   - {warn}")
        print()
    
    if results["valid"]:
        print("✅ VALIDATION PASSED")
    else:
        print("❌ VALIDATION FAILED")
    
    print("=" * 60)
    return results["valid"]


def main():
    parser = argparse.ArgumentParser(description="Validate Daily Report email output")
    parser.add_argument("file", nargs="?", help="Path to .eml file to validate")
    parser.add_argument("--latest", action="store_true", help="Validate the most recent dryrun_email_*.eml")
    
    args = parser.parse_args()
    
    if args.latest or not args.file:
        try:
            eml_path = find_latest_eml()
        except FileNotFoundError as e:
            print(f"❌ {e}")
            sys.exit(1)
    else:
        eml_path = Path(args.file)
    
    results = validate_email(eml_path)
    passed = print_results(results)
    
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
