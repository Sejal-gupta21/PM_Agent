"""
Deviation Detection and Analysis
Compares billing targets with actual logged effort and classifies deviations.
"""
import logging
from typing import Dict, List, Any, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class DeviationStatus(Enum):
    """Classification of billing deviation status"""
    ON_TRACK = "On Track"
    UNDER_BILLING = "Under-billing"
    OVER_BILLING = "Over-billing"
    CRITICAL_UNDER = "Critical (Under)"
    CRITICAL_OVER = "Critical (Over)"


class DeviationAnalyzer:
    """Analyze billing deviations between targets and actual effort"""
    
    def __init__(self, critical_threshold: float = 20.0):
        """
        Initialize deviation analyzer.
        
        Args:
            critical_threshold: Percentage threshold for critical deviation (default: 20%)
        """
        self.critical_threshold = critical_threshold
    
    def analyze_deviation(self, target: float, actual: float) -> Dict[str, Any]:
        """
        Analyze deviation between target and actual effort.
        
        Args:
            target: Target/planned effort hours
            actual: Actual logged effort hours
            
        Returns:
            Dictionary with deviation metrics and status
        """
        if target == 0:
            # Avoid division by zero
            if actual == 0:
                return {
                    'target': target,
                    'actual': actual,
                    'deviation_abs': 0.0,
                    'deviation_pct': 0.0,
                    'status': DeviationStatus.ON_TRACK.value,
                    'is_critical': False
                }
            else:
                # No target but work was done - over-billing
                return {
                    'target': target,
                    'actual': actual,
                    'deviation_abs': actual,
                    'deviation_pct': 100.0,
                    'status': DeviationStatus.CRITICAL_OVER.value,
                    'is_critical': True
                }
        
        deviation_abs = actual - target
        deviation_pct = (deviation_abs / target) * 100.0
        
        # Classify status
        if abs(deviation_pct) <= 10.0:
            status = DeviationStatus.ON_TRACK
            is_critical = False
        elif deviation_pct > self.critical_threshold:
            status = DeviationStatus.CRITICAL_OVER
            is_critical = True
        elif deviation_pct < -self.critical_threshold:
            status = DeviationStatus.CRITICAL_UNDER
            is_critical = True
        elif deviation_pct > 0:
            status = DeviationStatus.OVER_BILLING
            is_critical = False
        else:
            status = DeviationStatus.UNDER_BILLING
            is_critical = False
        
        return {
            'target': target,
            'actual': actual,
            'deviation_abs': deviation_abs,
            'deviation_pct': deviation_pct,
            'status': status.value,
            'is_critical': is_critical
        }
    
    # NOTE: Sprint-level aggregation logic was removed to revert recent changes.
    # The analyzer focuses on per-target deviation calculations via `analyze_deviation`.
    
    def analyze_by_module(self, effort_data: Dict[str, Any], targets: Dict[str, float]) -> Dict[str, Any]:
        """
        Analyze deviations by module/area.
        
        Args:
            effort_data: Effort data from ADO fetcher
            targets: Target hours by module (e.g., {'UI': 100, 'Backend': 150})
            
        Returns:
            Dictionary with per-module deviation analysis
        """
        by_area = effort_data.get('by_area', {})
        module_analysis = {}
        
        for area, area_data in by_area.items():
            target = targets.get(area, 0.0)
            actual = area_data.get('completed_work', 0.0)
            
            module_analysis[area] = self.analyze_deviation(target, actual)
            module_analysis[area]['work_item_count'] = area_data.get('count', 0)
            module_analysis[area]['remaining_work'] = area_data.get('remaining_work', 0.0)
        
        # Check for modules in targets but not in actual data
        for module, target in targets.items():
            if module not in module_analysis:
                module_analysis[module] = self.analyze_deviation(target, 0.0)
                module_analysis[module]['work_item_count'] = 0
                module_analysis[module]['remaining_work'] = 0.0
        
        logger.info(f"Analyzed deviations for {len(module_analysis)} modules")
        return module_analysis
    
    def analyze_by_user(self, effort_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze effort by user (no targets needed, just reporting).
        
        Args:
            effort_data: Effort data from ADO fetcher
            
        Returns:
            Dictionary with per-user effort summary
        """
        by_user = effort_data.get('by_user', {})
        user_analysis = {}
        
        for user, user_data in by_user.items():
            user_analysis[user] = {
                'completed_work': user_data.get('completed_work', 0.0),
                'remaining_work': user_data.get('remaining_work', 0.0),
                'work_item_count': user_data.get('count', 0),
                'total_effort': user_data.get('completed_work', 0.0) + user_data.get('remaining_work', 0.0)
            }
        
        logger.info(f"Analyzed effort for {len(user_analysis)} users")
        return user_analysis
    
    def identify_worst_offenders(self, module_analysis: Dict[str, Any], top_n: int = 5) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Identify modules/areas with worst deviations.
        
        Args:
            module_analysis: Module analysis from analyze_by_module()
            top_n: Number of worst offenders to return
            
        Returns:
            List of (module, analysis) tuples sorted by absolute deviation
        """
        items = []
        for module, analysis in module_analysis.items():
            items.append((module, analysis))
        
        # Sort by absolute deviation (descending)
        items.sort(key=lambda x: abs(x[1].get('deviation_abs', 0)), reverse=True)
        
        worst = items[:top_n]
        logger.info(f"Identified top {len(worst)} worst offenders")
        return worst
    
    def generate_risks_and_recommendations(self, module_analysis: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate risks and recommendations based on deviation analysis.
        
        Args:
            module_analysis: Module analysis from analyze_by_module()
            
        Returns:
            Dictionary with risks and recommendations
        """
        risks = []
        recommendations = []
        
        critical_under = []
        critical_over = []
        
        for module, analysis in module_analysis.items():
            status = analysis.get('status')
            deviation_pct = analysis.get('deviation_pct', 0)
            
            if status == DeviationStatus.CRITICAL_UNDER.value:
                critical_under.append((module, deviation_pct))
            elif status == DeviationStatus.CRITICAL_OVER.value:
                critical_over.append((module, deviation_pct))
        
        # Generate risks
        if critical_under:
            risk = f"High likelihood of delivery delay due to under-utilization in {len(critical_under)} module(s): "
            risk += ", ".join([f"{m} ({p:.1f}%)" for m, p in critical_under[:3]])
            risks.append(risk)
        
        if critical_over:
            risk = f"Budget overrun detected in {len(critical_over)} module(s): "
            risk += ", ".join([f"{m} (+{p:.1f}%)" for m, p in critical_over[:3]])
            risks.append(risk)
        
        # Generate recommendations
        if critical_under:
            for module, pct in critical_under[:3]:
                recommendations.append(f"Increase resource allocation for {module} to meet billing targets")
        
        if critical_over:
            for module, pct in critical_over[:3]:
                recommendations.append(f"Review scope/billing for {module} - actual effort exceeds target")
        
        if not risks:
            risks.append("No critical deviations detected. Billing is on track.")
        
        if not recommendations:
            recommendations.append("Continue monitoring effort vs targets.")
        
        return {
            'risks': risks,
            'recommendations': recommendations,
            'critical_count': len(critical_under) + len(critical_over)
        }
