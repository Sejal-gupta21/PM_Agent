"""
Email Sender for Billing Deviation Reports
Validates recipients against config.yaml and sends reports securely.
"""
import logging
from typing import List, Optional
from pathlib import Path
from .config_reader import BillingDeviationConfig

logger = logging.getLogger(__name__)


class BillingDeviationEmailer:
    """Send billing deviation reports via email with strict validation"""
    
    def __init__(self, config: Optional[BillingDeviationConfig] = None):
        """
        Initialize emailer with config.
        
        Args:
            config: BillingDeviationConfig instance. If None, creates new one.
        """
        self.config = config or BillingDeviationConfig()
    
    def validate_and_send_report(
        self,
        recipient_email: Optional[str],
        text_summary: str,
        html_report: Optional[str] = None,
        subject: str = "Billing Deviation Report",
        extra_attachments: Optional[list] = None,
    ) -> dict:
        """
        Validate recipient and send billing deviation report.
        
        Args:
            recipient_email: Email address to send to (must be in config.yaml)
            text_summary: Text version of the report
            html_report: HTML version of the report (optional)
            subject: Email subject
            
        Returns:
            Dictionary with success status and message
        """
        # If no recipient provided, don't send email
        if not recipient_email:
            logger.info("No recipient email provided - skipping email send")
            return {
                'success': False,
                'message': 'No recipient email provided',
                'action': 'display_only'
            }
        
        # Validate recipient against config.yaml
        if not self.config.validate_email(recipient_email):
            logger.warning(f"Email {recipient_email} not in allowed recipients list - will not send")
            return {
                'success': False,
                'message': f'Email {recipient_email} is not in the allowed recipients list (config.yaml)',
                'action': 'display_only',
                'validation_failed': True
            }
        
        # Recipient is valid - proceed with sending
        logger.info(f"Recipient {recipient_email} validated successfully - proceeding to send email")
        
        # Check if email provider is configured
        try:
            from utilities.emailer import is_email_ready
            ready, msg = is_email_ready()
            if not ready:
                logger.error(f"Email provider not configured: {msg}")
                return {
                    'success': False,
                    'message': f'Email provider not configured: {msg}. Set SENDGRID_API_KEY/FROM_EMAIL or SMTP credentials.',
                    'action': 'error'
                }
            logger.info(f"Email provider ready: {msg}")
        except Exception as e:
            logger.warning(f"Could not check email readiness: {e}")
        
        try:
            # Import emailer utility from utilities (existing shared module)
            logger.info("Importing utilities.emailer.send_report_attachment...")
            from utilities.emailer import send_report_attachment
            logger.info("Import successful")
            
            # Create temporary HTML file if provided
            attachments = []
            html_body = text_summary  # Default to text summary
            
            if html_report:
                import tempfile
                import os
                from datetime import datetime
                
                # Create temp file for HTML report
                timestamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
                temp_dir = Path("outputs")
                temp_dir.mkdir(exist_ok=True)
                
                html_file = temp_dir / f"billing_deviation_report_{timestamp}.html"
                with open(html_file, 'w', encoding='utf-8') as f:
                    f.write(html_report)
                
                attachments.append(html_file)
                logger.info(f"Created HTML report: {html_file}")
                
                # Convert text summary to simple HTML for email body
                html_body = f"<html><body><pre>{text_summary}</pre></body></html>"
            else:
                # Convert plain text to HTML
                html_body = f"<html><body><pre>{text_summary}</pre></body></html>"
            
            # Include extra attachments (e.g., CSV) if provided
            if extra_attachments:
                for a in extra_attachments:
                    try:
                        p = Path(a)
                        if p.exists():
                            attachments.append(p)
                    except Exception:
                        # ignore invalid attachment paths
                        pass

            # Send email using existing emailer utility
            logger.info(f"Calling send_report_attachment with recipient: {recipient_email}")
            logger.info(f"Subject: {subject}")
            logger.info(f"Attachments: {attachments}")
            logger.info(f"Body length: {len(html_body)} chars")

            success, message = send_report_attachment(
                to_emails=[recipient_email],  # Correct parameter name
                subject=subject,
                html_body=html_body,  # Correct parameter name
                attachments=attachments
            )
            
            logger.info(f"send_report_attachment returned: success={success}, message={message}")
            
            if success:
                logger.info(f"Successfully sent billing deviation report to {recipient_email}")
                return {
                    'success': True,
                    'message': f'Report sent to {recipient_email}',
                    'action': 'sent'
                }
            else:
                logger.error(f"Failed to send report: {message}")
                return {
                    'success': False,
                    'message': f'Failed to send email: {message}',
                    'action': 'error'
                }
                
        except Exception as e:
            logger.exception(f"Error sending billing deviation report: {e}")
            return {
                'success': False,
                'message': f'Error sending email: {str(e)}',
                'action': 'error'
            }
    
    def get_allowed_recipients(self) -> List[str]:
        """
        Get list of allowed email recipients from config.
        
        Returns:
            List of allowed email addresses
        """
        return self.config.get_report_email_recipients()
