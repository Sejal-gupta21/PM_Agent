"""
Snapshot of billing_deviation/ado_fetcher.py as of 2026-01-07
"""
import textwrap

content = '''
"""
ADO Effort Data Fetcher
Fetches work items and effort data from Azure DevOps for billing deviation analysis.
"""
import logging
import os
import requests
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import base64

logger = logging.getLogger(__name__)


class ADOEffortFetcher:
	"""Fetch effort and work item data from Azure DevOps"""
    
	def __init__(self, org_url: Optional[str] = None, pat: Optional[str] = None, project: Optional[str] = None):
		"""
		Initialize ADO effort fetcher.
        
		Args:
			org_url: Azure DevOps organization URL
			pat: Personal Access Token
			project: Project name
		"""
		from config import config as app_config
		self.org_url = org_url or app_config.ado_org_url
		self.pat = pat or app_config.ado_pat
		self.project = project or app_config.ado_project
        
		if not self.org_url or not self.pat:
			logger.error("ADO_ORG_URL and ADO_PAT must be configured")
        
		# Setup authorization header
		auth_string = f":{self.pat}"
		auth_bytes = auth_string.encode('ascii')
		auth_b64 = base64.b64encode(auth_bytes).decode('ascii')
		self.headers = {
			'Authorization': f'Basic {auth_b64}',
			'Content-Type': 'application/json'
		}
    
	# ... (truncated for snapshot) ...
'''

with open(__file__.replace('billing_deviation__ado_fetcher.py', '../../billing_deviation/ado_fetcher.py'), 'r', encoding='utf-8') as src:
	src_content = src.read()

with open(__file__, 'w', encoding='utf-8') as dst:
	dst.write(src_content)

print('Snapshot created: billing_deviation/ado_fetcher.py')

