Outlining Project Setup Steps...
Quick Start — full steps to run this project from scratch

Prereqs: Install python3.12+, git, and a system package manager if needed. Ensure you have network access for pip installs.
Clone repo:
```bash
git clone <your-repo-url> /home/sejal_walkingtree/Git/pm-agent
cd /home/sejal_walkingtree/Git/pm-agent
```
Create & activate virtualenv

Create venv:
```bash
python3 -m venv .venv
```
Activate:
```bash
source .venv/bin/activate
```
Verify python:
```bash
python -V
```
Install dependencies

If project has requirements.txt:
```bash
pip install --upgrade pip
pip install -r requirements.txt
```
Otherwise install the runtime packages used here:
```bash
pip install --upgrade pip
pip install streamlit requests python-dotenv a2a-sdk asyncclick uvicorn
```
Environment variables (config.yaml)

Update config.yaml in the repo root with your settings. Key entries:
```yaml
api_keys:
  openai_api_key: "sk-..."  # Required for LLM operations

ado:
  org_url: "https://dev.azure.com/YourOrg"
  project: "YourProject"
  pat: "<personal-access-token>"

email:
  from_email: "notifications@yourdomain"
  
smtp:
  host: "smtp.gmail.com"
  port: 587
  username: "..."
  password: "..."
```
IMPORTANT: `ado.org_url` and `ado.pat` are required for Azure DevOps API calls used by the project — set them correctly in `config.yaml` before running any ADO-related utilities.
Start required services

(Optional) Start host agent if used by your app:
```bash
python -m agents.host_agent &
```
Start Streamlit (preferred via manager script so logs/ports are handled):
```bash
python3 scripts/manage_streamlit.py
```
Or run directly: streamlit run app/chat_ai.py --server.port 8501 &
Verify the app

Open: http://localhost:8501 (or the Local URL shown in logs).
Tail logs: tail -f logs/streamlit.log
If you see import errors (e.g., ModuleNotFoundError: No module named 'google.adk'), install that package in the venv:
```bash
pip install google-adk
```
(or install the specific missing package referenced in tracebacks).
Run utilities manually
