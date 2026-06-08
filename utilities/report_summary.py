import csv
from collections import Counter


def build_iteration_summary(csv_path: str) -> tuple[str, str]:
    """
    Build a concise iteration summary from the iteration report CSV.

    Returns a tuple: (plain_text_summary, html_summary_snippet).

    The HTML snippet is suitable for inclusion in the email body.
    Format matches the Daily Report screenshot with metrics for:
    - Total Tasks
    - Completed
    - In Progress
    - Ready
    - Blocked
    - Utilization
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)

    total = len(rows)

    # State-based metrics
    completed = 0
    in_progress = 0
    ready = 0
    blocked = 0
    
    state_counter = Counter()
    type_counter = Counter()
    owner_counter = Counter()
    points_total = 0.0
    points_count = 0

    for r in rows:
        state = (r.get("State", "Unknown") or "Unknown").strip()
        typ = r.get("Work Item Type", r.get("Type", "Unknown"))
        owner = r.get("Assigned To", r.get("AssignedTo", "Unassigned")) or "Unassigned"
        
        # Categorize by state using centralized config
        from config import config as _cfg
        _cat = _cfg.classify_state(state)
        if _cat == "Completed":
            completed += 1
        elif _cat == "In Progress":
            in_progress += 1
        elif _cat in ("Not Started", "Unknown"):
            ready += 1
        
        # Check for blocked items
        blocked_field = r.get("Blocked", r.get("Is Blocked", "")) or ""
        if blocked_field.lower() in ("yes", "true", "1", "blocked"):
            blocked += 1
        elif "block" in state:
            blocked += 1
            
        state_counter[r.get("State", "Unknown")] += 1
        type_counter[typ] += 1
        owner_counter[owner] += 1

        # try common story points fields
        for k in ("Story Points", "Effort", "Microsoft.VSTS.Scheduling.StoryPoints"):
            v = r.get(k)
            if v:
                try:
                    points_total += float(v)
                    points_count += 1
                except Exception:
                    pass

    # Calculate utilization (completed / total)
    utilization = (completed / total * 100) if total > 0 else 0

    # Build plain-text summary
    lines = []
    lines.append("ITERATION SUMMARY")
    lines.append("")
    lines.append(f"  TOTAL TASKS:  {total}")
    lines.append(f"  COMPLETED:    {completed}")
    lines.append(f"  IN PROGRESS:  {in_progress}")
    lines.append(f"  READY:        {ready}")
    lines.append(f"  BLOCKED:      {blocked}")
    lines.append(f"  UTILIZATION:  {utilization:.0f}%")
    lines.append("")
    lines.append("By State:")
    for s, c in state_counter.most_common():
        lines.append(f" - {s}: {c}")

    lines.append("")
    lines.append("By Type:")
    for t, c in type_counter.most_common():
        lines.append(f" - {t}: {c}")

    lines.append("")
    lines.append("Top owners (by item count):")
    for o, c in owner_counter.most_common(5):
        lines.append(f" - {o}: {c}")

    if points_count:
        avg_points = points_total / points_count
        lines.append("")
        lines.append(f"Total points (from {points_count} items): {points_total}")
        lines.append(f"Average points per measured item: {avg_points:.2f}")

    plain_text = "\n".join(lines)

    # Build HTML summary matching the Daily Report screenshot format
    html_lines = []
    html_lines.append("<div style='font-family:Arial,Helvetica,sans-serif;'>")
    
    # Metrics cards in the style of the screenshot
    html_lines.append("<div style='display:flex;flex-wrap:wrap;gap:20px;margin:15px 0;'>")
    
    # Total Tasks - blue
    html_lines.append(f"""
        <div style='border-left:4px solid #3498db;padding:10px 15px;background:#f8f9fa;min-width:100px;'>
            <div style='font-size:11px;color:#666;text-transform:uppercase;'>Total Tasks</div>
            <div style='font-size:32px;font-weight:bold;color:#2c3e50;'>{total}</div>
        </div>
    """)
    
    # Completed - green
    html_lines.append(f"""
        <div style='border-left:4px solid #27ae60;padding:10px 15px;background:#f8f9fa;min-width:100px;'>
            <div style='font-size:11px;color:#666;text-transform:uppercase;'>Completed</div>
            <div style='font-size:32px;font-weight:bold;color:#27ae60;'>{completed}</div>
        </div>
    """)
    
    # In Progress - blue
    html_lines.append(f"""
        <div style='border-left:4px solid #3498db;padding:10px 15px;background:#f8f9fa;min-width:100px;'>
            <div style='font-size:11px;color:#666;text-transform:uppercase;'>In Progress</div>
            <div style='font-size:32px;font-weight:bold;color:#3498db;'>{in_progress}</div>
        </div>
    """)
    
    # Ready - yellow/orange
    html_lines.append(f"""
        <div style='border-left:4px solid #f39c12;padding:10px 15px;background:#f8f9fa;min-width:100px;'>
            <div style='font-size:11px;color:#666;text-transform:uppercase;'>Ready</div>
            <div style='font-size:32px;font-weight:bold;color:#f39c12;'>{ready}</div>
        </div>
    """)
    
    # Blocked - red
    html_lines.append(f"""
        <div style='border-left:4px solid #e74c3c;padding:10px 15px;background:#f8f9fa;min-width:100px;'>
            <div style='font-size:11px;color:#666;text-transform:uppercase;'>Blocked</div>
            <div style='font-size:32px;font-weight:bold;color:#e74c3c;'>{blocked}</div>
        </div>
    """)
    
    # Utilization - blue
    html_lines.append(f"""
        <div style='border-left:4px solid #3498db;padding:10px 15px;background:#f8f9fa;min-width:100px;'>
            <div style='font-size:11px;color:#666;text-transform:uppercase;'>Utilization</div>
            <div style='font-size:32px;font-weight:bold;color:#3498db;'>{utilization:.0f}%</div>
        </div>
    """)
    
    html_lines.append("</div>")  # close metrics container

    def small_table(title, counter, top_n=8):
        html = [f"<h4 style='margin:16px 0 6px 0;color:#2c3e50;'>{title}</h4>", 
                "<table style='border-collapse:collapse;'>"]
        for k, v in counter.most_common(top_n):
            html.append(
                f"<tr><td style='padding:4px 12px 4px 0;border-bottom:1px solid #eee;'>{k}</td>"
                f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:right;font-weight:bold;'>{v}</td></tr>"
            )
        html.append("</table>")
        return "".join(html)

    html_lines.append(small_table("By State", state_counter, top_n=10))
    html_lines.append(small_table("By Type", type_counter, top_n=10))

    html_lines.append("<h4 style='margin:16px 0 6px 0;color:#2c3e50;'>Top Owners</h4>")
    html_lines.append("<ul style='margin:0;padding-left:20px;'>")
    for o, c in owner_counter.most_common(5):
        html_lines.append(f"<li style='padding:2px 0;'>{o}: <strong>{c}</strong></li>")
    html_lines.append("</ul>")

    if points_count:
        avg_points = points_total / points_count
        html_lines.append(f"<p style='margin-top:12px;'><strong>Total points:</strong> {points_total} — <strong>Average:</strong> {avg_points:.2f}</p>")

    html_lines.append("</div>")
    html_summary = "".join(html_lines)

    return plain_text, html_summary
