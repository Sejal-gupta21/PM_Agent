"""
Main Orchestrator for Billing Deviation System
Coordinates fetching, analysis, reporting, and email sending.
"""
import logging
import os
from typing import Dict, List, Any, Optional
from datetime import datetime

from .config_reader import BillingDeviationConfig
from .ado_fetcher import ADOEffortFetcher
from .deviation_analyzer import DeviationAnalyzer
from .report_generator import BillingDeviationReporter
from .emailer import BillingDeviationEmailer

import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utilities.langfuse_client import trace_task

logger = logging.getLogger(__name__)


class BillingDeviationOrchestrator:
    """Main orchestrator for billing deviation tracking system"""
    
    def __init__(self):
        """Initialize orchestrator with all components"""
        self.config = BillingDeviationConfig()
        self.fetcher = ADOEffortFetcher()
        self.analyzer = DeviationAnalyzer(critical_threshold=20.0)
        self.reporter = BillingDeviationReporter()
        self.emailer = BillingDeviationEmailer(config=self.config)
        
        logger.info("Billing deviation orchestrator initialized")
    
    # Scheduler-specific target hours (hardcoded per requirements)
    SCHEDULER_TARGET_HOURS = 4000
    
    @trace_task("billing_deviation_report", metadata={"source": "pm_agent"})
    def generate_billing_deviation_report(
        self,
        iteration_path: str = "@CurrentIteration",
        billing_targets: Optional[Dict[str, float]] = None,
        recipient_email: Optional[str] = None,
        area_paths: Optional[List[str]] = None,
        user_target_hours: Optional[float] = None,
        filter_current_month: bool = False,
        scheduler_mode: bool = False,
        month: Optional[int] = None,
        year: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Generate complete billing deviation report.
        
        Args:
            iteration_path: ADO iteration path (default: @CurrentIteration)
            billing_targets: Dictionary of module->hours targets. If None, uses config.
            recipient_email: Email to send report to (validated against config.yaml)
            area_paths: Optional list of area paths to filter (NEW - for UI form flow)
            user_target_hours: Optional target hours from user input (NEW - for UI form flow)
            filter_current_month: If True, only include work items with Estimated Billing Date in current month
            scheduler_mode: If True, uses scheduler-specific logic:
                           - Target Hours = 4000 (hardcoded)
                           - Billing period = current month
                           - Only Completed/Closed work items
            month: Optional month (1-12) for billing report. If None, uses current month
            year: Optional year for billing report. If None, uses current year
            
        Returns:
            Dictionary with report results, summary, and send status
        """
        logger.info(f"Starting billing deviation report for iteration: {iteration_path}")
        if area_paths:
            logger.info(f"Filtering by area paths: {area_paths}")
        if user_target_hours:
            logger.info(f"Using user-provided target hours: {user_target_hours}")
        if filter_current_month:
            logger.info(f"Month filtering enabled: only work items with Estimated Billing Date in current month")
        if scheduler_mode:
            logger.info(f"SCHEDULER MODE: Using hardcoded target hours = {self.SCHEDULER_TARGET_HOURS}, current month, completed items only")
        
        # CRITICAL: Scheduler mode MUST use current month (ignore user selection)
        if scheduler_mode:
            month = None
            year = None
            logger.info(f"Scheduler mode: forcing month/year to None (current month)")
        
        try:
            # Step 1: Fetch work items and effort data from ADO
            # BILLING DEVIATION REQUIREMENT: ALWAYS use Completed/Closed items from current month UP TO TODAY
            # - Work items must be in Closed/Completed state
            # - Work items must be closed from start of month up to today (by StateChangeDate)
            # - Only Completed Work field is used for actual hours
            # This applies to BOTH scheduler and user queries
            if month is not None and year is not None:
                logger.info(f"Fetching ONLY Completed/Closed work items for {month}/{year}")
            else:
                logger.info(f"Fetching ONLY Completed/Closed work items from current month up to today for billing deviation")
            work_items = self.fetcher.fetch_completed_work_items_current_month(
                area_paths=area_paths,
                month=month,
                year=year
            )
            
            if not work_items:
                logger.warning(f"No completed work items found for current month up to today")
                return {
                    'success': False,
                    'message': f'No completed work items found for current month up to today.',
                    'summary': f'❌ No completed work items found.\n\n**Billing Period:** Current Month up to Today ({datetime.now().strftime("%B %d, %Y")})\n**Work Item Filter:** Closed/Completed state only\n\n**Possible reasons:**\n1. No work items have been closed/completed this month\n2. Area path filter excludes all completed items\n3. No work has been logged in Completed Work field\n\n**Suggestions:**\n- Verify work items are in Closed/Completed state\n- Check that Area Path filter is correct\n- Ensure Completed Work field is populated'
                }
            
            logger.info(f"Fetched {len(work_items)} work items")

            # Step 2: Extract and aggregate effort data
            logger.info("Extracting effort data...")
            effort_data = self.fetcher.extract_effort_data(work_items)
            
            # Step 3: Get billing targets
            # Priority order:
            # 1. SCHEDULER MODE: Use hardcoded SCHEDULER_TARGET_HOURS = 4000
            # 2. USER MODE: Use user_target_hours if provided
            # 3. FALLBACK: Use config or actual data
            
            if scheduler_mode:
                # SCHEDULER MODE: Use hardcoded target hours = 4000
                logger.info(f"SCHEDULER MODE: Using hardcoded target hours = {self.SCHEDULER_TARGET_HOURS}")
                user_target_hours = self.SCHEDULER_TARGET_HOURS
                # For scheduler mode, distribute target across all areas
                available_areas = list(effort_data.get('by_area', {}).keys())
                if available_areas:
                    per_area_target = self.SCHEDULER_TARGET_HOURS / len(available_areas)
                    billing_targets = {area: per_area_target for area in available_areas}
                else:
                    billing_targets = {'Total': self.SCHEDULER_TARGET_HOURS}
            elif user_target_hours is not None and user_target_hours > 0:
                # USER MODE: Use user-provided target hours
                logger.info(f"Using user-provided target hours: {user_target_hours}")
                # For user-provided flow, align user area inputs with actual fetched areas
                if area_paths:
                    # Find actual area keys from effort_data that match the user-provided area paths
                    available_areas = list(effort_data.get('by_area', {}).keys())
                    matched_areas = []
                    unmatched = []

                    # First, prefer exact matches (case-insensitive)
                    lower_to_actual = {a.strip().lower(): a for a in available_areas}
                    for ua in area_paths:
                        ua_norm = ua.strip().lower()
                        if not ua_norm:
                            continue
                        if ua_norm in lower_to_actual:
                            actual = lower_to_actual[ua_norm]
                            if actual not in matched_areas:
                                matched_areas.append(actual)
                        else:
                            unmatched.append(ua)

                    # For any unmatched user entries, fall back to fuzzy matching to be forgiving
                    if unmatched:
                        for ua in unmatched:
                            ua_norm = ua.strip().lower()
                            if not ua_norm:
                                continue
                            for a in available_areas:
                                a_norm = a.strip().lower()
                                if ua_norm and (ua_norm in a_norm or a_norm in ua_norm or a_norm.startswith(ua_norm) or ua_norm.startswith(a_norm)):
                                    if a not in matched_areas:
                                        matched_areas.append(a)

                    if matched_areas:
                        per_area_target = user_target_hours / len(matched_areas)
                        billing_targets = {area: per_area_target for area in matched_areas}
                        logger.info(f"Mapped user areas {area_paths} to actual areas {matched_areas} and distributed targets {per_area_target} hrs each")
                    else:
                        # No matches found in fetched data; fall back to using user-supplied area keys
                        per_area_target = user_target_hours / len(area_paths)
                        billing_targets = {area: per_area_target for area in area_paths}
                        logger.info(f"No matching actual areas found for {area_paths}; using user-supplied keys for targets")
                else:
                    # Single default target
                    billing_targets = {'Total': user_target_hours}
            elif billing_targets is None:
                # Original logic: Try to get from config, or use defaults based on actual data
                billing_targets = self.config.get_billing_targets()
                
                if not billing_targets:
                    # Generate default targets based on actual areas (assume 100% of actual as target)
                    logger.info("No billing targets configured - using actual effort as baseline")
                    billing_targets = {}
                    for area, data in effort_data.get('by_area', {}).items():
                        billing_targets[area] = data.get('completed_work', 0) * 1.0
            
            logger.info(f"Using billing targets for {len(billing_targets)} modules")
            
            # Step 4: Analyze deviations
            logger.info("Analyzing deviations...")

            # If user provided target hours, create a user_flow module analysis (only actuals)
            user_flow = False
            if user_target_hours is not None and user_target_hours > 0:
                user_flow = True
                module_analysis = {}
                # If the user provided area paths, try to map to actual areas and restrict analysis to those
                selected_areas = []
                if area_paths:
                    available_areas = list(effort_data.get('by_area', {}).keys())
                    matched = []
                    unmatched = []

                    # Prefer exact matches first
                    lower_to_actual = {a.strip().lower(): a for a in available_areas}
                    for ua in area_paths:
                        ua_norm = ua.strip().lower()
                        if not ua_norm:
                            continue
                        if ua_norm in lower_to_actual:
                            actual = lower_to_actual[ua_norm]
                            if actual not in matched:
                                matched.append(actual)
                        else:
                            unmatched.append(ua)

                    # Fuzzy-match remaining
                    if unmatched:
                        for ua in unmatched:
                            ua_norm = ua.strip().lower()
                            if not ua_norm:
                                continue
                            for a in available_areas:
                                a_norm = a.strip().lower()
                                if ua_norm and (ua_norm in a_norm or a_norm in ua_norm or a_norm.startswith(ua_norm) or ua_norm.startswith(a_norm)):
                                    if a not in matched:
                                        matched.append(a)

                    selected_areas = matched

                    # If no matches, fall back to using the raw area_paths as keys
                    if not selected_areas:
                        selected_areas = [a for a in area_paths]
                else:
                    # No area provided by user - include all areas
                    selected_areas = list(effort_data.get('by_area', {}).keys())

                # Build module analysis only for selected areas
                for area in selected_areas:
                    data = effort_data.get('by_area', {}).get(area, {})
                    actual = data.get('completed_work', 0.0)
                    module_analysis[area] = {
                        'actual': actual,
                        'target': billing_targets.get(area, 0.0) if billing_targets else 0.0,
                        'deviation_abs': 0.0,
                        'deviation_pct': 0.0,
                        'status': 'N/A',
                        'work_item_count': data.get('count', 0),
                        'remaining_work': data.get('remaining_work', 0.0)
                    }
            else:
                # Module analysis using existing analyzer (preserves original behavior)
                module_analysis = self.analyzer.analyze_by_module(effort_data, billing_targets)

            # User analysis
            user_analysis = self.analyzer.analyze_by_user(effort_data)

            # Worst offenders
            worst_offenders = self.analyzer.identify_worst_offenders(module_analysis, top_n=5)

            # Risks and recommendations
            risks_recommendations = self.analyzer.generate_risks_and_recommendations(module_analysis)
            
            # Calculate total deviation
            # NEW: If user provided target hours, use that; otherwise use original logic
            if user_target_hours is not None and user_target_hours > 0:
                total_target = user_target_hours
            else:
                total_target = sum(billing_targets.values())

                # If a default target hours is configured in config.yaml, use it
                try:
                    default_target = float(self.config.get_default_target_hours() or 0)
                except Exception:
                    default_target = 0

                if default_target and default_target > 0:
                    logger.info(f"Using configured default target hours from config: {default_target}")
                    total_target = default_target

            # Ensure total_actual is consistent with per-module analysis (sum of actuals)
            try:
                total_actual = sum([m.get('actual', 0) for m in module_analysis.values()])
            except Exception:
                total_actual = effort_data.get('total_completed_work', 0)

            # Fallback: if module sum is zero but effort_data has value, prefer effort_data
            if not total_actual:
                total_actual = effort_data.get('total_completed_work', 0)

            # Compute overall analysis
            if user_flow:
                # For user-driven flow, compute deviation as (target - actual) for the selected areas
                deviation = total_target - total_actual
                deviation_pct = (deviation / total_target * 100.0) if total_target else 0.0
                # Status: if actual > target => Ahead (overrun), if actual < target => Behind (under-delivered)
                if deviation == 0:
                    status = 'On Track'
                    is_critical = False
                elif total_actual > total_target:
                    status = 'Ahead'
                    is_critical = abs(deviation_pct) > self.analyzer.critical_threshold
                else:
                    status = 'Behind'
                    is_critical = abs(deviation_pct) > self.analyzer.critical_threshold

                total_analysis = {
                    'target': total_target,
                    'actual': total_actual,
                    # Store signed deviation as (target - actual) so positive means remaining hours
                    'deviation_abs': deviation,
                    'deviation_pct': deviation_pct,
                    'status': status,
                    'is_critical': is_critical
                }
            else:
                total_analysis = self.analyzer.analyze_deviation(total_target, total_actual)
            
            # Step 5: Compile complete analysis results
            analysis_results = {
                'iteration': f"Current Month up to Today ({datetime.now().strftime('%B %d, %Y')}) - Closed/Completed Items Only",
                'timestamp': datetime.now().isoformat(),
                'total': total_analysis,
                'by_module': module_analysis,
                'billing_targets': billing_targets,
                'user_flow': user_flow,
                'by_user': user_analysis,
                'worst_offenders': worst_offenders,
                'risks_recommendations': risks_recommendations,
                'work_item_count': len(work_items)
            }
            
            # Step 6: Generate reports
            logger.info("Generating reports...")
            text_summary = self.reporter.generate_text_summary(analysis_results)
            html_report = self.reporter.generate_html_report(analysis_results)
            # Generate detailed CSV attachment with per-user capacity/effort
            try:
                csv_path = self.reporter.generate_csv_details(analysis_results)
            except Exception as e:
                logger.exception(f"Failed to generate CSV details: {e}")
                csv_path = None
            
            # Step 7: Handle email sending (if recipient provided)
            email_result = None
            if recipient_email:
                logger.info(f"Email recipient provided: {recipient_email}")
                logger.info(f"Attempting to send report to {recipient_email}...")
                extra_attachments = [csv_path] if csv_path else None
                email_result = self.emailer.validate_and_send_report(
                    recipient_email=recipient_email,
                    text_summary=text_summary,
                    html_report=html_report,
                    subject="Billing Deviation Report",
                    extra_attachments=extra_attachments,
                )
                logger.info(f"Email result: {email_result}")
            else:
                logger.info("No recipient email provided - report will be displayed only")
                email_result = {
                    'success': False,
                    'message': 'No recipient email provided',
                    'action': 'display_only'
                }
            
            # Step 8: Return complete results
            logger.info("Billing deviation report generation completed")
            
            return {
                'success': True,
                'analysis': analysis_results,
                'text_summary': text_summary,
                'html_report': html_report,
                'email_result': email_result,
                'message': 'Billing deviation report generated successfully'
            }
            
        except Exception as e:
            logger.exception(f"Error generating billing deviation report: {e}")
            return {
                'success': False,
                'message': f'Error generating report: {str(e)}',
                'error': str(e)
            }
    
    def get_allowed_recipients(self) -> List[str]:
        """Get list of allowed email recipients"""
        return self.emailer.get_allowed_recipients()


def run_billing_deviation_report(
    iteration_path: str = "@CurrentIteration", 
    recipient_email: Optional[str] = None,
    area_paths: Optional[List[str]] = None,
    user_target_hours: Optional[float] = None,
    filter_current_month: bool = False,
    scheduler_mode: bool = False,
    month: Optional[int] = None,
    year: Optional[int] = None
) -> str:
    """
    Convenience function to run billing deviation report.
    This is the main entry point called from chat_ai.py and scheduler.
    
    Args:
        iteration_path: ADO iteration path
        recipient_email: Email to send report to (validated against config.yaml)
        area_paths: Optional list of area paths to filter (NEW - for UI form flow)
        user_target_hours: Optional target hours from user input (NEW - for UI form flow)
        filter_current_month: If True, only include work items with Estimated Billing Date in current month
        scheduler_mode: If True, uses scheduler-specific logic:
                       - Target Hours = 4000 (hardcoded)
                       - Billing period = current month
                       - Only Completed/Closed work items
        month: Optional month (1-12) for billing report. If None, uses current month
        year: Optional year for billing report. If None, uses current year
        
    Returns:
        Summary text to display in chat
    """
    orchestrator = BillingDeviationOrchestrator()
    result = orchestrator.generate_billing_deviation_report(
        iteration_path=iteration_path,
        recipient_email=recipient_email,
        area_paths=area_paths,
        user_target_hours=user_target_hours,
        filter_current_month=filter_current_month,
        scheduler_mode=scheduler_mode,
        month=month,
        year=year
    )
    
    if result.get('success'):
        # Build response message
        text_summary = result.get('text_summary', '')
        email_result = result.get('email_result', {})
        
        response_parts = []
        
        # Add email status if applicable
        logger.info(f"Processing email result: {email_result}")
        
        if email_result.get('success'):
            response_parts.append(f"✅ Report sent to {recipient_email}")
        elif email_result.get('action') == 'sent':
            response_parts.append(f"✅ Report sent to {recipient_email}")
        elif email_result.get('validation_failed'):
            response_parts.append(f"⚠️ Email {recipient_email} is not in allowed recipients list")
            response_parts.append(f"Allowed: {', '.join(orchestrator.get_allowed_recipients())}")
        elif email_result.get('action') == 'display_only':
            response_parts.append("📊 Billing Deviation Report (no email sent)")
        
        # Add summary
        response_parts.append("")
        response_parts.append(text_summary)
        
        return "\n".join(response_parts)
    else:
        return f"❌ {result.get('message', 'Failed to generate billing deviation report')}"
