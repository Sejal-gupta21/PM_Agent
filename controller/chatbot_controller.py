"""
Chatbot Controller - Entry point for UI → Orchestrator with Unified Tracing

This is the ONLY correct entry point from chat UI (Streamlit/Web/CLI) to the orchestrator.

Responsibilities (STRICT):
- Accept raw user input
- Normalize input (trim whitespace, string safety)
- Attach session metadata (session_id, turn_number, user_id, project, org)
- Construct structured request object
- **CREATE UNIFIED TRACE** for the entire request lifecycle
- Forward to orchestrator
- Return orchestrator response AS-IS

Non-Responsibilities (FORBIDDEN):
- NO LLM calls
- NO routing or intent detection
- NO agent calls
- NO tool execution (ADO MCP or otherwise)
- NO output modification
- NO business logic
- NO global state
- NO conversation history (orchestrator owns state)

This controller is:
- Stateless
- Deterministic
- Synchronous
- Creates single trace per request for unified observability
"""

import os
import logging
from typing import Dict, Any, Optional
from uuid import uuid4

from utilities.langfuse_client import (
    create_trace, finalize_trace, get_langfuse_client
)

logger = logging.getLogger("controller.chatbot")


class ChatbotController:
    """
    Thin, stateless controller that connects UI → Orchestrator.
    
    This is the ONLY entry point from chat UI to the orchestrator.
    Creates a single Langfuse trace per request for unified observability.
    """
    
    def __init__(self):
        """Initialize controller (stateless, no stored config)."""
        pass
    
    def process_request(
        self,
        user_query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        organization: Optional[str] = None,
        turn_number: int = 0
    ) -> Dict[str, Any]:
        """
        Process user request through orchestrator.
        
        This is the ONLY public method. It:
        1. Normalizes input
        2. Attaches metadata
        3. **Creates unified trace** for observability
        4. Calls orchestrator
        5. Finalizes trace and returns orchestrator response AS-IS
        
        Args:
            user_query: Raw user input (string)
            session_id: Session identifier (generate if None)
            user_id: User identifier (optional)
            project_id: ADO project (default from env)
            organization: ADO organization (default from env)
            turn_number: Conversation turn number (default 0)
            
        Returns:
            Dict with orchestrator response (passed through unchanged)
        """
        # Step 1: Normalize input
        normalized_query = self._normalize_input(user_query)
        
        # Step 2: Generate session ID if not provided
        actual_session_id = session_id or self._generate_session_id()
        
        # Step 3: Attach session metadata
        request = self._build_request(
            query=normalized_query,
            session_id=actual_session_id,
            user_id=user_id,
            project_id=project_id or os.getenv("ADO_PROJECT", "FracPro-OPS"),
            organization=organization or os.getenv("ADO_ORG_NAME", "Stratagen"),
            turn_number=turn_number
        )
        
        # Step 4: Create controller-level trace for unified observability
        from utilities.langfuse_client import create_trace, finalize_trace, set_current_trace, get_langfuse_client
        
        trace = create_trace(
            name="controller_request",
            input_data={
                "query": normalized_query[:500],
                "session_id": actual_session_id,
                "turn_number": turn_number
            },
            metadata={
                "source": "chatbot_controller",
                "provenance": "controller",  # Tracks entry point for observability
                "project_id": project_id or os.getenv("ADO_PROJECT", "FracPro-OPS"),
                "organization": organization or os.getenv("ADO_ORG_NAME", "Stratagen"),
                "user_id": user_id
            },
            session_id=actual_session_id
        )
        
        # Set as current trace so orchestrator creates spans under it
        if trace:
            set_current_trace(trace)
        
        # Step 5: Forward to orchestrator (synchronous)
        try:
            orchestrator_response = self._call_orchestrator(request)
        except Exception as e:
            logger.exception(f"Controller: orchestrator call failed: {e}")
            
            # Finalize trace with error
            if trace:
                finalize_trace(
                    trace,
                    output={"error": str(e)},
                    status="error"
                )
                client = get_langfuse_client()
                if client:
                    client.flush()
            
            # Return error response in expected format
            orchestrator_response = {
                "is_task_complete": True,
                "content": f"[ERROR] Failed to process request: {e}",
                "error": str(e),
                "confidence": 0.0,
                "warnings": [f"Orchestrator error: {e}"]
            }
        else:
            # Finalize trace with success
            if trace:
                finalize_trace(
                    trace,
                    output={
                        "is_task_complete": orchestrator_response.get("is_task_complete"),
                        "response_length": len(str(orchestrator_response.get("content", "")))
                    },
                    status="success"
                )
                client = get_langfuse_client()
                if client:
                    client.flush()
        
        # Step 6: Return orchestrator response AS-IS (no modification)
        return orchestrator_response
    
    def _normalize_input(self, user_query: str) -> str:
        """
        Normalize raw user input.
        
        - Trim whitespace
        - Ensure string safety
        - No transformation or interpretation
        
        Args:
            user_query: Raw input
            
        Returns:
            Normalized string
        """
        if not user_query:
            return ""
        
        # Convert to string if needed
        query = str(user_query)
        
        # Trim whitespace
        query = query.strip()
        
        # Basic safety: ensure single line or preserve newlines if intentional
        # (Do NOT modify content, just safety checks)
        
        return query
    
    def _generate_session_id(self) -> str:
        """
        Generate unique session ID.
        
        Returns:
            UUID-based session ID
        """
        return f"session-{uuid4().hex[:12]}"
    
    def _build_request(
        self,
        query: str,
        session_id: str,
        user_id: Optional[str],
        project_id: str,
        organization: str,
        turn_number: int
    ) -> Dict[str, Any]:
        """
        Build structured request object for orchestrator.
        
        This is a simple dict construction with metadata.
        No logic, no transformation.
        
        Args:
            query: Normalized user query
            session_id: Session identifier
            user_id: User identifier (optional)
            project_id: ADO project
            organization: ADO organization
            turn_number: Conversation turn number
            
        Returns:
            Structured request dict
        """
        request = {
            "query": query,
            "session_id": session_id,
            "turn_number": turn_number,
            "metadata": {
                "project_id": project_id,
                "organization": organization,
            }
        }
        
        if user_id:
            request["metadata"]["user_id"] = user_id
        
        return request
    
    def _call_orchestrator(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call orchestrator with structured request (SYNC version).
        
        This uses the existing process_query_sync() helper.
        No logic, no transformation.
        
        Args:
            request: Structured request dict
            
        Returns:
            Orchestrator response (passed through)
        """
        from utilities.chat_helpers import process_query_sync
        
        # Extract query and session_id for orchestrator call
        query = request["query"]
        session_id = request["session_id"]
        
        # Log the request
        logger.info(f"Controller: forwarding to orchestrator (session={session_id}, turn={request['turn_number']})")
        
        # Call orchestrator (sync wrapper)
        response = process_query_sync(query, session_id)
        
        # Log completion (minimal)
        logger.info(f"Controller: orchestrator returned (is_complete={response.get('is_task_complete')})")
        
        # Return AS-IS
        return response
    
    async def _call_orchestrator_async(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call orchestrator with structured request (ASYNC version).
        
        This directly calls the async orchestrator for native async support.
        
        Args:
            request: Structured request dict
            
        Returns:
            Orchestrator response (passed through)
        """
        from utilities.chat_helpers import process_via_orchestrator
        
        # Extract query and session_id for orchestrator call
        query = request["query"]
        session_id = request["session_id"]
        
        # Log the request
        logger.info(f"Controller: forwarding to orchestrator [ASYNC] (session={session_id}, turn={request['turn_number']})")
        
        # Call orchestrator (async)
        response = await process_via_orchestrator(query, session_id)
        
        # Log completion (minimal)
        logger.info(f"Controller: orchestrator returned (is_complete={response.get('is_task_complete')})")
        
        # Return AS-IS
        return response
    
    async def process_request_async(
        self,
        user_query: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        project_id: Optional[str] = None,
        organization: Optional[str] = None,
        turn_number: int = 0
    ) -> Dict[str, Any]:
        """
        Process user request through orchestrator (ASYNC version).
        
        This is the async-compatible version of process_request().
        Use this when calling from async contexts (tests, async apps, etc).
        
        Args:
            user_query: Raw user input (string)
            session_id: Session identifier (generate if None)
            user_id: User identifier (optional)
            project_id: ADO project (default from env)
            organization: ADO organization (default from env)
            turn_number: Conversation turn number (default 0)
            
        Returns:
            Dict with orchestrator response (passed through unchanged)
        """
        # Step 1: Normalize input
        normalized_query = self._normalize_input(user_query)
        
        # Step 2: Generate session ID if not provided
        actual_session_id = session_id or self._generate_session_id()
        
        # Step 3: Attach session metadata
        request = self._build_request(
            query=normalized_query,
            session_id=actual_session_id,
            user_id=user_id,
            project_id=project_id or os.getenv("ADO_PROJECT", "FracPro-OPS"),
            organization=organization or os.getenv("ADO_ORG_NAME", "Stratagen"),
            turn_number=turn_number
        )
        
        # Step 4: Create controller-level trace for unified observability
        from utilities.langfuse_client import create_trace, finalize_trace, set_current_trace, get_langfuse_client
        
        trace = create_trace(
            name="controller_request",
            input_data={
                "query": normalized_query[:500],
                "session_id": actual_session_id,
                "turn_number": turn_number
            },
            metadata={
                "source": "chatbot_controller",
                "provenance": "controller",
                "project_id": project_id or os.getenv("ADO_PROJECT", "FracPro-OPS"),
                "organization": organization or os.getenv("ADO_ORG_NAME", "Stratagen"),
                "user_id": user_id,
                "async_mode": True
            },
            session_id=actual_session_id
        )
        
        # Set as current trace so orchestrator creates spans under it
        if trace:
            set_current_trace(trace)
        
        # Step 5: Forward to orchestrator (asynchronous)
        try:
            orchestrator_response = await self._call_orchestrator_async(request)
        except Exception as e:
            logger.exception(f"Controller: orchestrator call failed: {e}")
            
            # Finalize trace with error
            if trace:
                finalize_trace(
                    trace,
                    output={"error": str(e)},
                    status="error"
                )
                client = get_langfuse_client()
                if client:
                    client.flush()
            
            # Return error response in expected format
            orchestrator_response = {
                "is_task_complete": True,
                "content": f"[ERROR] Failed to process request: {e}",
                "error": str(e),
                "confidence": 0.0,
                "warnings": [f"Orchestrator error: {e}"]
            }
        else:
            # Finalize trace with success
            if trace:
                finalize_trace(
                    trace,
                    output={
                        "is_task_complete": orchestrator_response.get("is_task_complete"),
                        "response_length": len(str(orchestrator_response.get("content", "")))
                    },
                    status="success"
                )
                client = get_langfuse_client()
                if client:
                    client.flush()
        
        # Step 6: Return orchestrator response AS-IS (no modification)
        return orchestrator_response


# ============================================================================
# SINGLETON INSTANCE (for convenience)
# ============================================================================

_controller_instance: Optional[ChatbotController] = None


def get_controller() -> ChatbotController:
    """
    Get singleton controller instance.
    
    Returns:
        ChatbotController instance
    """
    global _controller_instance
    if _controller_instance is None:
        _controller_instance = ChatbotController()
    return _controller_instance
