import html
from datetime import datetime


def format_html_table_for_wi(work_item_id: str, data: dict) -> str:
    """
    Create a simple HTML report for a work item containing deployments and time logs.
    """
    lines = []
    lines.append(f"<h2>Work Item {html.escape(str(work_item_id))} report</h2>")

    deployments = data.get("deployments") or {}
    if deployments:
        lines.append("<h3>Deployment Schedule</h3>")
        lines.append("<table border='1' cellpadding='4' style='border-collapse:collapse'><tr><th>Environment</th><th>ScheduledUTC</th></tr>")
        if isinstance(deployments, dict):
            for k, v in deployments.items():
                env = html.escape(str(k))
                dt = html.escape(str(v))
                lines.append(f"<tr><td>{env}</td><td>{dt}</td></tr>")
        else:
            for item in deployments:
                if isinstance(item, dict):
                    env = html.escape(str(item.get('Environment') or item.get('environment') or item.get('env') or ''))
                    dt = html.escape(str(item.get('ScheduledUTC') or item.get('scheduled') or item.get('date') or ''))
                    lines.append(f"<tr><td>{env}</td><td>{dt}</td></tr>")
        lines.append("</table><br/>")

    time_logs = data.get('timeLogs') or []
    if time_logs:
        lines.append("<h3>Time Log Entries</h3>")
        lines.append("<table border='1' cellpadding='4' style='border-collapse:collapse'><tr><th>Date (UTC)</th><th>User</th><th>Hours</th><th>Comment</th></tr>")
        for e in time_logs:
            date = html.escape(str(e.get('date') or e.get('loggedDate') or e.get('when') or e.get('timestamp') or e.get('createdAt') or ''))
            user = html.escape(str(e.get('user') or e.get('author') or e.get('displayName') or (e.get('person') and e['person'].get('displayName')) or ''))
            hours = html.escape(str(e.get('hours') or e.get('time') or e.get('spent') or e.get('completedWork') or e.get('delta') or ''))
            comment = html.escape(str(e.get('comment') or e.get('notes') or e.get('description') or e.get('message') or ''))
            lines.append(f"<tr><td>{date}</td><td>{user}</td><td>{hours}</td><td>{comment}</td></tr>")
        lines.append("</table>")

    lines.append(f"<p>Generated: {datetime.utcnow().isoformat()}Z</p>")
    return '\n'.join(lines)
