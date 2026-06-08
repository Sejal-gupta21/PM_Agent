#!/usr/bin/env python3
"""
Capacity Data Sources - Support for multiple capacity data sources

This module provides adapters for fetching capacity data from various sources:
- Azure DevOps (default)
- Google Sheets
- Excel/CSV files
- Manual configuration files
"""

import os
import json
import csv
from typing import Dict, Any, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class CapacityDataSource:
    """Base class for capacity data sources."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
    
    async def get_team_capacity(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Fetch team capacity data."""
        raise NotImplementedError("Subclasses must implement get_team_capacity")


class ADOCapacitySource(CapacityDataSource):
    """Fetch capacity data from Azure DevOps."""
    
    def __init__(self, config: Dict[str, Any], mcp_connector):
        super().__init__(config)
        self.mcp = mcp_connector
    
    async def get_team_capacity(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Fetch team capacity from ADO."""
        try:
            result_str = await self.mcp.call_tool("work_get_team_capacity", {
                "project": project,
                "team": team,
                "iterationId": iteration_id
            })
            
            logger.debug(f"ADO capacity response type: {type(result_str)}")
            
            # Handle plain text error messages
            if isinstance(result_str, str):
                if "no team capacity" in result_str.lower() or "not found" in result_str.lower():
                    logger.warning(f"Team {team} has no capacity data in ADO: {result_str}")
                    return {}
                try:
                    result = json.loads(result_str)
                except json.JSONDecodeError:
                    logger.error(f"Cannot parse ADO capacity response as JSON: {result_str}")
                    return {}
            else:
                result = result_str
            
            return result
            
        except Exception as e:
            logger.error(f"Error fetching ADO capacity: {e}")
            return {}


class ADOTimeLogCapacitySource(CapacityDataSource):
    """Fetch attendance records from Azure DevOps work item completed work.
    
    This source analyzes CompletedWork field across all work items in an iteration
    and work item comments to build attendance records per employee.
    
    Note: This provides aggregate data, not daily breakdown unless using a time tracking extension.
    """
    
    def __init__(self, config: Dict[str, Any], mcp_connector):
        super().__init__(config)
        self.mcp = mcp_connector
    
    async def get_team_capacity(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Fetch team capacity from completed work on work items."""
        try:
            logger.info(f"Fetching attendance from completed work for iteration {iteration_id}")
            
            # Step 1: Get all work items in the iteration with full details
            work_items = await self._get_iteration_work_items_with_details(project, team, iteration_id)
            if not work_items:
                logger.warning("No work items found in iteration")
                return {}
            
            logger.info(f"Found {len(work_items)} work items in iteration")
            
            # Step 2: Extract completed work per user
            work_summary = self._extract_completed_work_by_user(work_items)
            
            # Step 3: Build capacity structure
            team_capacity = self._build_capacity_from_completed_work(work_summary)
            
            logger.info(f"Built capacity data with {len(team_capacity.get('teamMembers', []))} members")
            return team_capacity
            
        except Exception as e:
            logger.error(f"Error fetching ADO completed work capacity: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {}
    
    async def _get_iteration_work_items_with_details(self, project: str, team: str, iteration_id: str) -> List[Dict[str, Any]]:
        """Get all work items for the iteration with full field details."""
        try:
            result_str = await self.mcp.call_tool("wit_get_work_items_for_iteration", {
                "project": project,
                "team": team,
                "iterationId": iteration_id
            })
            
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
            
            # Handle different response formats
            if isinstance(result, list):
                work_items_list = result
            elif isinstance(result, dict):
                raw_items = result.get("value", result.get("workItems", result.get("workItemRelations", [])))
                if len(raw_items) > 0 and isinstance(raw_items[0], dict) and 'target' in raw_items[0]:
                    work_items_list = [item['target'] for item in raw_items if 'target' in item]
                else:
                    work_items_list = raw_items
            else:
                work_items_list = []
            
            # Extract IDs and fetch full details if needed
            if work_items_list and 'fields' not in work_items_list[0]:
                logger.info("Fetching full work item details...")
                ids = [wi.get('id') for wi in work_items_list if 'id' in wi]
                if ids:
                    full_items_str = await self.mcp.call_tool("wit_get_work_items_batch_by_ids", {
                        "project": project,
                        "ids": ids,
                        "fields": [
                            "System.Id",
                            "System.Title",
                            "System.AssignedTo",
                            "System.State",
                            "Microsoft.VSTS.Scheduling.CompletedWork",
                            "Microsoft.VSTS.Scheduling.OriginalEstimate",
                            "Microsoft.VSTS.Scheduling.RemainingWork",
                            "System.ChangedDate"
                        ]
                    })
                    
                    full_items = json.loads(full_items_str) if isinstance(full_items_str, str) else full_items_str
                    
                    if isinstance(full_items, dict):
                        work_items_list = full_items.get("value", full_items.get("workItems", []))
                    elif isinstance(full_items, list):
                        work_items_list = full_items
            
            return work_items_list
            
        except Exception as e:
            logger.error(f"Error fetching iteration work items: {e}")
            return []
    
    def _extract_completed_work_by_user(self, work_items: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        """Extract completed work per user from work items.
        
        Returns dict of {user_name: {"completed": hours, "original": hours, "remaining": hours, "work_items": count}}
        """
        from collections import defaultdict
        
        user_work = defaultdict(lambda: {
            "completed": 0,
            "original": 0,
            "remaining": 0,
            "work_items": 0
        })
        
        for wi in work_items:
            if not isinstance(wi, dict):
                continue
            
            fields = wi.get("fields", {})
            
            # Get assigned user
            assigned_to = fields.get("System.AssignedTo")
            if not assigned_to:
                continue
            
            # Handle different formats of AssignedTo field
            if isinstance(assigned_to, dict):
                user_name = assigned_to.get("displayName") or assigned_to.get("name", "Unknown")
            elif isinstance(assigned_to, str):
                user_name = assigned_to.split("<")[0].strip() if "<" in assigned_to else assigned_to
            else:
                user_name = "Unknown"
            
            # Get work metrics
            completed = fields.get("Microsoft.VSTS.Scheduling.CompletedWork", 0) or 0
            original = fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate", 0) or 0
            remaining = fields.get("Microsoft.VSTS.Scheduling.RemainingWork", 0) or 0
            
            try:
                completed = float(completed)
                original = float(original)
                remaining = float(remaining)
            except (ValueError, TypeError):
                continue
            
            if completed > 0 or original > 0:
                user_work[user_name]["completed"] += completed
                user_work[user_name]["original"] += original
                user_work[user_name]["remaining"] += remaining
                user_work[user_name]["work_items"] += 1
                
                logger.debug(f"{user_name}: +{completed}h completed on WI {wi.get('id')}")
        
        return dict(user_work)
    
    def _build_capacity_from_completed_work(self, work_summary: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
        """Build capacity data structure from completed work summary.
        
        Note: Since we don't have daily breakdown, we estimate capacity based on total completed work.
        """
        team_members = []
        
        for user, metrics in work_summary.items():
            # Estimate average capacity per day (assuming standard 2-week sprint = 10 working days)
            # This is a rough estimate - actual capacity varies
            total_completed = metrics["completed"]
            num_work_items = metrics["work_items"]
            
            # Estimate 6-8 hours per day average, based on completed work
            # If someone completed 40 hours, that's roughly 5-6 days of work
            estimated_days = total_completed / 6.5 if total_completed > 0 else 0
            avg_capacity = 6.5  # Standard estimate
            
            team_members.append({
                "teamMember": {
                    "displayName": user
                },
                "activities": [{
                    "name": "Development",
                    "capacityPerDay": avg_capacity
                }],
                "daysOff": [],  # Cannot determine days off from this data
                "actualData": {
                    "totalCompletedHours": round(total_completed, 2),
                    "totalOriginalEstimate": round(metrics["original"], 2),
                    "totalRemainingWork": round(metrics["remaining"], 2),
                    "workItemsCount": num_work_items,
                    "estimatedWorkingDays": round(estimated_days, 1)
                }
            })
        
        return {
            "teamMembers": team_members,
            "source": "ado-completed-work",
            "note": "Capacity estimated from completed work. Daily breakdown not available without time tracking extension."
        }


class GoogleSheetsCapacitySource(CapacityDataSource):
    """Fetch capacity data from Google Sheets."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.sheet_url = config.get("sheet_url")
        self.credentials_path = config.get("credentials_path")
        self._service = None
    
    def _get_service(self):
        """Initialize Google Sheets API service."""
        if self._service:
            return self._service
        
        try:
            from googleapiclient.discovery import build
            from google.oauth2 import service_account
            
            # Load credentials
            SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
            credentials = service_account.Credentials.from_service_account_file(
                self.credentials_path, scopes=SCOPES)
            
            self._service = build('sheets', 'v4', credentials=credentials)
            return self._service
            
        except ImportError:
            logger.error("Google Sheets API not installed. Run: pip install google-api-python-client google-auth")
            return None
        except Exception as e:
            logger.error(f"Error initializing Google Sheets service: {e}")
            return None
    
    def _extract_sheet_id(self, url: str) -> Optional[str]:
        """Extract spreadsheet ID from Google Sheets URL."""
        try:
            # Handle various URL formats
            if "/d/" in url:
                sheet_id = url.split("/d/")[1].split("/")[0]
                return sheet_id
            return url  # Assume it's already just the ID
        except Exception as e:
            logger.error(f"Error extracting sheet ID from URL: {e}")
            return None
    
    async def get_team_capacity(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Fetch team capacity from Google Sheets."""
        try:
            service = self._get_service()
            if not service:
                return {}
            
            sheet_id = self._extract_sheet_id(self.sheet_url)
            if not sheet_id:
                logger.error("Invalid Google Sheets URL")
                return {}
            
            # Read data from the sheet
            range_name = 'Sheet1!A:I'  # Adjust based on your template
            result = service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=range_name
            ).execute()
            
            values = result.get('values', [])
            if not values:
                logger.warning("No data found in Google Sheet")
                return {}
            
            # Parse CSV-like data
            return self._parse_capacity_data(values, team)
            
        except Exception as e:
            logger.error(f"Error fetching Google Sheets capacity: {e}")
            return {}
    
    def _parse_capacity_data(self, rows: List[List[str]], team: str) -> Dict[str, Any]:
        """Parse capacity data from sheet rows."""
        try:
            # Expected columns: Team, Sprint/Iteration, Team Member, Email, Capacity Per Day, Activity, Days Off Start, Days Off End, Notes
            headers = rows[0] if rows else []
            team_members = []
            
            for row in rows[1:]:  # Skip header
                if len(row) < 5:
                    continue
                
                row_team = row[0] if len(row) > 0 else ""
                if row_team != team:
                    continue
                
                member_name = row[2] if len(row) > 2 else "Unknown"
                capacity_per_day = float(row[4]) if len(row) > 4 and row[4] else 0
                activity = row[5] if len(row) > 5 else "Development"
                days_off_start = row[6] if len(row) > 6 else None
                days_off_end = row[7] if len(row) > 7 else None
                
                # Build days off array
                days_off = []
                if days_off_start and days_off_end:
                    days_off.append({
                        "start": days_off_start,
                        "end": days_off_end
                    })
                
                team_members.append({
                    "teamMember": {
                        "displayName": member_name
                    },
                    "activities": [{
                        "name": activity,
                        "capacityPerDay": capacity_per_day
                    }],
                    "daysOff": days_off
                })
            
            return {
                "teamMembers": team_members
            }
            
        except Exception as e:
            logger.error(f"Error parsing capacity data: {e}")
            return {}


class CSVCapacitySource(CapacityDataSource):
    """Fetch capacity data from CSV/Excel file."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.file_path = config.get("csv_file_path") or config.get("file_path")
    
    async def get_team_capacity(self, project: str, team: str, iteration_id: str) -> Dict[str, Any]:
        """Fetch team capacity from CSV file."""
        try:
            logger.info(f"CSVCapacitySource.get_team_capacity called for team: {team}")
            logger.info(f"CSV file path: {self.file_path}")
            
            if not os.path.exists(self.file_path):
                logger.error(f"CSV file not found: {self.file_path}")
                return {}
            
            with open(self.file_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            
            logger.info(f"Loaded {len(rows)} rows from CSV")
            result = self._parse_capacity_data(rows, team)
            logger.info(f"Parsed {len(result.get('teamMembers', []))} team members for team {team}")
            return result
            
        except Exception as e:
            logger.error(f"Error reading CSV capacity data: {e}")
            return {}
    
    def _parse_capacity_data(self, rows: List[Dict[str, str]], team: str) -> Dict[str, Any]:
        """Parse capacity data from CSV rows.
        
        Supports two formats:
        1. Full format: Team, Team Member, Capacity Per Day (hours), Activity, Days Off Start, Days Off End
        2. Simple format: Name, Email, CapacityPerDay, DaysOff, Activity
        """
        try:
            team_members = []
            
            for row in rows:
                # Skip team filtering if Team column doesn't exist (simple format)
                if "Team" in row:
                    row_team = row.get("Team", "")
                    if row_team and row_team != team:
                        continue
                
                # Support both "Team Member" and "Name" columns
                member_name = row.get("Team Member") or row.get("Name", "Unknown")
                
                # Support both "Capacity Per Day (hours)" and "CapacityPerDay" columns
                capacity_str = row.get("Capacity Per Day (hours)") or row.get("CapacityPerDay", "0")
                capacity_per_day = float(capacity_str or 0)
                
                activity = row.get("Activity", "Development")
                
                # Build days off array - support both formats
                days_off = []
                days_off_start = row.get("Days Off Start")
                days_off_end = row.get("Days Off End")
                days_off_count = row.get("DaysOff")
                
                if days_off_start and days_off_end:
                    # Full format with date ranges
                    days_off.append({
                        "start": days_off_start,
                        "end": days_off_end
                    })
                elif days_off_count and int(days_off_count or 0) > 0:
                    # Simple format with count - create dummy date ranges
                    import datetime
                    today = datetime.date.today()
                    for i in range(int(days_off_count)):
                        day_off = today + datetime.timedelta(days=i+1)
                        days_off.append({
                            "start": day_off.isoformat(),
                            "end": day_off.isoformat()
                        })
                
                team_members.append({
                    "teamMember": {
                        "displayName": member_name
                    },
                    "activities": [{
                        "name": activity,
                        "capacityPerDay": capacity_per_day
                    }],
                    "daysOff": days_off
                })
            
            return {
                "teamMembers": team_members
            }
            
        except Exception as e:
            logger.error(f"Error parsing CSV capacity data: {e}")
            return {}


def create_capacity_source(source_type: str, config: Dict[str, Any], mcp_connector=None) -> Optional[CapacityDataSource]:
    """Factory function to create appropriate capacity data source."""
    source_type = source_type.lower()
    
    if source_type == "ado" or source_type == "azure-devops":
        if not mcp_connector:
            logger.error("MCP connector required for ADO source")
            return None
        return ADOCapacitySource(config, mcp_connector)
    
    elif source_type == "ado-timelogs" or source_type == "ado-attendance":
        if not mcp_connector:
            logger.error("MCP connector required for ADO time logs source")
            return None
        logger.info("Creating ADO Time Logs capacity source")
        return ADOTimeLogCapacitySource(config, mcp_connector)
    
    elif source_type == "google-sheets" or source_type == "gsheet":
        return GoogleSheetsCapacitySource(config)
    
    elif source_type == "csv" or source_type == "excel":
        return CSVCapacitySource(config)
    
    else:
        logger.error(f"Unknown capacity source type: {source_type}")
        return None
