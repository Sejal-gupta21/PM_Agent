# pm-agent

A production-grade Python conversational system for Azure DevOps with Streamlit UI, deterministic regex→semantic→light-LLM routing, hybrid multi-agent workflows, Langfuse tracing, and vector-powered knowledge search to automate sprint planning, capacity triaging, and evidence-backed reporting.

## Environment Setup
- Python 3.11 (the repo typically uses `.venv` at the project root).
- Install dependencies with `pip install -r requirements.txt` inside the virtualenv.
- Local development automatically loads a `.env` file when present, so add one if you prefer not to export variables manually.

## Azure DevOps Credentials
- Provide an Azure DevOps personal access token via either `ADO_PAT` or `ADO_MCP_AUTH_TOKEN` (the helper in `utilities/mcp/pat.py` checks in that order).
- Required supporting values:
	- `ADO_ORG_URL`
	- `ADO_PROJECT`
	- Optional: `ADO_TEAM`, `AREAS`, `WI_TYPES`, and `WIQL_TEXT`/`WIQL_FILE` overrides.
- The Streamlit UI, CLI scripts, and MCP utilities all share this lookup order, so exporting the token once covers every entry point.

## Running the Streamlit App
- Use `python3 scripts/manage_streamlit.py` to recycle the Streamlit server. The helper will kill old instances, restart with the project virtualenv (when available), stream logs to `logs/streamlit.log`, and open `http://localhost:8501` in your browser.

## Generating Iteration Reports
- `python3 scripts/generate_iteration_report.py` (or invoke through the Streamlit UI) fetches work items and emits CSV/HTML snapshots in `outputs/`.
- Ensure `ADO_ORG_URL` plus a PAT env var are set before running the command; pass optional flags/environment to limit by team, area paths, or custom WIQL.
- When no WIQL override is provided the tooling runs the default query that targets the FracPro-OPS project and the two XOPS iterations shared in the Streamlit UI.
