"""
Configuration reader for billing deviation system.
Reads billing targets, sprint dates, and email recipients from config.yaml.
"""
import logging
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class BillingDeviationConfig:
    """Read and manage billing deviation configuration from config.yaml"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize config reader.
        
        Args:
            config_path: Path to config.yaml. If None, searches in parent directory.
        """
        if config_path is None:
            # Default to config.yaml in project root
            config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        
        self.config_path = Path(config_path)
        self._config = None
        self._load_config()
    
    def _load_config(self):
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)
            logger.info(f"Loaded billing deviation config from {self.config_path}")
        except FileNotFoundError:
            logger.error(f"Config file not found: {self.config_path}")
            self._config = {}
        except Exception as e:
            logger.exception(f"Failed to load config: {e}")
            self._config = {}
    
    def get_report_email_recipients(self) -> List[str]:
        """
        Get allowed email recipients from config.
        
        Returns:
            List of allowed email addresses
        """
        recipients = self._config.get('reportEmailRecipients', [])
        logger.info(f"Loaded {len(recipients)} allowed email recipients")
        return recipients
    
    def validate_email(self, email: str) -> bool:
        """
        Validate if an email address is in the allowed recipients list.
        Case-insensitive comparison.
        
        Args:
            email: Email address to validate
            
        Returns:
            True if email is allowed, False otherwise
        """
        allowed = self.get_report_email_recipients()
        email_lower = email.lower().strip()
        allowed_lower = [e.lower().strip() for e in allowed]
        
        is_valid = email_lower in allowed_lower
        
        logger.info(f"Validating email: {email}")
        logger.info(f"Allowed recipients: {allowed}")
        logger.info(f"Validation result: {is_valid}")
        
        if not is_valid:
            logger.warning(f"Email {email} is NOT in allowed recipients list: {allowed}")
        else:
            logger.info(f"Email {email} validated successfully")
        
        return is_valid
    
    def get_billing_targets(self) -> Dict[str, Any]:
        """
        Get billing targets configuration.
        
        Returns:
            Dictionary with billing targets per module/team
        """
        return self._config.get('billingTargets', {})
    
    def get_sprint_config(self) -> Dict[str, Any]:
        """
        Get sprint/billing cycle configuration.
        
        Returns:
            Dictionary with sprint dates and settings
        """
        return self._config.get('sprintConfig', {})
    
    def get_team_structure(self) -> Dict[str, Any]:
        """
        Get team structure configuration.
        
        Returns:
            Dictionary with team and module mappings
        """
        return self._config.get('teamStructure', {})

    def get_default_target_hours(self) -> float:
        """
        Read a default target hours value from config under `billing_defaults`.

        Returns:
            The default target hours as a float (0.0 if not configured).
        """
        try:
            bd = self._config.get('billing_defaults', {}) or {}
            val = bd.get('default_target_hours', 0)
            return float(val or 0)
        except Exception:
            logger.exception("Failed to read billing_defaults.default_target_hours")
            return 0.0
