# Capacity Triaging - Multi-Source Support

## Overview

The Capacity Triaging feature now supports multiple data sources for team capacity and leave information:

1. **Azure DevOps (ADO)** - Default, fetch from ADO Sprint Capacity
2. **ADO Time Logs (NEW)** - Fetch actual attendance from work item time logs
3. **Google Sheets** - Centralized spreadsheet management
4. **CSV/Excel File** - Simple file-based approach

## Quick Start

### Option 1: Using Azure DevOps Sprint Capacity (Default)

No configuration needed! Just ensure teams maintain capacity data in ADO:

1. Go to your Sprint → **Capacity** tab in Azure DevOps
2. For each team member, set:
   - Daily capacity (hours/day)
   - Days off (leave dates)
   - Activity breakdown

Run the triaging:
```bash
python scripts/capacity_triaging.py
```

### Option 2: Using ADO Time Logs (Actual Attendance) - NEW ✨

This option analyzes actual work logged in Azure DevOps to build attendance records automatically!

**How it works:**
1. Fetches all work items in the sprint/iteration
2. Analyzes update history to track when `CompletedWork` field changed
3. Aggregates hours logged by each employee per day
4. Calculates average daily capacity and identifies days off

**Configure PM Agent:**

**Option A: Using Environment Variables**
```bash
export CAPACITY_SOURCE_TYPE="ado-timelogs"
python scripts/capacity_triaging.py
```

**Option B: Edit config.yaml**
```yaml
capacity_triaging:
  source_type: "ado-timelogs"
```

**Benefits:**
- ✅ No manual capacity data entry needed
- ✅ Based on actual work logged, not estimates
- ✅ Automatically detects days off (no work logged)
- ✅ Shows real productivity patterns per developer

**Requirements:**
- Team must log completed work on work items regularly
- Uses `Microsoft.VSTS.Scheduling.CompletedWork` field
- Requires work item read permissions in ADO

**Note:** This analyzes historical data from the current iteration. For future capacity planning, use Option 1 (Sprint Capacity).

### Option 3: Using Google Sheets

#### Setup

1. **Create Google Cloud Service Account**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project
   - Enable "Google Sheets API"
   - Create a service account
   - Download the JSON credentials file
   - Save it to: `credentials/google_sheets_creds.json`

2. **Create Your Capacity Sheet**
   - Use the template: `templates/capacity_data_template.csv`
   - Create a Google Sheet with the same format
   - Share it with the service account email (from JSON file)
   - Give "Viewer" permissions

3. **Configure PM Agent**

   **Option A: Using UI**
   ```bash
   streamlit run app/capacity_config.py
   ```
   - Select "Google Sheets"
   - Paste your sheet URL
   - Click "Save Configuration"

   **Option B: Using Environment Variables**
   ```bash
   export CAPACITY_SOURCE_TYPE="google-sheets"
   export CAPACITY_SOURCE_URL="https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
   export CAPACITY_GOOGLE_CREDS_PATH="credentials/google_sheets_creds.json"
   ```

   **Option C: Edit config.yaml**
   ```yaml
   capacity_triaging:
     source_type: "google-sheets"
     external_source:
       google_sheets_url: "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
       google_credentials_path: "credentials/google_sheets_creds.json"
   ```

4. **Run**
   ```bash
   python scripts/capacity_triaging.py
   ```

### Option 4: Using CSV/Excel File

1. **Create Your Capacity File**
   - Copy the template: `templates/capacity_data_template.csv`
   - Update with your team's capacity data
   - Save it in the project

2. **Configure PM Agent**

   **Option A: Using UI**
   ```bash
   streamlit run app/capacity_config.py
   ```
   - Select "CSV/Excel File"
   - Enter file path
   - Click "Save Configuration"

   **Option B: Using Environment Variables**
   ```bash
   export CAPACITY_SOURCE_TYPE="csv"
   export CAPACITY_SOURCE_URL="path/to/your/capacity_data.csv"
   ```

   **Option C: Edit config.yaml**
   ```yaml
   capacity_triaging:
     source_type: "csv"
     external_source:
       csv_file_path: "path/to/your/capacity_data.csv"
   ```

3. **Run**
   ```bash
   python scripts/capacity_triaging.py
   ```

## Data Format

All sources use the same format:

| Column | Description | Example | Required |
|--------|-------------|---------|----------|
| Team | Team name (must match ADO) | XOPS 25 | Yes |
| Sprint/Iteration | Sprint identifier | 25.24 | Yes |
| Team Member | Full name | Ankur Kumar | Yes |
| Email | Member's email | ankur@example.com | Yes |
| Capacity Per Day (hours) | Available hours/day | 6 | Yes |
| Activity | Work type | Development | Yes |
| Days Off Start | Leave start date | 2025-12-20 | No |
| Days Off End | Leave end date | 2025-12-21 | No |
| Notes | Optional comments | Planned Leave | No |

### Example CSV

```csv
Team,Sprint/Iteration,Team Member,Email,Capacity Per Day (hours),Activity,Days Off Start,Days Off End,Notes
XOPS 25,25.24,Ankur Kumar,kumarankur131@gmail.com,6,Development,2025-12-20,2025-12-21,Planned Leave
XOPS 25,25.24,Sejal Gupta,sejal.gupta@walkingtree.tech,6,Development,,,
XOPS 25,25.24,Yati Gautam,yati.gautam@walkingtree.tech,6,Testing,,,
```

## Testing Your Configuration

Test with a specific team:

```bash
export ADO_PROJECT="FracPro-OPS"
export ADO_TEAM="XOPS 25"
python scripts/test_capacity_triaging.py
```

This will:
1. Fetch capacity data from your configured source
2. Analyze sprint progress and risks
3. Generate an HTML report
4. Optionally send email to the dev team

## Troubleshooting

### Google Sheets Issues

**Error: "Permission denied"**
- Make sure you shared the sheet with the service account email
- Check that the service account has at least "Viewer" permissions

**Error: "Invalid credentials"**
- Verify the JSON file path in config
- Ensure the service account has "Google Sheets API" enabled

**Error: "Sheet not found"**
- Check the sheet URL is correct
- Ensure the sheet ID is properly extracted

### CSV/Excel Issues

**Error: "File not found"**
- Check the file path is correct (relative to project root)
- Use forward slashes (/) or escaped backslashes (\\\\)

**Error: "Invalid format"**
- Ensure headers match the template exactly
- Check for extra spaces in column names

### General Issues

**No capacity data found**
- Check team name matches exactly (case-sensitive)
- Verify sprint/iteration name matches current sprint
- Ensure at least one team member has data

**Want to switch back to ADO?**
```bash
export CAPACITY_SOURCE_TYPE="ado"
# or edit config.yaml and set source_type: "ado"
```

## Integration with Chatbot

The PM Agent chatbot can now ask users to select their data source:

```python
# In your chatbot logic
source = input("Select capacity data source (ado/google-sheets/csv): ")

if source == "google-sheets":
    url = input("Enter Google Sheets URL: ")
    # Set environment variables or update config
    os.environ["CAPACITY_SOURCE_TYPE"] = "google-sheets"
    os.environ["CAPACITY_SOURCE_URL"] = url
```

## Scheduling Automated Runs

Add to your scheduler (e.g., cron):

```bash
# Run twice daily at 9 AM and 2 PM
0 9,14 * * * /path/to/pm-agent/.venv/bin/python /path/to/pm-agent/scripts/capacity_triaging.py
```

## Requirements

### For Google Sheets Integration

Install additional dependencies:

```bash
pip install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
```

Or add to `requirements.txt`:
```
google-api-python-client>=2.100.0
google-auth>=2.23.0
```

## Support

For issues or questions:
1. Check logs in `logs/pm_agent.log`
2. Run with DEBUG level: `export LOG_LEVEL=DEBUG`
3. Test configuration with `python scripts/test_capacity_triaging.py`
