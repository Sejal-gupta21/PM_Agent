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
    
    def fetch_work_items_by_iteration(
        self, 
        iteration_path: str, 
        work_item_types: Optional[List[str]] = None,
        area_paths: Optional[List[str]] = None,
        filter_current_month: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Fetch work items for a specific iteration.
        
        Args:
            iteration_path: Iteration path (e.g., 'Sprint 1', '@CurrentIteration')
            work_item_types: List of work item types to fetch (default: User Story, Bug, Task)
            area_paths: Optional list of area paths to filter (NEW - for UI form flow)
            filter_current_month: If True, filter by Estimated Billing Date in current month
            
        Returns:
            List of work items with effort fields
        """
        if not work_item_types:
            work_item_types = ["User Story", "Bug", "Task"]
        
        types_clause = ", ".join([f"'{t}'" for t in work_item_types])
        
        # NEW: Add area path filter if specified
        area_clause = ""
        if area_paths:
            area_conditions = " OR ".join([f"[System.AreaPath] UNDER '{area}'" for area in area_paths])
            area_clause = f" AND ({area_conditions})"
        
        wiql_query = f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo],
               [Microsoft.VSTS.Scheduling.OriginalEstimate],
               [Microsoft.VSTS.Scheduling.CompletedWork],
               [Microsoft.VSTS.Scheduling.RemainingWork],
               [System.IterationPath], [System.AreaPath], [System.WorkItemType]
        FROM WorkItems
        WHERE [System.TeamProject] = '{self.project}'
          AND [System.IterationPath] = '{iteration_path}'
          AND [System.WorkItemType] IN ({types_clause}){area_clause}
        ORDER BY [System.Id] DESC
        """
        
        logger.info(f"Executing WIQL for iteration '{iteration_path}' in project '{self.project}'")
        if area_paths:
            logger.info(f"Filtering by area paths: {area_paths}")
        if filter_current_month:
            logger.info(f"Month filtering enabled: will filter by Estimated Billing Date in current month")
        logger.debug(f"WIQL Query: {wiql_query}")
        
        try:
            url = f"{self.org_url}/{self.project}/_apis/wit/wiql?api-version=7.1"
            response = requests.post(url, json={"query": wiql_query}, headers=self.headers, timeout=30)
            
            if response.status_code != 200:
                logger.error(f"WIQL query failed: HTTP {response.status_code}")
                return []
            
            wiql_result = response.json()
            work_item_refs = wiql_result.get('workItems', [])
            
            if not work_item_refs:
                logger.info(f"No work items found for iteration {iteration_path}")
                return []
            
            # Fetch full work item details
            ids = [str(wi['id']) for wi in work_item_refs]
            work_items = self._fetch_work_items_by_ids(ids)

            # If requested, log a small sample of field keys / estimated date values
            if filter_current_month and work_items:
                try:
                    for i, wi in enumerate(work_items[:5]):
                        fields = wi.get('fields', {}) or {}
                        logger.debug(f"Sample work item {i} keys: {list(fields.keys())[:20]}")
                        sample_date = fields.get('Custom.EstimatedBillingDate') or fields.get('EstimatedBillingDate') or fields.get('Estimated Billing Date')
                        logger.debug(f"Sample work item {i} EstimatedBillingDate raw: {sample_date}")
                except Exception:
                    logger.exception("Error logging sample work item fields for month filtering")

            # Apply month filtering if requested
            if filter_current_month and work_items:
                work_items = self._filter_by_current_month(work_items)
                logger.info(f"After month filtering: {len(work_items)} work items remain")

            return work_items
            
        except Exception as e:
            logger.exception(f"Error fetching work items: {e}")
            return []
    
    def fetch_completed_work_items_current_month(
        self,
        area_paths: Optional[List[str]] = None,
        work_item_types: Optional[List[str]] = None,
        month: Optional[int] = None,
        year: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch ONLY Closed/Completed work items from the current month.
        This is the NEW billing deviation logic:
        - Only Completed/Closed state work items
        - Only items closed/completed in the current month (by StateChangeDate)
        - Filtered by area paths if provided
        
        Args:
            area_paths: Optional list of area paths to filter
            work_item_types: List of work item types to fetch (default: User Story, Bug, Task, Dev Bug)
            month: Optional month (1-12). If None, uses current month
            year: Optional year (e.g., 2025). If None, uses current year
            
        Returns:
            List of completed work items from current month with effort fields
        """
        if not work_item_types:
            work_item_types = ["User Story", "Bug", "Task", "Dev Bug"]
        
        types_clause = ", ".join([f"'{t}'" for t in work_item_types])
        
        # Calculate month date range
        # If month/year not specified, use current month UP TO TODAY
        # If month/year specified, use the ENTIRE month (1st to last day)
        now = datetime.now()
        
        if month is None or year is None:
            # Default: current month UP TO TODAY (not including today)
            # This matches the user's manual query: Closed Date >= @StartOfMonth AND < @Today
            # Example: if today is Jan 7, we want Jan 1-6 (NOT including Jan 7)
            first_day_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # Use TODAY as the upper bound (< today means up to yesterday)
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date_dt = today
            use_less_than = True  # < today (excludes today)
        else:
            # User-specified month/year: use ENTIRE month
            # Use start of NEXT month as upper bound for inclusive range
            first_day_of_month = datetime(year, month, 1, 0, 0, 0, 0)
            # Calculate start of NEXT month
            if month == 12:
                start_of_next_month = datetime(year + 1, 1, 1, 0, 0, 0, 0)
            else:
                start_of_next_month = datetime(year, month + 1, 1, 0, 0, 0, 0)
            end_date_dt = start_of_next_month
            use_less_than = True  # < start of next month (includes all of current month)
        
        # Format dates for WIQL
        start_date = first_day_of_month.strftime("%Y-%m-%d")
        end_date = end_date_dt.strftime("%Y-%m-%d")
        
        # Build area path filter
        # WIQL doesn't support LIKE, so we need a different approach for short names
        # Strategy: If short name (no backslash), fetch all and filter in Python
        # If full path (has backslash), use exact match in WIQL
        area_clause = ""
        has_short_names = False
        wiql_area_paths = []
        python_filter_patterns = []
        
        if area_paths:
            for area in area_paths:
                if '\\' in area:
                    # Full path - can use in WIQL
                    wiql_area_paths.append(area)
                else:
                    # Short name - will need Python filtering
                    has_short_names = True
                    python_filter_patterns.append(area)
            
            # If we have full paths, add them to WIQL
            if wiql_area_paths:
                area_conditions = " OR ".join([f"[System.AreaPath] = '{area}'" for area in wiql_area_paths])
                if has_short_names:
                    # Can't mix - we'll fetch all and filter in Python
                    area_clause = ""
                    python_filter_patterns.extend(wiql_area_paths)
                else:
                    area_clause = f" AND ({area_conditions})"
        
        # WIQL: Only Closed items with Changed Date in current month
        # Use ChangedDate field to match user's manual queries
        # This filters items that were modified (including state changes to Closed) in the current month
        wiql_query = f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo],
               [Microsoft.VSTS.Scheduling.OriginalEstimate],
               [Microsoft.VSTS.Scheduling.CompletedWork],
               [Microsoft.VSTS.Scheduling.RemainingWork],
               [System.IterationPath], [System.AreaPath], [System.WorkItemType],
               [System.ChangedDate]
        FROM WorkItems
        WHERE [System.TeamProject] = '{self.project}'
          AND [System.WorkItemType] IN ({types_clause})
          AND [System.State] = 'Closed'
          AND [System.ChangedDate] >= '{start_date}'
          AND [System.ChangedDate] < '{end_date}'
          AND [Microsoft.VSTS.Scheduling.CompletedWork] > 0{area_clause}
        ORDER BY [System.Id] DESC
        """
        
        # Log the query with appropriate context
        if month is not None and year is not None:
            logger.info(f"Fetching COMPLETED work items for {first_day_of_month.strftime('%B %Y')} ({start_date} to < {end_date})")
        else:
            logger.info(f"Fetching COMPLETED work items for current month UP TO TODAY ({start_date} to < {end_date})")
        if area_paths:
            logger.info(f"Filtering by area paths (flexible matching): {area_paths}")
        logger.info(f"WIQL Query:\n{wiql_query}")
        
        try:
            url = f"{self.org_url}/{self.project}/_apis/wit/wiql?api-version=7.1"
            response = requests.post(url, json={"query": wiql_query}, headers=self.headers, timeout=30)
            
            if response.status_code != 200:
                logger.error(f"WIQL query failed: HTTP {response.status_code}")
                logger.error(f"Response: {response.text}")
                return []
            
            wiql_result = response.json()
            work_item_refs = wiql_result.get('workItems', [])
            
            if not work_item_refs:
                logger.info(f"No completed work items found for current month")
                return []
            
            # Fetch full work item details
            ids = [str(wi['id']) for wi in work_item_refs]
            work_items = self._fetch_work_items_by_ids(ids)
            
            # Apply Python-side filtering for short area path names
            if python_filter_patterns:
                logger.info(f"Applying Python-side area path filtering for: {python_filter_patterns}")
                filtered_items = []
                for wi in work_items:
                    area_path = wi.get('fields', {}).get('System.AreaPath', '')
                    area_path_lower = area_path.lower()
                    # Check if any pattern matches (case-insensitive, flexible matching)
                    for pattern in python_filter_patterns:
                        pattern_lower = pattern.strip().lower()
                        # Match if:
                        # 1. Area path ends with the pattern (e.g., "...\\XOPS 25")
                        # 2. Area path equals the pattern exactly
                        # 3. Pattern appears anywhere in the area path (for multi-word like "xops bugs enhancement")
                        if (area_path_lower.endswith(f'\\{pattern_lower}') or 
                            area_path_lower == pattern_lower or
                            pattern_lower in area_path_lower):
                            filtered_items.append(wi)
                            logger.debug(f"Matched work item {wi.get('id')} with area path '{area_path}' to pattern '{pattern}'")
                            break
                work_items = filtered_items
                logger.info(f"After Python filtering: {len(work_items)} work items match area paths")
            
            logger.info(f"Fetched {len(work_items)} completed work items from {first_day_of_month.strftime('%b %d')} to {end_date_dt.strftime('%b %d, %Y')}")
            return work_items
            
        except Exception as e:
            logger.exception(f"Error fetching completed work items for current month: {e}")
            return []

    def fetch_recent_work_items(self, top: int = 100, work_item_types: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Fetch recent work items without iteration filter (fallback).
        
        Args:
            top: Maximum number of work items to return
            work_item_types: List of work item types to fetch (default: User Story, Bug, Task)
            
        Returns:
            List of work items with effort fields
        """
        if not work_item_types:
            work_item_types = ["User Story", "Bug", "Task"]
        
        types_clause = ", ".join([f"'{t}'" for t in work_item_types])
        
        wiql_query = f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo],
               [Microsoft.VSTS.Scheduling.OriginalEstimate],
               [Microsoft.VSTS.Scheduling.CompletedWork],
               [Microsoft.VSTS.Scheduling.RemainingWork],
               [System.IterationPath], [System.AreaPath], [System.WorkItemType]
        FROM WorkItems
        WHERE [System.TeamProject] = '{self.project}'
          AND [System.WorkItemType] IN ({types_clause})
          AND [Microsoft.VSTS.Scheduling.CompletedWork] > 0
        ORDER BY [System.ChangedDate] DESC
        """
        
        try:
            url = f"{self.org_url}/{self.project}/_apis/wit/wiql?api-version=7.1"
            response = requests.post(url, json={"query": wiql_query}, headers=self.headers, timeout=30)
            
            if response.status_code != 200:
                logger.error(f"WIQL query failed: HTTP {response.status_code}")
                return []
            
            wiql_result = response.json()
            work_item_refs = wiql_result.get('workItems', [])
            
            if not work_item_refs:
                logger.info(f"No recent work items found")
                return []
            
            # Limit to top N
            work_item_refs = work_item_refs[:top]
            
            # Fetch full work item details
            ids = [str(wi['id']) for wi in work_item_refs]
            return self._fetch_work_items_by_ids(ids)
            
        except Exception as e:
            logger.exception(f"Error fetching recent work items: {e}")
            return []
    
    def _fetch_work_items_by_ids(self, ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch full work item details by IDs.
        
        Args:
            ids: List of work item IDs
            
        Returns:
            List of work items with all fields
        """
        if not ids:
            return []
        
        try:
            # Batch fetch work items (max 200 per request)
            batch_size = 200
            all_items = []
            
            for i in range(0, len(ids), batch_size):
                batch_ids = ids[i:i+batch_size]
                ids_param = ",".join(batch_ids)
                
                url = f"{self.org_url}/{self.project}/_apis/wit/workitems"
                params = {
                    'ids': ids_param,
                    'api-version': '7.1',
                    '$expand': 'all'
                }
                
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
                
                if response.status_code == 200:
                    result = response.json()
                    items = result.get('value', [])
                    all_items.extend(items)
                else:
                    logger.error(f"Failed to fetch work items batch: HTTP {response.status_code}")
            
            logger.info(f"Fetched {len(all_items)} work items from ADO")
            return all_items
            
        except Exception as e:
            logger.exception(f"Error fetching work item details: {e}")
            return []
    
    def extract_effort_data(self, work_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Extract and aggregate effort data from work items.
        
        Args:
            work_items: List of work items from ADO
            
        Returns:
            Dictionary with aggregated effort data by user, module, team
        """
        effort_data = {
            'by_user': {},
            'by_module': {},
            'by_area': {},
            'by_type': {},
            'total_original_estimate': 0.0,
            'total_completed_work': 0.0,
            'total_remaining_work': 0.0,
            'work_items': []
        }
        
        for wi in work_items:
            fields = wi.get('fields', {})
            
            wi_id = fields.get('System.Id')
            title = fields.get('System.Title', '')
            state = fields.get('System.State', '')
            assigned_to = self._extract_user_name(fields.get('System.AssignedTo'))
            area_path = fields.get('System.AreaPath', 'No Area')
            wi_type = fields.get('System.WorkItemType', 'Unknown')
            
            original_estimate = float(fields.get('Microsoft.VSTS.Scheduling.OriginalEstimate', 0) or 0)
            # Robustly extract completed work (ADO may present this field under different keys)
            completed_work = self._extract_completed_work(fields)
            remaining_work = float(fields.get('Microsoft.VSTS.Scheduling.RemainingWork', 0) or 0)
            
            # Aggregate totals
            effort_data['total_original_estimate'] += original_estimate
            effort_data['total_completed_work'] += completed_work
            effort_data['total_remaining_work'] += remaining_work
            
            # By user
            if assigned_to:
                if assigned_to not in effort_data['by_user']:
                    effort_data['by_user'][assigned_to] = {
                        'original_estimate': 0.0,
                        'completed_work': 0.0,
                        'remaining_work': 0.0,
                        'count': 0
                    }
                effort_data['by_user'][assigned_to]['original_estimate'] += original_estimate
                effort_data['by_user'][assigned_to]['completed_work'] += completed_work
                effort_data['by_user'][assigned_to]['remaining_work'] += remaining_work
                effort_data['by_user'][assigned_to]['count'] += 1
            
            # By area/module
            if area_path not in effort_data['by_area']:
                effort_data['by_area'][area_path] = {
                    'original_estimate': 0.0,
                    'completed_work': 0.0,
                    'remaining_work': 0.0,
                    'count': 0
                }
            effort_data['by_area'][area_path]['original_estimate'] += original_estimate
            effort_data['by_area'][area_path]['completed_work'] += completed_work
            effort_data['by_area'][area_path]['remaining_work'] += remaining_work
            effort_data['by_area'][area_path]['count'] += 1
            
            # By type
            if wi_type not in effort_data['by_type']:
                effort_data['by_type'][wi_type] = {
                    'original_estimate': 0.0,
                    'completed_work': 0.0,
                    'remaining_work': 0.0,
                    'count': 0
                }
            effort_data['by_type'][wi_type]['original_estimate'] += original_estimate
            effort_data['by_type'][wi_type]['completed_work'] += completed_work
            effort_data['by_type'][wi_type]['remaining_work'] += remaining_work
            effort_data['by_type'][wi_type]['count'] += 1
            
            # Store individual work item
            effort_data['work_items'].append({
                'id': wi_id,
                'title': title,
                'state': state,
                'assigned_to': assigned_to,
                'area_path': area_path,
                'type': wi_type,
                'original_estimate': original_estimate,
                'completed_work': completed_work,
                'remaining_work': remaining_work
            })
        
        logger.info(f"Extracted effort data for {len(work_items)} work items")
        return effort_data
    
    def _extract_user_name(self, assigned_to_field: Any) -> str:
        """Extract user name from AssignedTo field"""
        if not assigned_to_field:
            return "Unassigned"
        
        if isinstance(assigned_to_field, dict):
            return assigned_to_field.get('displayName', 'Unknown')
        
        if isinstance(assigned_to_field, str):
            # Format: "Display Name <email@domain.com>"
            if '<' in assigned_to_field:
                return assigned_to_field.split('<')[0].strip()
            return assigned_to_field
        
        return "Unknown"

    def _filter_by_current_month(self, work_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter work items by Estimated Billing Date in current month.
        
        Args:
            work_items: List of work items from ADO
            
        Returns:
            Filtered list containing only items with Estimated Billing Date in current month
        """
        from datetime import datetime
        
        current_date = datetime.now()
        current_year = current_date.year
        current_month = current_date.month
        
        filtered_items = []
        
        for wi in work_items:
            fields = wi.get('fields', {})
            
            # Check for Estimated Billing Date field (try multiple possible field names)
            estimated_billing_date = None
            field_candidates = [
                'Custom.EstimatedBillingDate',
                'EstimatedBillingDate',
                'Estimated Billing Date',
                'WEF_D04A8CA24BEB4F95AA0C6D5F8C9E3B5B_Kanban.Column'  # Sometimes custom fields use GUIDs
            ]
            
            # Try direct field lookup
            for field_name in field_candidates:
                if field_name in fields:
                    estimated_billing_date = fields.get(field_name)
                    if estimated_billing_date:
                        break
            
            # If not found, search for any field containing "billing" and "date"
            if not estimated_billing_date:
                for field_key, field_value in fields.items():
                    if isinstance(field_key, str):
                        key_lower = field_key.lower()
                        if 'billing' in key_lower and 'date' in key_lower:
                            estimated_billing_date = field_value
                            logger.debug(f"Found Estimated Billing Date using field: {field_key}")
                            break
            
            if not estimated_billing_date:
                # If no billing date, skip this work item
                logger.debug(f"Work item {fields.get('System.Id')} has no Estimated Billing Date - excluding from month filter")
                continue
            
            try:
                # Parse the date (ADO returns dates in ISO format: YYYY-MM-DDTHH:MM:SS.sssZ)
                if isinstance(estimated_billing_date, str):
                    billing_date = datetime.fromisoformat(estimated_billing_date.replace('Z', '+00:00'))
                else:
                    continue
                
                # Check if the billing date is in the current month and year
                if billing_date.year == current_year and billing_date.month == current_month:
                    filtered_items.append(wi)
                    logger.debug(f"Work item {fields.get('System.Id')} included: billing date {billing_date.date()} is in current month")
                else:
                    logger.debug(f"Work item {fields.get('System.Id')} excluded: billing date {billing_date.date()} is not in current month ({current_year}-{current_month:02d})")
                    
            except Exception as e:
                logger.warning(f"Failed to parse Estimated Billing Date for work item {fields.get('System.Id')}: {e}")
                continue
        
        logger.info(f"Month filter: {len(filtered_items)} of {len(work_items)} work items have Estimated Billing Date in {current_year}-{current_month:02d}")
        return filtered_items
    
    def _extract_completed_work(self, fields: Dict[str, Any]) -> float:
        """Robust extraction of Completed Work from ADO work item fields.

        Checks multiple possible field keys and formats, returns a float.
        """
        candidates = [
            'Microsoft.VSTS.Scheduling.CompletedWork',
            'Completed Work',
            'CompletedWork',
            'completedWork',
        ]

        for key in candidates:
            if key in fields:
                try:
                    return float(fields.get(key) or 0)
                except Exception:
                    try:
                        # If it's a string with extra text, extract numbers
                        import re

                        m = re.search(r"[0-9]+(\.[0-9]+)?", str(fields.get(key)))
                        if m:
                            return float(m.group(0))
                    except Exception:
                        pass

        # Fallback: try to find any field name that contains 'completed' and 'work'
        for k, v in fields.items():
            if isinstance(k, str) and 'completed' in k.lower() and 'work' in k.lower():
                try:
                    return float(v or 0)
                except Exception:
                    pass

        return 0.0
