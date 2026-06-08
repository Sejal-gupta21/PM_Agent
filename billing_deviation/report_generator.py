"""
Report Generation for Billing Deviation
Generates summary reports in text and HTML formats.
"""
import logging
from typing import Dict, List, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class BillingDeviationReporter:
    """Generate billing deviation reports in various formats"""
    
    def generate_text_summary(self, analysis_results: Dict[str, Any]) -> str:
        """
        Generate a concise text summary of billing deviation analysis.
        
        Args:
            analysis_results: Complete analysis results from main orchestrator
            
        Returns:
            Formatted text summary (concise format for UI)
        """
        lines = []
        lines.append("Billing Deviation Report")
        lines.append("-" * 25)
        
        # Overall metrics
        total_data = analysis_results.get('total', {})
        target = total_data.get('target', 0)
        actual = total_data.get('actual', 0)
        total_dev_abs = total_data.get('deviation_abs', 0)

        # Determine status: prefer analyzer/orchestrator-provided status when available
        status_text = total_data.get('status', '')
        if not status_text:
            # Fallback determination based on deviation sign (signed deviation = actual - target)
            if total_dev_abs > 0:
                status_text = "Behind"
            elif total_dev_abs < 0:
                status_text = "Ahead"
            else:
                status_text = "On Track"
        
        lines.append(f"Total Target: {target:.0f} hrs")
        lines.append(f"Total Actual: {actual:.0f} hrs")
        lines.append(f"Deviation: {abs(total_dev_abs):.0f} hrs ({status_text})")
        lines.append("")
        
        # Module/area breakdown (top 5 only)
        module_analysis = analysis_results.get('by_module', {})
        user_flow = analysis_results.get('user_flow', False)
        if module_analysis:
            lines.append("Modules:")
            
            # Sorting: for user_flow show by actual desc, otherwise by absolute deviation
            if user_flow:
                sorted_modules = sorted(
                    module_analysis.items(),
                    key=lambda x: x[1].get('actual', 0),
                    reverse=True
                )
            else:
                sorted_modules = sorted(
                    module_analysis.items(),
                    key=lambda x: abs(x[1].get('deviation_abs', 0)),
                    reverse=True
                )
            
            # Show modules
            # For user_flow show all modules so the displayed sum matches Total Actual
            if user_flow:
                iter_modules = sorted_modules
            else:
                iter_modules = sorted_modules[:5]

            if user_flow:
                # Build integer allocations for module actuals so their sum equals rounded total
                import math
                raw_values = [(m, d.get('actual', 0.0)) for m, d in iter_modules]
                total_raw = sum(v for _, v in raw_values)
                rounded_total = round(total_raw)
                floored = {m: int(math.floor(v)) for m, v in raw_values}
                remainder_items = sorted([(m, (v - floored[m])) for m, v in raw_values], key=lambda x: x[1], reverse=True)
                diff = rounded_total - sum(floored.values())
                alloc = dict(floored)
                idx = 0
                while diff > 0 and idx < len(remainder_items):
                    alloc[remainder_items[idx][0]] += 1
                    diff -= 1
                    idx += 1

            for module, data in iter_modules:
                mod_actual = data.get('actual', 0)
                mod_dev_abs = data.get('deviation_abs', 0)
                mod_target = data.get('target', 0)

                # Shorten module name if needed
                module_short = module.split('\\')[-1] if '\\' in module else module
                module_short = module_short[:40]

                if user_flow:
                    display_val = alloc.get(module, int(round(mod_actual)))
                    lines.append(f"- {module_short}: {display_val} hrs")
                else:
                    # Determine Ahead/Behind per-module
                    if mod_dev_abs > 0:
                        mod_status = "Ahead"
                    elif mod_dev_abs < 0:
                        mod_status = "Behind"
                    else:
                        mod_status = "On Track"
                    # Show actual hours along with deviation to avoid confusion when summing
                    lines.append(f"- {module_short}: {mod_actual:.0f} hrs (Δ {mod_dev_abs:+.0f} hrs, {mod_status})")
            
            lines.append("")
        
        # Primary risk / status line
        risks_data = analysis_results.get('risks_recommendations', {})
        risks = risks_data.get('risks', [])
        # If analyzer returned only the default 'no critical deviations' message,
        # show a more informative status based on the total deviation (ahead/behind).
        default_no_crit = len(risks) == 1 and isinstance(risks[0], str) and "No critical deviations detected" in risks[0]
        if risks and not default_no_crit:
            lines.append(f"Risk: {risks[0]}")
        else:
            # Compose a status line based on total deviation amount and computed status_text
            if status_text == "Ahead":
                lines.append(f"Risk: Billing is ahead by {abs(total_dev_abs):.0f} hrs — potential budget overrun.")
            elif status_text == "Behind":
                lines.append(f"Risk: Billing is behind by {abs(total_dev_abs):.0f} hrs — potential delivery delay.")
            else:
                lines.append("Risk: No critical deviations detected. Billing is on track.")
        
        summary = "\n".join(lines)
        logger.info("Generated concise text summary report")
        return summary
    
    def generate_html_report(self, analysis_results: Dict[str, Any]) -> str:
        """
        Generate an HTML report for billing deviation analysis.
        
        Args:
            analysis_results: Complete analysis results from main orchestrator
            
        Returns:
            HTML formatted report
        """
        total_data = analysis_results.get('total', {})
        module_analysis = analysis_results.get('by_module', {})
        user_analysis = analysis_results.get('by_user', {})
        worst_offenders = analysis_results.get('worst_offenders', [])
        risks_data = analysis_results.get('risks_recommendations', {})
        
        html = []
        html.append("<!DOCTYPE html>")
        html.append("<html><head><meta charset='UTF-8'>")
        html.append("<title>Billing Deviation Report</title>")
        html.append("<style>")
        html.append("body { font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }")
        html.append(".container { max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }")
        html.append("h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }")
        html.append("h2 { color: #34495e; margin-top: 30px; border-bottom: 1px solid #bdc3c7; padding-bottom: 5px; }")
        html.append(".metric-box { display: inline-block; background: #ecf0f1; padding: 15px 25px; margin: 10px; border-radius: 5px; }")
        html.append(".metric-label { font-size: 12px; color: #7f8c8d; text-transform: uppercase; }")
        html.append(".metric-value { font-size: 24px; font-weight: bold; color: #2c3e50; }")
        html.append(".status-on-track { color: #27ae60; }")
        html.append(".status-under { color: #e67e22; }")
        html.append(".status-over { color: #e74c3c; }")
        html.append(".status-critical { color: #c0392b; font-weight: bold; }")
        html.append("table { width: 100%; border-collapse: collapse; margin: 20px 0; }")
        html.append("th { background: #34495e; color: white; padding: 12px; text-align: left; }")
        html.append("td { padding: 10px; border-bottom: 1px solid #ecf0f1; }")
        html.append("tr:hover { background: #f8f9fa; }")
        html.append(".positive { color: #e74c3c; }")
        html.append(".negative { color: #e67e22; }")
        html.append(".risk-box { background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 10px 0; }")
        html.append(".rec-box { background: #d1ecf1; border-left: 4px solid #17a2b8; padding: 15px; margin: 10px 0; }")
        html.append("</style></head><body>")
        html.append("<div class='container'>")
        
        # Header
        html.append("<h1>📊 Billing Deviation Summary Report</h1>")
        html.append(f"<p><strong>Generated:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>")
        
        # Overall metrics
        html.append("<h2>Overall Metrics</h2>")
        html.append(f"<div class='metric-box'>")
        html.append(f"<div class='metric-label'>Total Target</div>")
        html.append(f"<div class='metric-value'>{total_data.get('target', 0):.1f} hrs</div>")
        html.append(f"</div>")
        
        html.append(f"<div class='metric-box'>")
        html.append(f"<div class='metric-label'>Total Actual</div>")
        html.append(f"<div class='metric-value'>{total_data.get('actual', 0):.1f} hrs</div>")
        html.append(f"</div>")
        
        dev_abs = total_data.get('deviation_abs', 0)
        dev_pct = total_data.get('deviation_pct', 0)
        html.append(f"<div class='metric-box'>")
        html.append(f"<div class='metric-label'>Deviation</div>")
        html.append(f"<div class='metric-value'>{dev_abs:+.1f} hrs ({dev_pct:+.1f}%)</div>")
        html.append(f"</div>")
        
        status_class = self._get_status_class(total_data.get('status', ''))
        html.append(f"<div class='metric-box'>")
        html.append(f"<div class='metric-label'>Status</div>")
        html.append(f"<div class='metric-value {status_class}'>{total_data.get('status', 'Unknown')}</div>")
        html.append(f"</div>")
        
        # Module breakdown
        if module_analysis:
            html.append("<h2>Module/Area Breakdown</h2>")
            user_flow = analysis_results.get('user_flow', False)
            if user_flow:
                # For user_flow show only Module and Actual columns
                html.append("<table>")
                html.append("<tr><th>Module</th><th>Actual (hrs)</th></tr>")
                sorted_modules = sorted(
                    module_analysis.items(),
                    key=lambda x: x[1].get('actual', 0),
                    reverse=True
                )
                # Allocate integer actuals to ensure sum equals rounded total shown
                import math
                raw_values = [(m, d.get('actual', 0.0)) for m, d in sorted_modules]
                total_raw = sum(v for _, v in raw_values)
                rounded_total = round(total_raw)
                floored = {m: int(math.floor(v)) for m, v in raw_values}
                remainder_items = sorted([(m, (v - floored[m])) for m, v in raw_values], key=lambda x: x[1], reverse=True)
                diff = rounded_total - sum(floored.values())
                alloc = dict(floored)
                idx = 0
                while diff > 0 and idx < len(remainder_items):
                    alloc[remainder_items[idx][0]] += 1
                    diff -= 1
                    idx += 1

                for module, data in sorted_modules:
                    actual = alloc.get(module, int(round(data.get('actual', 0))))
                    html.append(f"<tr>")
                    html.append(f"<td>{module}</td>")
                    html.append(f"<td>{actual:.1f}</td>")
                    html.append(f"</tr>")
                html.append("</table>")
            else:
                html.append("<table>")
                html.append("<tr><th>Module</th><th>Target (hrs)</th><th>Actual (hrs)</th><th>Deviation</th><th>Status</th></tr>")
                sorted_modules = sorted(
                    module_analysis.items(),
                    key=lambda x: abs(x[1].get('deviation_abs', 0)),
                    reverse=True
                )
                for module, data in sorted_modules:
                    target = data.get('target', 0)
                    actual = data.get('actual', 0)
                    dev_abs = data.get('deviation_abs', 0)
                    dev_pct = data.get('deviation_pct', 0)
                    status = data.get('status', 'Unknown')
                    status_class = self._get_status_class(status)
                    dev_class = "positive" if dev_abs > 0 else "negative"
                    html.append(f"<tr>")
                    html.append(f"<td>{module}</td>")
                    html.append(f"<td>{target:.1f}</td>")
                    html.append(f"<td>{actual:.1f}</td>")
                    html.append(f"<td class='{dev_class}'>{dev_abs:+.1f} hrs ({dev_pct:+.1f}%)</td>")
                    html.append(f"<td class='{status_class}'>{status}</td>")
                    html.append(f"</tr>")
                html.append("</table>")
        
        # User breakdown
        if user_analysis:
            html.append("<h2>User Effort Breakdown</h2>")
            html.append("<table>")
            html.append("<tr><th>User</th><th>Completed (hrs)</th><th>Remaining (hrs)</th><th>Total (hrs)</th><th>Work Items</th></tr>")
            
            sorted_users = sorted(
                user_analysis.items(),
                key=lambda x: x[1].get('total_effort', 0),
                reverse=True
            )
            
            for user, data in sorted_users:
                completed = data.get('completed_work', 0)
                remaining = data.get('remaining_work', 0)
                total = data.get('total_effort', 0)
                count = data.get('work_item_count', 0)
                
                html.append(f"<tr>")
                html.append(f"<td>{user}</td>")
                html.append(f"<td>{completed:.1f}</td>")
                html.append(f"<td>{remaining:.1f}</td>")
                html.append(f"<td><strong>{total:.1f}</strong></td>")
                html.append(f"<td>{count}</td>")
                html.append(f"</tr>")
            
            html.append("</table>")
        
        # Risks
        risks = risks_data.get('risks', [])
        if risks:
            # If only default non-critical message is present, show a status summary
            default_no_crit = len(risks) == 1 and isinstance(risks[0], str) and "No critical deviations detected" in risks[0]
            if default_no_crit:
                if total_data.get('deviation_abs', 0) > 0:
                    msg = f"Billing is ahead by {abs(total_data.get('deviation_abs', 0)):.0f} hrs — potential budget overrun."
                elif total_data.get('deviation_abs', 0) < 0:
                    msg = f"Billing is behind by {abs(total_data.get('deviation_abs', 0)):.0f} hrs — potential delivery delay."
                else:
                    msg = "No critical deviations detected. Billing is on track."

                html.append("<h2>⚠️ Risks</h2>")
                html.append(f"<div class='risk-box'>{msg}</div>")
            else:
                html.append("<h2>⚠️ Risks</h2>")
                for risk in risks:
                    html.append(f"<div class='risk-box'>{risk}</div>")
        
        # Recommendations
        recommendations = risks_data.get('recommendations', [])
        if recommendations:
            html.append("<h2>💡 Recommendations</h2>")
            for rec in recommendations:
                html.append(f"<div class='rec-box'>{rec}</div>")
        
        html.append("</div></body></html>")
        
        report_html = "\n".join(html)
        logger.info("Generated HTML report")
        return report_html

    def generate_csv_details(self, analysis_results: Dict[str, Any]) -> str:
        """
        Generate a CSV file with per-user capacity and effort details.

        Returns the path to the generated CSV file.
        """
        import csv
        from datetime import datetime
        from pathlib import Path

        users = analysis_results.get('by_user', {})
        billing_targets = analysis_results.get('billing_targets', {}) or {}

        timestamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
        out_dir = Path('outputs')
        out_dir.mkdir(exist_ok=True)
        csv_path = out_dir / f"billing_deviation_details_{timestamp}.csv"

        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["User", "Completed Work (hrs)", "Remaining Work (hrs)", "Total Effort (hrs)", "Work Item Count"])

            for user, data in users.items():
                completed = data.get('completed_work', 0.0)
                remaining = data.get('remaining_work', 0.0)
                total = data.get('total_effort', completed + remaining)
                count = data.get('work_item_count', data.get('count', 0))

                writer.writerow([user, f"{completed:.2f}", f"{remaining:.2f}", f"{total:.2f}", count])

        logger.info(f"Generated CSV details: {csv_path}")
        return str(csv_path)
    
    def _get_status_class(self, status: str) -> str:
        """Get CSS class for status"""
        if "On Track" in status:
            return "status-on-track"
        elif "Critical" in status:
            return "status-critical"
        elif "Under" in status:
            return "status-under"
        elif "Over" in status:
            return "status-over"
        return ""
