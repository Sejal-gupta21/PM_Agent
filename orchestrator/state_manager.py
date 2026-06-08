"""
Conversation State Manager - Orchestrator-level conversation state.

This module provides centralized conversation state management:
1. Session tracking across turns
2. Context aggregation from multiple agents
3. Turn-based state management
4. Cross-agent context sharing

Architecture Position:
    Controller → Orchestrator → [STATE MANAGER] → Routing Engine
                                      ↓
                              PM Agent ←→ PM Skills Agent

The State Manager sits at the orchestrator level to:
- Maintain unified conversation context
- Share state between agents
- Track multi-turn conversation history
- Enable follow-up query handling
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from threading import Lock

logger = logging.getLogger("orchestrator.state_manager")


@dataclass
class TurnContext:
    """Context for a single conversation turn."""
    turn_number: int
    query: str
    timestamp: datetime
    agent_used: Optional[str] = None
    skill_used: Optional[str] = None
    tool_used: Optional[str] = None
    result_summary: Optional[str] = None
    entities_extracted: Dict[str, Any] = field(default_factory=dict)
    

@dataclass
class SessionState:
    """Complete state for a conversation session."""
    session_id: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_active: datetime = field(default_factory=datetime.utcnow)
    
    # Turn history
    turns: List[TurnContext] = field(default_factory=list)
    current_turn: int = 0
    
    # Sticky context (persists across turns)
    project: Optional[str] = None
    team: Optional[str] = None
    organization: Optional[str] = None
    iteration: Optional[str] = None
    area_path: Optional[str] = None
    
    # Resolved entities (cached for follow-ups)
    resolved_identities: Dict[str, str] = field(default_factory=dict)  # name -> canonical token
    resolved_work_items: Dict[int, Dict] = field(default_factory=dict)  # id -> summary
    
    # Agent-specific context
    pm_agent_context: Dict[str, Any] = field(default_factory=dict)
    pm_skill_agent_context: Dict[str, Any] = field(default_factory=dict)
    
    # Pending operations
    pending_clarification: Optional[Dict[str, Any]] = None
    pending_billing_deviation: Optional[Dict[str, Any]] = None  # For billing deviation follow-up
    
    def add_turn(self, query: str, **kwargs) -> TurnContext:
        """Add a new turn to the conversation."""
        self.current_turn += 1
        turn = TurnContext(
            turn_number=self.current_turn,
            query=query,
            timestamp=datetime.utcnow(),
            **kwargs
        )
        self.turns.append(turn)
        self.last_active = datetime.utcnow()
        
        # Keep last 20 turns
        if len(self.turns) > 20:
            self.turns = self.turns[-20:]
        
        return turn
    
    def get_last_turn(self) -> Optional[TurnContext]:
        """Get the most recent turn."""
        return self.turns[-1] if self.turns else None
    
    def get_context_for_agent(self, agent_name: str) -> Dict[str, Any]:
        """Get context tailored for a specific agent."""
        base_context = {
            "session_id": self.session_id,
            "turn_number": self.current_turn,
            "project": self.project,
            "team": self.team,
            "organization": self.organization,
            "iteration": self.iteration,
            "area_path": self.area_path,
        }
        
        # Add recent conversation summary
        if self.turns:
            last_turn = self.turns[-1]
            base_context["last_query"] = last_turn.query
            base_context["last_agent"] = last_turn.agent_used
            base_context["last_tool"] = last_turn.tool_used
        
        # Add resolved entities
        if self.resolved_identities:
            base_context["resolved_identities"] = self.resolved_identities
        
        # Add agent-specific context
        if agent_name == "pm_agent":
            base_context.update(self.pm_agent_context)
        elif agent_name == "pm_skill_agent":
            base_context.update(self.pm_skill_agent_context)
        
        return base_context
    
    def update_from_agent(self, agent_name: str, context_update: Dict[str, Any]):
        """Update session state from agent execution results."""
        self.last_active = datetime.utcnow()
        
        # Update sticky context if provided
        if "project" in context_update:
            self.project = context_update["project"]
        if "team" in context_update:
            self.team = context_update["team"]
        if "iteration" in context_update:
            self.iteration = context_update["iteration"]
        if "area_path" in context_update:
            self.area_path = context_update["area_path"]
        
        # Update resolved entities
        if "resolved_identity" in context_update:
            identity = context_update["resolved_identity"]
            if isinstance(identity, dict):
                name = identity.get("name", identity.get("displayName", ""))
                token = identity.get("token", identity.get("uniqueName", ""))
                if name and token:
                    self.resolved_identities[name.lower()] = token
        
        # Update pending operations
        if "pending_billing_deviation" in context_update:
            self.pending_billing_deviation = context_update["pending_billing_deviation"]
            if self.pending_billing_deviation is None:
                logger.info(f"[STATE_MANAGER] Cleared pending_billing_deviation for session {self.session_id}")
        
        # Store agent-specific context
        if agent_name == "pm_agent":
            self.pm_agent_context.update(context_update)
        elif agent_name == "pm_skill_agent":
            self.pm_skill_agent_context.update(context_update)
    
    def set_pending_clarification(self, clarification_type: str, original_query: str, 
                                   ambiguous_token: Optional[str] = None):
        """Set pending clarification state."""
        self.pending_clarification = {
            "type": clarification_type,
            "original_query": original_query,
            "ambiguous_token": ambiguous_token,
            "timestamp": datetime.utcnow().isoformat()
        }
    
    def resolve_clarification(self, user_response: str) -> Optional[str]:
        """
        Resolve pending clarification with user's response.
        
        Returns:
            Reconstructed query if clarification resolved, None otherwise
        """
        if not self.pending_clarification:
            return None
        
        original = self.pending_clarification.get("original_query", "")
        clarification_type = self.pending_clarification.get("type", "")
        ambiguous_token = self.pending_clarification.get("ambiguous_token", "")
        
        # Clear pending state
        self.pending_clarification = None
        
        # Reconstruct based on type
        if clarification_type == "identity" and ambiguous_token:
            # Replace ambiguous token with full name
            reconstructed = original.lower().replace(ambiguous_token.lower(), user_response)
            if reconstructed == original.lower():
                reconstructed = f"{original} (name: {user_response})"
            return reconstructed
        
        return f"{original} {user_response}"
    
    def is_stale(self, ttl_hours: int = 24) -> bool:
        """Check if session is stale."""
        age = datetime.utcnow() - self.last_active
        return age > timedelta(hours=ttl_hours)


class ConversationStateManager:
    """
    Centralized conversation state manager for the orchestrator.
    
    Manages session state across multiple agents and provides:
    - Session lifecycle management
    - Cross-agent context sharing
    - Follow-up query handling
    - State persistence (optional)
    """
    
    def __init__(self, ttl_hours: int = 24, max_sessions: int = 1000):
        """
        Initialize state manager.
        
        Args:
            ttl_hours: Session time-to-live in hours
            max_sessions: Maximum concurrent sessions
        """
        self._sessions: Dict[str, SessionState] = {}
        self._lock = Lock()
        self.ttl_hours = ttl_hours
        self.max_sessions = max_sessions
        logger.info(f"ConversationStateManager initialized (ttl={ttl_hours}h, max={max_sessions})")
    
    def get_or_create(self, session_id: str) -> SessionState:
        """Get existing session or create new one."""
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionState(session_id=session_id)
                logger.debug(f"Created new session: {session_id}")
                
                # Cleanup if too many sessions
                if len(self._sessions) > self.max_sessions:
                    self._cleanup_stale_sessions()
            
            return self._sessions[session_id]
    
    def get(self, session_id: str) -> Optional[SessionState]:
        """Get session if exists."""
        return self._sessions.get(session_id)
    
    def update_session(self, session_id: str, agent_name: str, 
                       context_update: Dict[str, Any], 
                       query: str = None,
                       result_summary: str = None):
        """
        Update session after agent execution.
        
        Args:
            session_id: Session identifier
            agent_name: Name of agent that executed
            context_update: Context to merge into session
            query: Original query (for turn tracking)
            result_summary: Summary of result (for history)
        """
        session = self.get_or_create(session_id)
        
        # Update last turn if query provided
        if query:
            turn = session.add_turn(
                query=query,
                agent_used=agent_name,
                result_summary=result_summary[:200] if result_summary else None
            )
            if "tool" in context_update:
                turn.tool_used = context_update["tool"]
            if "skill" in context_update:
                turn.skill_used = context_update["skill"]
        
        # Merge context update
        session.update_from_agent(agent_name, context_update)
    
    def get_context_for_routing(self, session_id: str, query: str) -> Dict[str, Any]:
        """
        Get context to assist routing decisions.
        
        Includes:
        - Session history summary
        - Pending clarification state
        - Recent agent/tool usage
        """
        session = self.get_or_create(session_id)
        
        context = {
            "session_id": session_id,
            "turn_number": session.current_turn,
            "has_history": len(session.turns) > 0,
        }
        
        # Add pending clarification
        if session.pending_clarification:
            context["pending_clarification"] = session.pending_clarification
            
            # Check if current query looks like clarification response
            words = query.strip().split()
            is_short_response = (
                len(words) <= 4 and
                not any(kw in query.lower() for kw in ["get", "show", "list", "find", "what", "how"])
            )
            context["is_clarification_response"] = is_short_response
        
        # Add pending billing_deviation context
        if session.pending_billing_deviation:
            context["pending_billing_deviation"] = session.pending_billing_deviation
            logger.info(f"[STATE_MANAGER] Found pending billing_deviation: {session.pending_billing_deviation}")
            
            # Check if query looks like a numeric response (target hours)
            query_stripped = query.strip()
            is_numeric_response = bool(re.match(r'^\d+$', query_stripped))
            context["is_billing_deviation_response"] = is_numeric_response
        
        # Add recent history
        if session.turns:
            last_turn = session.turns[-1]
            context["last_agent"] = last_turn.agent_used
            context["last_skill"] = last_turn.skill_used
            context["last_tool"] = last_turn.tool_used
        
        return context
    
    def delete(self, session_id: str):
        """Delete a session."""
        with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.debug(f"Deleted session: {session_id}")
    
    def _cleanup_stale_sessions(self):
        """Remove stale sessions."""
        stale = [sid for sid, sess in self._sessions.items() if sess.is_stale(self.ttl_hours)]
        for sid in stale:
            del self._sessions[sid]
        if stale:
            logger.info(f"Cleaned up {len(stale)} stale sessions")
    
    def get_stats(self) -> Dict[str, Any]:
        """Get manager statistics."""
        return {
            "active_sessions": len(self._sessions),
            "max_sessions": self.max_sessions,
            "ttl_hours": self.ttl_hours
        }


# Singleton instance
_state_manager: Optional[ConversationStateManager] = None


def get_state_manager() -> ConversationStateManager:
    """Get or create state manager singleton."""
    global _state_manager
    if _state_manager is None:
        _state_manager = ConversationStateManager()
    return _state_manager
