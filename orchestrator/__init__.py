"""
Orchestrator - Request routing and agent coordination.

This module provides:
- Guardrails: Input validation and safety layer
- State Manager: Conversation state tracking
- Routing Engine: Deterministic routing to agents
- Agent discovery and dispatch
- Response synthesis coordination

Architecture (matching system diagram):
    User Request → Controller → Orchestrator
                                    ├── Guardrails (input validation)
                                    ├── State Manager (conversation tracking)
                                    ├── Routing Engine (agent selection)
                                    │
                                    ├── Route to PM Skills Agent (Domain Rules, SOP Logic, LangGraph)
                                    └── Route to PM Agent / ADO Data Agent (Azure DevOps MCP)
                    
Note: PM Skills Agent uses LangGraph for skill workflow orchestration.
PM Agent handles ADO data access via MCP connector.
"""

from .router import Router, RouteDecision, Orchestrator, get_router, get_orchestrator
from .guardrails import Guardrails, GuardrailResult, GuardrailAction, get_guardrails
from .state_manager import ConversationStateManager, SessionState, get_state_manager
from .query_resolver import QueryResolver, ResolverResult, ResolutionMethod, get_query_resolver
from .synthesis import (
    Synthesizer, 
    SynthesisStatus, 
    SynthesisResult, 
    AgentResult, 
    AgentStatus,
    synthesize_response,
    get_synthesizer,
)
from .parameter_extractor import (
    ParameterExtractor,
    WIQLBuilder,
    get_parameter_extractor,
)
from .plan_validator import (
    PlanValidator,
    ValidationResult,
    get_plan_validator,
)

__all__ = [
    # Query Resolver (NEW - intent resolution)
    "QueryResolver",
    "ResolverResult",
    "ResolutionMethod",
    "get_query_resolver",
    # Parameter Extractor (NEW - Phase 2)
    "ParameterExtractor",
    "WIQLBuilder",
    "get_parameter_extractor",
    # Plan Validator (NEW - Phase 5)
    "PlanValidator",
    "ValidationResult",
    "get_plan_validator",
    # Router (agent selection)
    "Router", 
    "RouteDecision", 
    "Orchestrator",
    "get_router",
    "get_orchestrator",
    # Guardrails
    "Guardrails",
    "GuardrailResult",
    "GuardrailAction",
    "get_guardrails",
    # State Manager
    "ConversationStateManager",
    "SessionState",
    "get_state_manager",
    # Synthesis Layer (NEW)
    "Synthesizer",
    "SynthesisStatus",
    "SynthesisResult",
    "AgentResult",
    "AgentStatus",
    "synthesize_response",
    "get_synthesizer",
]
