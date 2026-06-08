"""Centralized configuration management for PM Agent.

This module loads configuration from config.yaml and provides access throughout
the application. All configuration values should be accessed through this module
instead of reading from environment variables directly.
"""
import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

# Load config.yaml once at module import
_config_dict: Dict[str, Any] = {}
_config_file = Path(__file__).parent / "config.yaml"

def _load_config() -> Dict[str, Any]:
    """Load configuration from config.yaml file."""
    if _config_file.exists():
        with open(_config_file, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {}

_config_dict = _load_config()


class _Config:
    """Configuration accessor with backward compatibility."""
    
    DEFAULT_HOST = "0.0.0.0"  # Use "0.0.0.0" for K8s/container environments and "localhost" for dev
    HOST_AGENT_PORT = 8080
    MCP_AGENT_PORT = 10003
    PM_AGENT_PORT = 10005
    # POST_DESIGN_AGENT_PORT = 10006

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value by dotted key path (e.g., 'ado.org_url')."""
        keys = key.split('.')
        value = _config_dict
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value
    
    def get_dict(self) -> Dict[str, Any]:
        """Get the entire configuration dictionary."""
        return _config_dict.copy()
    
    # Azure DevOps shortcuts
    @property
    def ado_org_url(self) -> str:
        return self.get('ado.org_url', 'https://dev.azure.com/Stratagen')
    
    @property
    def ado_project(self) -> str:
        return self.get('ado.project', 'FracPro-OPS')
    
    @property
    def ado_team(self) -> str:
        return self.get('ado.team', '')
    
    @property
    def ado_pat(self) -> str:
        return self.get('ado.pat', '')
    
    @property
    def ado_iteration(self) -> str:
        return self.get('ado.iteration', '@CurrentIteration')
    
    @property
    def ado_org_name(self) -> str:
        return self.get('ado.org_name', '')
    
    @property
    def ado_mcp_auth_token(self) -> str:
        return self.get('ado.mcp_auth_token', '')
    
    # API Keys
    @property
    def openai_api_key(self) -> str:
        return self.get('api_keys.openai_api_key', '')
    
    @property
    def google_api_key(self) -> str:
        return self.get('api_keys.google_api_key', '')
    
    @property
    def sendgrid_api_key(self) -> str:
        return self.get('api_keys.sendgrid_api_key', '')
    
    @property
    def openai_embedding_model(self) -> str:
        return self.get('openai.embedding_model', 'text-embedding-3-small')
    
    # Email Configuration
    @property
    def from_email(self) -> str:
        return self.get('email.from_email', '')
    
    @property
    def default_pm_email(self) -> str:
        return self.get('email.default_pm_email', '')
    
    @property
    def pm_email(self) -> str:
        return self.get('email.pm_email', '')
    
    # SMTP Configuration
    @property
    def smtp_host(self) -> str:
        return self.get('smtp.host', 'smtp.gmail.com')
    
    @property
    def smtp_port(self) -> int:
        return int(self.get('smtp.port', 587))
    
    @property
    def smtp_username(self) -> str:
        return self.get('smtp.username', '')
    
    @property
    def smtp_password(self) -> str:
        return self.get('smtp.password', '')
    
    @property
    def smtp_from(self) -> str:
        return self.get('smtp.from_email', '')
    
    @property
    def smtp_from_email(self) -> str:
        return self.get('smtp.from_email', '')
    
    # Logging
    @property
    def log_level(self) -> str:
        return self.get('logging.log_level', 'INFO')
    
    @property
    def environment(self) -> str:
        return self.get('logging.environment', 'local')
    
    # LLM Configuration
    @property
    def llm_max_output_tokens(self) -> int:
        return int(self.get('llm.max_output_tokens', 1024))
    
    @property
    def llm_max_tool_summary(self) -> int:
        return int(self.get('llm.max_tool_summary', 8))

    @property
    def allow_google_fallback(self) -> bool:
        """Whether to allow falling back to Google Generative APIs when OpenAI is unavailable.

        Default: False
        """
        val = self.get('llm.allow_google_fallback', False)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)
    
    # PM Agent
    @property
    def pm_agent_url(self) -> str:
        return self.get('pm_agent.url', 'http://localhost:10005/task')
    
    @property
    def pm_use_fixed_skills(self) -> bool:
        val = self.get('pm_agent.use_fixed_skills', False)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)
    
    @property
    def pm_default_project(self) -> str:
        return self.get('pm_agent.default_project', 'FracPro-OPS')

    @property
    def pm_log_reasoning(self) -> bool:
        val = self.get('pm_agent.log_reasoning', False)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)

    @property
    def pm_reasoning_log_path(self) -> str:
        return self.get('pm_agent.reasoning_log_path', 'logs/planner_reasoning.log')

    @property
    def pm_min_confidence(self) -> float:
        """Minimum planner confidence threshold. Below this, ask for clarification."""
        return float(self.get('pm_agent.min_confidence', 0.6))

    @property
    def pm_mutation_confidence(self) -> float:
        """Minimum confidence for mutating tools (create/update). Requires higher threshold."""
        return float(self.get('pm_agent.mutation_confidence', 0.9))

    @property
    def pm_identity_cache_size(self) -> int:
        """LRU cache size for identity lookups."""
        return int(self.get('pm_agent.identity_cache_size', 256))

    @property
    def pm_identity_cache_ttl(self) -> int:
        """TTL in seconds for identity cache entries."""
        return int(self.get('pm_agent.identity_cache_ttl', 600))
    
    # Vector Database Configuration
    @property
    def vector_db_mode(self) -> str:
        """Vector DB mode: 'lite' (embedded Milvus) or 'server' (external Milvus)."""
        return self.get('vector_db.mode', 'lite')
    
    @property
    def vector_db_milvus_host(self) -> str:
        """Milvus server host (only used in 'server' mode)."""
        return self.get('vector_db.milvus_host', 'localhost')
    
    @property
    def vector_db_milvus_port(self) -> int:
        """Milvus server port (only used in 'server' mode)."""
        return int(self.get('vector_db.milvus_port', 19530))
    
    @property
    def vector_db_collection_name(self) -> str:
        """Milvus collection name prefix."""
        return self.get('vector_db.collection_name', 'pmagent')
    
    @property
    def vector_db_embedding_dim(self) -> int:
        """Embedding dimension (1536 for OpenAI text-embedding-3-small)."""
        return int(self.get('vector_db.embedding_dim', 1536))

    # Report Configuration
    @property
    def report_recipients(self) -> list:
        return self.get('report.recipients', [])
    
    @property
    def report_cron(self) -> str:
        return self.get('report.cron', '0 10 * * *')
    
    @property
    def report_timezone(self) -> str:
        return self.get('report.timezone', 'UTC')
    
    @property
    def report_send_attach_html(self) -> bool:
        val = self.get('report.send_attach_html', False)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)
    
    @property
    def report_email_recipients(self) -> list:
        return self.get('reportEmailRecipients', [])
    
    # Overlooked Stories
    @property
    def overlooked_enabled(self) -> bool:
        val = self.get('overlooked.enabled', True)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)
    
    @property
    def overlooked_send_to(self) -> list:
        return self.get('overlooked.send_to', [])
    
    @property
    def overlooked_new_threshold_days(self) -> int:
        return int(self.get('overlooked.new_threshold_days', 90))
    
    @property
    def overlooked_active_threshold_days(self) -> int:
        return int(self.get('overlooked.active_threshold_days', 60))
    
    # Query Configuration
    @property
    def query_areas(self) -> list:
        return self.get('query.areas', [])
    
    @property
    def query_wi_types(self) -> list:
        return self.get('query.wi_types', ['User Story', 'Bug'])
    
    @property
    def query_iteration_path(self) -> str:
        return self.get('query.iteration_path', '@CurrentIteration')
    
    @property
    def query_wiql_text(self) -> str:
        return self.get('query.wiql_text', '')
    
    @property
    def query_wiql_file(self) -> str:
        return self.get('query.wiql_file', '')
    
    # Langfuse Configuration
    @property
    def langfuse_secret_key(self) -> str:
        return self.get('langfuse.secret_key', '')
    
    @property
    def langfuse_public_key(self) -> str:
        return self.get('langfuse.public_key', '')
    
    @property
    def langfuse_base_url(self) -> str:
        return self.get('langfuse.base_url', '')
    
    # Capacity Triaging Configuration
    @property
    def capacity_source_type(self) -> str:
        return self.get('capacity_triaging.source_type', 'ado')
    
    @property
    def capacity_csv_file_path(self) -> str:
        return self.get('capacity_triaging.external_source.csv_file_path', '')
    
    @property
    def capacity_google_sheets_url(self) -> str:
        return self.get('capacity_triaging.external_source.google_sheets_url', '')
    
    @property
    def capacity_google_credentials_path(self) -> str:
        return self.get('capacity_triaging.external_source.google_credentials_path', 'credentials/google_sheets_creds.json')
    
    @property
    def capacity_deviation_threshold(self) -> float:
        return float(self.get('capacity_triaging.thresholds.capacity_deviation', 20))
    
    @property
    def sprint_progress_threshold(self) -> float:
        return float(self.get('capacity_triaging.thresholds.sprint_progress', 30))
    
    # ══════════════════════════════════════════════════════════════════════════
    # Orchestrator Light Planner Configuration
    # ══════════════════════════════════════════════════════════════════════════
    
    @property
    def light_planner_enabled(self) -> bool:
        """Whether the light LLM planner is enabled."""
        val = self.get('orchestrator.light_planner.enabled', True)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)
    
    @property
    def light_planner_model(self) -> str:
        """Model to use for light planner (smaller/cheaper model)."""
        return self.get('orchestrator.light_planner.model', 'gpt-3.5-turbo')
    
    @property
    def light_planner_max_tokens(self) -> int:
        """Maximum tokens for light planner output (kept small for routing hints)."""
        return int(self.get('orchestrator.light_planner.max_tokens', 150))
    
    @property
    def light_planner_temperature(self) -> float:
        """Temperature for light planner (0.0 for deterministic routing)."""
        return float(self.get('orchestrator.light_planner.temperature', 0.0))
    
    @property
    def light_planner_accept_threshold(self) -> float:
        """Accept hint if confidence >= this threshold."""
        return float(self.get('orchestrator.light_planner.accept_threshold', 0.80))
    
    @property
    def light_planner_escalate_threshold(self) -> float:
        """Escalate to deep planner if confidence < this threshold."""
        return float(self.get('orchestrator.light_planner.escalate_threshold', 0.55))
    
    @property
    def light_planner_full_plan_threshold(self) -> float:
        """Accept Light LLM full plan if confidence >= this threshold."""
        return float(self.get('orchestrator.light_planner.full_plan_threshold', 0.75))
    
    # ══════════════════════════════════════════════════════════════════════════
    # Multi-Tool Orchestrator Configuration
    # ══════════════════════════════════════════════════════════════════════════
    
    @property
    def orchestrator_max_parallel_tools(self) -> int:
        """Maximum number of tools to execute in parallel."""
        return int(self.get('orchestrator.max_parallel_tools', 3))
    
    @property
    def orchestrator_default_timeout(self) -> int:
        """Default timeout in seconds for tool execution."""
        return int(self.get('orchestrator.default_timeout_seconds', 60))
    
    @property
    def orchestrator_max_retries(self) -> int:
        """Maximum number of retries per tool."""
        return int(self.get('orchestrator.max_retries_per_tool', 2))
    
    @property
    def light_planner_cache_ttl(self) -> int:
        """Cache TTL in seconds for light planner results."""
        return int(self.get('orchestrator.light_planner.cache_ttl_seconds', 3600))
    
    @property
    def light_planner_use_fallback(self) -> bool:
        """Fall back to heuristic if LLM fails."""
        val = self.get('orchestrator.light_planner.use_fallback_heuristic', True)
        if isinstance(val, str):
            return val.lower() in ('1', 'true', 'yes')
        return bool(val)

    # ══════════════════════════════════════════════════════════════════════════
    # STATE CATEGORIES - Centralized state classification
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def state_categories_provider(self) -> str:
        """Current PM tool provider (ado, jira, github)."""
        return self.get('state_categories.provider', 'ado')

    @property
    def state_categories_raw(self) -> Dict[str, Any]:
        """Raw state_categories.categories dict from config.yaml."""
        return self.get('state_categories.categories', {}) or {}

    def get_state_categories(self) -> Dict[str, list]:
        """Return canonical STATE_CATEGORIES dict.

        Returns:
            {
                "Not Started": ["New", "Ready", ...],
                "In Progress": ["Active", "Design", ...],
                "Completed":   ["Closed", "Resolved", ...],
                "Blocked":     ["On Hold", "Issues Found", ...],
            }
        """
        raw = self.state_categories_raw
        if not raw:
            # Hardcoded fallback so the system never runs without categories
            # These are the EXACT 29 states from the ADO instance
            return {
                "Not Started": ["New", "Ready", "Requested", "Scheduled",
                                "In Planning", "Accepted"],
                "In Progress": ["Active", "Design", "Code Review", "Code Complete",
                                "QA", "QA Complete", "UAT", "PRE-PROD",
                                "Approved for Production", "In Progress",
                                "Awaiting Approvals"],
                "Completed":   ["Closed", "Resolved", "Completed", "UAT Complete",
                                "Released", "Removed", "Not a Bug",
                                "Requirement Bug"],
                "Blocked":     ["On Hold", "Issues Found", "Reopened", "Inactive"],
            }
        result: Dict[str, list] = {}
        for _key, bucket in raw.items():
            label = bucket.get('label', _key)
            states = bucket.get('states', [])
            result[label] = list(states)
        return result

    def classify_state(self, state: str) -> str:
        """Classify a single work-item state into its category label.

        Args:
            state: e.g. "Active", "Closed", "On Hold"

        Returns:
            Category label ("Not Started", "In Progress", "Completed",
            "Blocked") or "Unknown" if no match.
        """
        cats = self.get_state_categories()
        state_lower = state.lower().strip()
        for label, states in cats.items():
            if state_lower in [s.lower() for s in states]:
                return label
        return "Unknown"

    def get_states_for_category(self, category: str) -> list:
        """Get all states belonging to a category.

        Args:
            category: "completed", "in_progress", "not_started", "blocked"
                      (case-insensitive, underscores or spaces accepted)

        Returns:
            List of state names, or empty list if category not found.
        """
        cats = self.get_state_categories()
        # Normalise lookup key: "in_progress" -> "in progress"
        lookup = category.lower().replace('_', ' ').strip()
        for label, states in cats.items():
            if label.lower() == lookup:
                return list(states)
        return []

    def get_all_known_states(self) -> list:
        """Flat list of every state across all categories."""
        all_states: list = []
        for states in self.get_state_categories().values():
            all_states.extend(states)
        return all_states

    def build_state_summary(self, work_items: list) -> Dict[str, int]:
        """Count work items per category.

        Args:
            work_items: List of ADO work-item dicts (with 'fields' key).

        Returns:
            {"Not Started": 5, "In Progress": 12, "Completed": 8, "Blocked": 2, "Unknown": 0}
        """
        cats = self.get_state_categories()
        summary = {label: 0 for label in cats}
        summary["Unknown"] = 0
        for item in work_items:
            fields = item.get('fields', {})
            state = fields.get('System.State', '')
            category = self.classify_state(state)
            summary[category] = summary.get(category, 0) + 1
        return summary


config = _Config()

