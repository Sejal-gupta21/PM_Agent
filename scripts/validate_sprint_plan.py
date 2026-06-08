#!/usr/bin/env python3
"""
Validation script for Sprint Plan CSV.
Verifies that all tasks have both FE and BE developers assigned,
dates are within sprint boundaries, and capacity is not exceeded.
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path
from collections import defaultdict


def validate_sprint_plan(csv_path: str, sprint_start: str = None, sprint_end: str = None) -> bool:
    """
    Validate the sprint plan CSV file.
    
    Args:
        csv_path: Path to the sprint plan CSV file
        sprint_start: Sprint start date (YYYY-MM-DD) - optional, will use dates from file if not provided
        sprint_end: Sprint end date (YYYY-MM-DD) - optional
    
    Returns:
        True if validation passes, False otherwise
    """
    csv_file = Path(csv_path)
    if not csv_file.exists():
        print(f"❌ Error: File not found: {csv_path}")
        return False
    
    print(f"\n{'='*60}")
    print(f"🔍 SPRINT PLAN VALIDATION")
    print(f"{'='*60}")
    print(f"File: {csv_file.name}")
    
    errors = []
    warnings = []
    
    # Parse dates if provided
    start_date = datetime.strptime(sprint_start, "%Y-%m-%d") if sprint_start else None
    end_date = datetime.strptime(sprint_end, "%Y-%m-%d") if sprint_end else None
    
    # Read CSV
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    if not rows:
        print("❌ Error: CSV file is empty")
        return False
    
    print(f"Total rows: {len(rows)}")
    
    # Validation 1: Check that all tasks have BOTH FE and BE assigned
    print(f"\n--- Validation 1: FE/BE Assignment Check ---")
    missing_fe = []
    missing_be = []
    missing_both = []
    
    for i, row in enumerate(rows, 1):
        task_name = row.get("Task Name", f"Row {i}")
        fe = row.get("Responsible - Frontend", "").strip()
        be = row.get("Responsible - Backend", "").strip()
        
        if not fe and not be:
            missing_both.append(task_name)
        elif not fe:
            missing_fe.append(task_name)
        elif not be:
            missing_be.append(task_name)
    
    if missing_both:
        errors.append(f"Tasks missing BOTH FE and BE: {len(missing_both)}")
        for task in missing_both[:5]:  # Show first 5
            print(f"  ❌ {task}: No FE or BE assigned")
        if len(missing_both) > 5:
            print(f"  ... and {len(missing_both) - 5} more")
    
    if missing_fe:
        errors.append(f"Tasks missing FE developer: {len(missing_fe)}")
        for task in missing_fe[:5]:
            print(f"  ⚠️ {task}: No FE assigned")
        if len(missing_fe) > 5:
            print(f"  ... and {len(missing_fe) - 5} more")
    
    if missing_be:
        errors.append(f"Tasks missing BE developer: {len(missing_be)}")
        for task in missing_be[:5]:
            print(f"  ⚠️ {task}: No BE assigned")
        if len(missing_be) > 5:
            print(f"  ... and {len(missing_be) - 5} more")
    
    if not missing_both and not missing_fe and not missing_be:
        print(f"  ✅ All {len(rows)} tasks have both FE and BE developers assigned")
    
    # Validation 2: Check that evidence columns are populated
    print(f"\n--- Validation 2: Evidence Check ---")
    missing_fe_evidence = 0
    missing_be_evidence = 0
    
    for row in rows:
        fe_evidence = row.get("Evidence_FE", "").strip()
        be_evidence = row.get("Evidence_BE", "").strip()
        
        if not fe_evidence:
            missing_fe_evidence += 1
        if not be_evidence:
            missing_be_evidence += 1
    
    if missing_fe_evidence > 0:
        warnings.append(f"Tasks missing FE evidence: {missing_fe_evidence}")
        print(f"  ⚠️ {missing_fe_evidence} tasks missing FE evidence")
    if missing_be_evidence > 0:
        warnings.append(f"Tasks missing BE evidence: {missing_be_evidence}")
        print(f"  ⚠️ {missing_be_evidence} tasks missing BE evidence")
    
    if missing_fe_evidence == 0 and missing_be_evidence == 0:
        print(f"  ✅ All {len(rows)} tasks have evidence for both FE and BE")
    
    # Validation 3: Check dates are within sprint boundaries
    print(f"\n--- Validation 3: Date Boundary Check ---")
    date_errors = []
    
    # Auto-detect sprint dates from data if not provided
    if not start_date or not end_date:
        all_start_dates = []
        all_end_dates = []
        for row in rows:
            try:
                s = datetime.strptime(row.get("Start Date", ""), "%Y-%m-%d")
                e = datetime.strptime(row.get("End Date", ""), "%Y-%m-%d")
                all_start_dates.append(s)
                all_end_dates.append(e)
            except:
                pass
        
        if all_start_dates and all_end_dates:
            if not start_date:
                start_date = min(all_start_dates)
            if not end_date:
                end_date = max(all_end_dates)
            print(f"  Auto-detected sprint: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    
    if start_date and end_date:
        for row in rows:
            task_name = row.get("Task Name", "Unknown")
            try:
                task_start = datetime.strptime(row.get("Start Date", ""), "%Y-%m-%d")
                task_end = datetime.strptime(row.get("End Date", ""), "%Y-%m-%d")
                
                if task_start < start_date:
                    date_errors.append(f"{task_name}: Start date {task_start.strftime('%Y-%m-%d')} before sprint start")
                if task_end > end_date:
                    date_errors.append(f"{task_name}: End date {task_end.strftime('%Y-%m-%d')} after sprint end")
                if task_start > task_end:
                    date_errors.append(f"{task_name}: Start date after end date")
            except ValueError as e:
                date_errors.append(f"{task_name}: Invalid date format")
        
        if date_errors:
            errors.extend(date_errors[:5])
            for err in date_errors[:5]:
                print(f"  ❌ {err}")
            if len(date_errors) > 5:
                print(f"  ... and {len(date_errors) - 5} more")
        else:
            print(f"  ✅ All dates are within sprint boundaries")
    else:
        warnings.append("Could not verify date boundaries (no sprint dates)")
        print(f"  ⚠️ Skipped: No sprint dates provided or detected")
    
    # Validation 4: Calculate developer workload
    print(f"\n--- Validation 4: Developer Workload ---")
    fe_workload = defaultdict(float)
    be_workload = defaultdict(float)
    
    for row in rows:
        fe = row.get("Responsible - Frontend", "").strip()
        be = row.get("Responsible - Backend", "").strip()
        hours = float(row.get("Estimated Hours", 0) or 0)
        
        if fe:
            fe_workload[fe] += hours * 0.5  # FE gets half
        if be:
            be_workload[be] += hours * 0.5  # BE gets half
    
    # Combine workloads
    total_workload = defaultdict(float)
    for dev, hours in fe_workload.items():
        total_workload[dev] += hours
    for dev, hours in be_workload.items():
        total_workload[dev] += hours
    
    # Check for overloaded developers (assuming 80h max for 10-day sprint)
    max_hours = 80  # 10 days * 8 hours
    overloaded = [(dev, hours) for dev, hours in total_workload.items() if hours > max_hours]
    
    if overloaded:
        for dev, hours in sorted(overloaded, key=lambda x: -x[1]):
            warnings.append(f"Developer {dev} is overloaded: {hours:.0f}h (max: {max_hours}h)")
            print(f"  🔴 {dev}: {hours:.0f}h (over capacity by {hours - max_hours:.0f}h)")
    
    # Show top loaded developers
    print(f"\n  Top 5 developers by workload:")
    for dev, hours in sorted(total_workload.items(), key=lambda x: -x[1])[:5]:
        status = "🔴" if hours > max_hours else "🟢"
        print(f"    {status} {dev}: {hours:.0f}h")
    
    # Validation 5: Check unique developers (full names)
    print(f"\n--- Validation 5: Developer Name Format ---")
    all_devs = set()
    for row in rows:
        fe = row.get("Responsible - Frontend", "").strip()
        be = row.get("Responsible - Backend", "").strip()
        if fe:
            all_devs.add(fe)
        if be:
            all_devs.add(be)
    
    # Check if names are in proper format (should have space between first and last name)
    proper_names = [d for d in all_devs if " " in d and not "@" in d and not "." in d.split()[-1]]
    email_like = [d for d in all_devs if "@" in d or "." in d.replace(" ", "")]
    
    print(f"  Total unique developers: {len(all_devs)}")
    print(f"  Full name format: {len(proper_names)}")
    if email_like:
        warnings.append(f"Some developer names still in email format: {len(email_like)}")
        print(f"  ⚠️ Email-like format: {len(email_like)} (e.g., {list(email_like)[:3]})")
    else:
        print(f"  ✅ All names in proper full name format")
    
    # Summary
    print(f"\n{'='*60}")
    print(f"📋 VALIDATION SUMMARY")
    print(f"{'='*60}")
    print(f"Total tasks: {len(rows)}")
    fe_devs = set(r.get('Responsible - Frontend', '') for r in rows if r.get('Responsible - Frontend', '').strip())
    be_devs = set(r.get('Responsible - Backend', '') for r in rows if r.get('Responsible - Backend', '').strip())
    print(f"Unique FE developers: {len(fe_devs)}")
    print(f"Unique BE developers: {len(be_devs)}")
    
    if errors:
        print(f"\n❌ ERRORS ({len(errors)}):")
        for err in errors:
            print(f"   - {err}")
    
    if warnings:
        print(f"\n⚠️ WARNINGS ({len(warnings)}):")
        for warn in warnings:
            print(f"   - {warn}")
    
    if not errors and not warnings:
        print(f"\n✅ All validations passed!")
        return True
    elif not errors:
        print(f"\n✅ Validation passed with {len(warnings)} warnings")
        return True
    else:
        print(f"\n❌ Validation FAILED with {len(errors)} errors")
        return False


def main():
    parser = argparse.ArgumentParser(description="Validate Sprint Plan CSV")
    parser.add_argument("csv_file", help="Path to sprint plan CSV file")
    parser.add_argument("--start", help="Sprint start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Sprint end date (YYYY-MM-DD)")
    
    args = parser.parse_args()
    
    success = validate_sprint_plan(args.csv_file, args.start, args.end)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
