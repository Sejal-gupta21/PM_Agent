"""
Langfuse Client - Centralized observability client with trace context propagation.

This module provides:
1. Singleton Langfuse client initialization using config.yaml credentials
2. Trace context manager for unified tracing across the entire request lifecycle
3. Helper functions for creating spans within an active trace
4. Parent trace management for single-trace observability across multi-agent flows

CRITICAL: All agent steps must use the same trace_id for unified observability.
Use create_parent_trace() at the entry point, then all child spans automatically 
roll up under that trace via ContextVars.
"""

import os
import time
import logging
import threading
import traceback
from typing import Optional, Dict, Any, Tuple
from contextvars import ContextVar
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_langfuse_client = None

# Context variables to store the current trace across async boundaries
_current_trace: ContextVar[Optional[Any]] = ContextVar("current_trace", default=None)
_current_trace_id: ContextVar[Optional[str]] = ContextVar("current_trace_id", default=None)
_trace_start_time: ContextVar[Optional[float]] = ContextVar("trace_start_time", default=None)
_trace_metadata: ContextVar[Optional[Dict[str, Any]]] = ContextVar("trace_metadata", default=None)

# Thread-local storage as fallback for sync->async boundary crossing
# (ContextVars don't propagate across asyncio.run_until_complete boundaries)
_thread_local = threading.local()


def get_langfuse_client():
    """Return a singleton Langfuse client or None if unavailable.

    This centralizes Langfuse initialization so other modules can import
    `get_langfuse_client()` and avoid duplicate inits.
    
    Uses config.yaml credentials (preferred) with env var fallback.
    """
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    try:
        from langfuse import Langfuse
        
        # Prefer config.yaml credentials over env vars
        try:
            from config import config
            public_key = config.langfuse_public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
            secret_key = config.langfuse_secret_key or os.getenv("LANGFUSE_SECRET_KEY")
            base_url = config.langfuse_base_url or os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        except ImportError:
            public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
            secret_key = os.getenv("LANGFUSE_SECRET_KEY")
            base_url = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

        if not public_key or not secret_key:
            logger.warning("[langfuse_client] Langfuse credentials not configured")
            return None

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=base_url
        )

        if client and getattr(client, "auth_check", lambda: True)():
            _langfuse_client = client
            logger.info("[langfuse_client] Langfuse client initialized with base_url=%s", base_url)
            return _langfuse_client
        else:
            logger.warning("[langfuse_client] Langfuse auth failed or client unavailable")
            return None
    except ImportError:
        logger.debug("[langfuse_client] langfuse package not installed")
        return None
    except Exception as e:
        # If Langfuse import fails due to pydantic/python incompatibility or
        # other environment issues, fall back to a lightweight local stub
        # client that records traces to a local file. This preserves behavior
        # (no crashes) and provides trace data for debugging until the
        # environment is corrected (e.g., use Python 3.11 and pinned packages).
        logger.error(f"[langfuse_client] initialization failed: {e}", exc_info=True)

        try:
            # Simple local stub client
            class _LocalObservation:
                def __init__(self, name, trace_id):
                    self.name = name
                    self.trace_id = trace_id
                    self._data = {"name": name, "trace_id": trace_id, "spans": []}

                def start_observation(self, as_type=None, name=None, input=None, metadata=None):
                    span_id = f"local-span-{len(self._data['spans'])+1}"
                    span = {"id": span_id, "type": as_type or "SPAN", "name": name, "input": input, "metadata": metadata}
                    self._data["spans"].append(span)
                    return _LocalSpan(self, span)

                def generation(self, name=None, model=None, input=None, output=None, metadata=None):
                    g = {"name": name, "model": model, "input": input, "output": output, "metadata": metadata}
                    self._data.setdefault("generations", []).append(g)
                    return g

                def update(self, **kwargs):
                    self._data.setdefault("updated", {}).update(kwargs)

                def end(self):
                    # write to outputs for inspection
                    try:
                        import json
                        out_dir = os.path.join(os.getcwd(), "outputs")
                        os.makedirs(out_dir, exist_ok=True)
                        path = os.path.join(out_dir, f"langfuse_local_trace_{self.trace_id}.json")
                        with open(path, "w", encoding="utf-8") as fh:
                            json.dump(self._data, fh, indent=2, default=str)
                    except Exception:
                        pass

            class _LocalSpan:
                def __init__(self, parent, span_dict):
                    self._parent = parent
                    self._span = span_dict

                def update(self, **kwargs):
                    self._span.setdefault("updated", {}).update(kwargs)

                def end(self):
                    # Persist the parent observation when the span ends
                    try:
                        self._parent.end()
                    except Exception:
                        pass

            class _LocalClient:
                def __init__(self):
                    self._counter = 0

                def auth_check(self):
                    return True

                def start_observation(self, name=None, input=None, metadata=None, as_type=None):
                    self._counter += 1
                    trace_id = f"local-trace-{int(os.times().system)}-{self._counter}"
                    return _LocalObservation(name or "local", trace_id)

                def trace(self, name=None, input=None, metadata=None, session_id=None, **kwargs):
                    # Accepts 'input' and arbitrary kwargs to be compatible with different SDK signatures
                    self._counter += 1
                    trace_id = f"local-trace-{int(os.times().system)}-{self._counter}"
                    obs = _LocalObservation(name or "local", trace_id)
                    # Record initial input/metadata for debugging
                    try:
                        obs._data["input"] = input
                        obs._data["metadata"] = metadata or {}
                        if session_id:
                            obs._data["session_id"] = session_id
                    except Exception:
                        pass
                    return obs

                # Backwards-compatible alias expected by tests/code
                def start_span(self, name=None, as_type=None, input=None, metadata=None):
                    self._counter += 1
                    trace_id = f"local-trace-{int(os.times().system)}-{self._counter}"
                    obs = _LocalObservation(name or "local", trace_id)
                    return obs.start_observation(as_type=as_type, name=name, input=input, metadata=metadata)

                def flush(self):
                    return True

            _langfuse_client = _LocalClient()
            logger.warning("[langfuse_client] using local fallback Langfuse stub client due to import error; install compatible Langfuse/pydantic and use Python 3.11 for full functionality")
            return _langfuse_client
        except Exception:
            return None


def create_parent_trace(
    name: str, 
    user_id: str = "default",
    input_data: Optional[Dict[str, Any]] = None, 
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[Any]:
    """
    Create a parent trace for a user request.
    
    This is the PRIMARY entry point for creating traces. Call this ONCE at the
    start of processing a user query. All subsequent create_span() calls will
    automatically nest under this parent trace via ContextVars.
    
    Args:
        name: Trace name (e.g., "user_query", "skill_execution")
        user_id: User identifier for grouping traces
        input_data: Input data for the trace (e.g., {"query": "..."})
        metadata: Additional metadata (skill_id, session_id, etc.)
        
    Returns:
        Langfuse trace object or None
        
    Example:
        # At the entry point of your request handler:
        trace = create_parent_trace(
            "user_query",
            user_id="user@example.com",
            input_data={"query": "what is the sprint status"},
            metadata={"skill_id": "get_sprint_status"}
        )
        
        # All subsequent spans nest under this trace automatically:
        span1 = create_span("skill_matching", ...)  # child of trace
        span2 = create_span("tool_execution", ...)  # child of trace
    """
    client = get_langfuse_client()
    if not client:
        return None
    
    try:
        # Build comprehensive metadata
        meta = metadata or {}
        meta["user_id"] = user_id
        meta["created_at"] = datetime.now(timezone.utc).isoformat()
        
        trace = client.start_observation(
            name=name,
            input=input_data,
            metadata=meta
        )
        
        # Store in context vars for child spans
        _current_trace.set(trace)
        _current_trace_id.set(trace.trace_id if hasattr(trace, 'trace_id') else None)
        _trace_start_time.set(time.time())
        _trace_metadata.set({"skill_id": meta.get("skill_id")})
        
        logger.debug("[langfuse_client] Created parent trace: %s (trace_id=%s, user=%s)", 
                     name, getattr(trace, 'trace_id', 'unknown'), user_id)
        return trace
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to create parent trace: {e}")
        return None


def create_trace(name: str, input_data: Optional[Dict[str, Any]] = None, 
                 metadata: Optional[Dict[str, Any]] = None, 
                 session_id: Optional[str] = None) -> Optional[Any]:
    """
    Create a new top-level trace for a request.
    
    This should be called ONCE at the entry point (e.g., chatbot controller or orchestrator).
    All subsequent spans should be created within this trace context.
    
    Args:
        name: Trace name (e.g., "controller_request")
        input_data: Input data for the trace
        metadata: Additional metadata
        session_id: Session identifier for grouping traces
        
    Returns:
        Langfuse observation object (trace) or None
    """
    client = get_langfuse_client()
    if not client:
        return None
    
    # Try creating a trace using the client's trace() API first. If that fails
    # (different SDK signatures or missing kwargs), fall back to start_observation.
    try:
        try:
            trace = client.trace(name=name, input=input_data, metadata=metadata, session_id=session_id)
        except Exception:
            trace = None

        if trace is not None:
            set_current_trace(trace)
            logger.debug("[langfuse_client] Created trace: %s (trace_id=%s, session=%s)", name, getattr(trace, 'id', 'unknown'), session_id or 'none')
            return trace

        # Fallback to start_observation if client.trace() wasn't usable
        logger.debug("[langfuse_client] Using start_observation fallback (session in metadata)")
        meta = metadata or {}
        if session_id:
            meta["session_id"] = session_id

        trace = client.start_observation(name=name, input=input_data, metadata=meta)
        set_current_trace(trace, trace.trace_id if hasattr(trace, 'trace_id') else None)
        logger.debug("[langfuse_client] Created trace via start_observation: %s (trace_id=%s)", name, getattr(trace, 'trace_id', 'unknown'))
        return trace
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to create trace: {e}")
        return None


def join_trace(trace_id: str, name: str = "joined_trace", 
               session_id: Optional[str] = None,
               metadata: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """
    Join an existing trace by ID.
    
    Used by remote agents to join a trace started by the orchestrator/controller.
    This enables single-trace observability across A2A boundaries.
    
    Args:
        trace_id: The ID of the existing trace to join
        name: Name for this trace segment
        session_id: Optional session ID
        metadata: Additional metadata
        
    Returns:
        Langfuse trace object or None
    """
    if not trace_id:
        return None
    
    client = get_langfuse_client()
    if not client:
        return None
    
    try:
        # Create a new trace that references the parent trace ID
        trace = client.trace(
            name=name,
            metadata={**(metadata or {}), "parent_trace_id": trace_id},
            session_id=session_id
        )
        # Store in context var for child spans
        _current_trace.set(trace)
        _current_trace_id.set(trace.id if hasattr(trace, 'id') else None)
        logger.debug("[langfuse_client] Joined trace: %s (parent=%s)", name, trace_id)
        return trace
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to join trace: {e}")
        return None


def get_current_trace() -> Optional[Any]:
    """Get the current active trace from context or thread-local storage."""
    # First try context var (works within same async context)
    trace = _current_trace.get()
    if trace:
        logger.debug("[langfuse_client] get_current_trace: Found in ContextVar")
        return trace
    # Fallback to thread-local (works across sync->async boundaries)
    tl_trace = getattr(_thread_local, 'trace', None)
    if tl_trace:
        logger.debug("[langfuse_client] get_current_trace: Found in thread-local")
    else:
        logger.warning("[langfuse_client] get_current_trace: NO TRACE FOUND in ContextVar or thread-local!")
    return tl_trace


def get_current_trace_id() -> Optional[str]:
    """Get the current active trace ID from context or thread-local storage."""
    # First try context var
    trace_id = _current_trace_id.get()
    if trace_id:
        return trace_id
    # Fallback to thread-local
    return getattr(_thread_local, 'trace_id', None)


def set_current_trace(trace: Any, trace_id: Optional[str] = None):
    """Set the current trace in both context and thread-local storage."""
    resolved_id = trace_id or (trace.id if hasattr(trace, 'id') else None)
    # Set in context var (for async propagation within same context)
    _current_trace.set(trace)
    _current_trace_id.set(resolved_id)
    # Also set in thread-local (for sync->async boundary crossing)
    _thread_local.trace = trace
    _thread_local.trace_id = resolved_id
    logger.debug("[langfuse_client] Set current trace: %s", resolved_id)


def create_span(name: str, input_data: Optional[Dict[str, Any]] = None,
                metadata: Optional[Dict[str, Any]] = None,
                parent_trace: Optional[Any] = None,
                session_id: Optional[str] = None) -> Optional[Any]:
    """
    Create a span within the current trace context.
    
    This ensures all spans roll up under a single parent trace for unified observability.
    The trace hierarchy is maintained through ContextVars, so all spans created in the
    same request lifecycle will appear under the same parent trace in Langfuse.
    
    CRITICAL: Always call create_trace() once at the entry point (e.g., controller),
    then all subsequent create_span() calls will automatically nest under that trace.
    
    Args:
        name: Span name (e.g., "pm_agent_invoke", "llm_planner")
        input_data: Input data for the span
        metadata: Additional metadata (include step info for easier debugging)
        parent_trace: Optional explicit parent trace (uses context if not provided)
        session_id: Session ID for grouping traces (CRITICAL for unified sessions)
        
    Returns:
        Langfuse span object or None
        
    Example:
        # At controller entry point
        trace = create_trace("controller_request", input_data={"query": query})
        
        # All subsequent spans nest under the trace
        span1 = create_span("orchestrator", input_data={"step": 1})
        span2 = create_span("llm_planner", input_data={"step": 2})
        span3 = create_span("pm_agent_invoke", input_data={"step": 3})
        
        # In Langfuse UI, you'll see:
        # controller_request (trace)
        #   ├── orchestrator (span)
        #   ├── llm_planner (span)
        #   └── pm_agent_invoke (span)
    """
    trace = parent_trace or get_current_trace()
    client = get_langfuse_client()
    
    if not trace and not client:
        logger.debug("[langfuse_client] create_span: No trace and no client available")
        return None
    
    if not trace:
        logger.warning(f"[langfuse_client] create_span '{name}': No parent trace found, will create standalone observation")
    
    try:
        # Langfuse SDK v3.10.7+: use start_observation on parent or client
        if trace and hasattr(trace, 'start_observation'):
            # Create child span under existing trace/observation
            # session_id is inherited from parent trace automatically
            span = trace.start_observation(
                as_type="SPAN",
                name=name,
                input=input_data,
                metadata=metadata or {}
            )
        elif client:
            # No active trace - create standalone observation with session_id
            # Use start_observation (not .span() which doesn't exist)
            meta = metadata or {}
            if session_id:
                meta["session_id"] = session_id
                
            span = client.start_observation(
                name=name,
                as_type="SPAN",
                input=input_data,
                metadata=meta
            )
        else:
            return None
            
        logger.debug("[langfuse_client] Created span: %s", name)
        return span
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to create span: {e}")
        return None


def create_generation(name: str, model: str, input_data: Optional[Any] = None,
                      output: Optional[Any] = None, metadata: Optional[Dict[str, Any]] = None,
                      parent_span: Optional[Any] = None) -> Optional[Any]:
    """
    Create a generation event for LLM calls within a span or trace.
    
    Args:
        name: Generation name (e.g., "planner_llm", "synthesis_llm")
        model: Model name (e.g., "gpt-4o-mini")
        input_data: LLM input (prompt)
        output: LLM output
        metadata: Additional metadata
        parent_span: Optional explicit parent span
        
    Returns:
        Langfuse generation object or None
    """
    parent = parent_span or get_current_trace()
    if not parent:
        return None
    
    try:
        generation = parent.generation(
            name=name,
            model=model,
            input=input_data,
            output=output,
            metadata=metadata or {}
        )
        logger.debug("[langfuse_client] Created generation: %s (model=%s)", name, model)
        return generation
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to create generation: {e}")
        return None


def clear_current_trace():
    """Clear the current trace from both context and thread-local storage."""
    _current_trace.set(None)
    _current_trace_id.set(None)
    _thread_local.trace = None
    _thread_local.trace_id = None
    logger.debug("[langfuse_client] Cleared current trace")


def finalize_trace(trace: Optional[Any], output: Optional[Dict[str, Any]] = None,
                   status: str = "success", level: Optional[str] = None):
    """
    Finalize a trace with output and status.
    
    Args:
        trace: Trace object to finalize
        output: Output data
        status: Status message
        level: Optional level (DEBUG, INFO, WARNING, ERROR)
    """
    if not trace:
        return
    
    client = get_langfuse_client()
    
    try:
        # Normalize error output when status indicates failure
        normalized_output = output
        if status and str(status).lower() == "error":
            if isinstance(output, Exception):
                normalized_output = {"error": str(output), "traceback": traceback.format_exc()}
            elif isinstance(output, str):
                normalized_output = {"error": output}
            elif isinstance(output, dict) and "error" not in output:
                normalized_output = {**output, "error": str(output)}

        update_kwargs = {"output": normalized_output, "status_message": status}
        if level:
            update_kwargs["level"] = level
        trace.update(**update_kwargs)
        trace.end()  # Critical: must call end() to finalize the trace
        
        # Clear the current trace context
        clear_current_trace()
        
        # Flush to ensure data is sent immediately
        if client:
            client.flush()
        logger.debug("[langfuse_client] Finalized trace with status=%s", status)
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to finalize trace: {e}")


def finalize_span(span: Optional[Any], output: Optional[Any] = None,
                  status: str = "success", level: Optional[str] = None):
    """
    Finalize a span with output and status.
    
    Args:
        span: Span object to finalize
        output: Output data
        status: Status message
        level: Optional level (DEBUG, INFO, WARNING, ERROR)
    """
    if not span:
        return
    
    try:
        # Langfuse SDK v3.10.7+ uses update() and end() methods
        # Normalize output for error cases and include traceback when possible
        normalized_output = output
        if status and str(status).lower() == "error":
            if isinstance(output, Exception):
                normalized_output = {"error": str(output), "traceback": traceback.format_exc()}
            elif isinstance(output, str):
                normalized_output = {"error": output}
            elif isinstance(output, dict) and "error" not in output:
                normalized_output = {**output, "error": str(output)}

        update_kwargs = {}
        if normalized_output is not None:
            update_kwargs["output"] = normalized_output
        if status:
            update_kwargs["status_message"] = status
        if level:
            update_kwargs["level"] = level

        if update_kwargs:
            try:
                span.update(**update_kwargs)
            except Exception:
                # Best-effort: avoid failing finalize on update errors
                try:
                    span.update(output=str(normalized_output), status_message=status, level=level)
                except Exception:
                    pass
        span.end()
        logger.debug("[langfuse_client] Finalized span with status=%s", status)
    except Exception as e:
        logger.error(f"[langfuse_client] Failed to finalize span: {e}")


def trace_task(task_name: str, metadata: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None):
    """
    Decorator to add Langfuse tracing to PM Agent tasks (capacity triaging, backlog triaging, etc.).
    
    Usage:
        @trace_task("backlog_triaging", metadata={"project": "FracPro-OPS"})
        async def run_backlog_triaging(config, options, recipients):
            ...
    
    Args:
        task_name: Name of the task (e.g., "backlog_triaging", "capacity_triaging")
        metadata: Additional metadata to include in the trace
        session_id: Optional session ID for grouping traces (defaults to "pm_agent_tasks")
    
    Returns:
        Decorator function
    """
    import functools
    import asyncio
    from datetime import datetime
    
    # Default session ID groups all PM Agent tasks together
    if session_id is None:
        session_id = f"pm_agent_tasks_{datetime.now().strftime('%Y%m%d')}"
    
    def decorator(func):
        is_async = asyncio.iscoroutinefunction(func)
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            client = get_langfuse_client()
            trace = None
            span = None

            # Prepare logging inputs
            args_to_log = args[1:] if args and hasattr(args[0], '__class__') else args
            input_data = {
                "task": task_name,
                "args_count": len(args_to_log),
                "kwargs_keys": list(kwargs.keys())
            }
            trace_metadata = {
                "task_type": "pm_agent_task",
                "task_name": task_name,
                **(metadata or {})
            }

            # If there's an existing trace in context, create a span under it instead
            existing = get_current_trace()
            if existing:
                try:
                    span = create_span(
                        name=f"task_{task_name}",
                        input_data=input_data,
                        metadata=trace_metadata,
                        parent_trace=existing,
                        session_id=session_id
                    )
                    logger.info(f"[trace_task] Created span for task under existing trace: {task_name}")
                except Exception as e:
                    logger.error(f"[trace_task] Failed to create span under existing trace for {task_name}: {e}")
            else:
                # No existing trace - create a standalone trace
                if client:
                    try:
                        trace = create_trace(
                            name=f"task_{task_name}",
                            input_data=input_data,
                            metadata=trace_metadata,
                            session_id=session_id
                        )
                        logger.info(f"[trace_task] Created trace for task: {task_name} (session: {session_id})")
                    except Exception as e:
                        logger.error(f"[trace_task] Failed to create trace for {task_name}: {e}")
            
            # Execute function
            try:
                result = await func(*args, **kwargs)
                
                # Finalize trace/span with result
                try:
                    output = {
                        "success": result.get("success") if isinstance(result, dict) else True,
                        "task": task_name
                    }
                    if isinstance(result, dict):
                        output["result_keys"] = list(result.keys())

                    if span:
                        finalize_span(span, output=output, status="success")
                    elif trace:
                        finalize_trace(trace, output=output, status="success")

                    # Flush and wait to ensure data is sent
                    if client:
                        client.flush()
                        import time
                        time.sleep(1)
                        logger.info(f"[trace_task] Finalized and flushed trace/span for: {task_name}")
                except Exception as e:
                    logger.error(f"[trace_task] Failed to finalize trace/span for {task_name}: {e}")
                
                return result
                
            except Exception as e:
                # Finalize trace/span with error
                try:
                    if span:
                        finalize_span(span, output={"error": str(e), "task": task_name}, status="error", level="ERROR")
                    elif trace:
                        finalize_trace(trace, output={"error": str(e), "task": task_name}, status="error", level="ERROR")
                    if client:
                        client.flush()
                        import time
                        time.sleep(1)
                    logger.error(f"[trace_task] Finalized error trace/span for {task_name}: {e}")
                except Exception as trace_err:
                    logger.error(f"[trace_task] Failed to finalize error trace/span for {task_name}: {trace_err}")
                raise
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            """Wrapper for synchronous functions"""
            client = get_langfuse_client()
            trace = None
            span = None

            args_to_log = args[1:] if args and hasattr(args[0], '__class__') else args
            input_data = {
                "task": task_name,
                "args_count": len(args_to_log),
                "kwargs_keys": list(kwargs.keys())
            }
            trace_metadata = {
                "task_type": "pm_agent_task",
                "task_name": task_name,
                **(metadata or {})
            }

            existing = get_current_trace()
            if existing:
                try:
                    span = create_span(
                        name=f"task_{task_name}",
                        input_data=input_data,
                        metadata=trace_metadata,
                        parent_trace=existing,
                        session_id=session_id
                    )
                    logger.info(f"[trace_task] Created span for task under existing trace: {task_name}")
                except Exception as e:
                    logger.error(f"[trace_task] Failed to create span under existing trace for sync {task_name}: {e}")
            else:
                if client:
                    try:
                        trace = create_trace(
                            name=f"task_{task_name}",
                            input_data=input_data,
                            metadata=trace_metadata,
                            session_id=session_id
                        )
                        logger.info(f"[trace_task] Created trace for sync task: {task_name} (session: {session_id})")
                    except Exception as e:
                        logger.error(f"[trace_task] Failed to create trace for sync {task_name}: {e}")
            
            # Execute function
            try:
                result = func(*args, **kwargs)
                
                # Finalize trace/span with result
                try:
                    output = {
                        "success": result.get("success") if isinstance(result, dict) else True,
                        "task": task_name
                    }
                    if isinstance(result, dict):
                        output["result_keys"] = list(result.keys())

                    if span:
                        finalize_span(span, output=output, status="success")
                    elif trace:
                        finalize_trace(trace, output=output, status="success")

                    if client:
                        client.flush()
                        import time
                        time.sleep(1)
                        logger.info(f"[trace_task] Finalized and flushed sync trace/span for: {task_name}")
                except Exception as e:
                    logger.error(f"[trace_task] Failed to finalize sync trace/span for {task_name}: {e}")
                
                return result
                
            except Exception as e:
                # Finalize trace/span with error
                try:
                    if span:
                        finalize_span(span, output={"error": str(e), "task": task_name}, status="error", level="ERROR")
                    elif trace:
                        finalize_trace(trace, output={"error": str(e), "task": task_name}, status="error", level="ERROR")
                    if client:
                        client.flush()
                        import time
                        time.sleep(1)
                    logger.error(f"[trace_task] Finalized sync error trace/span for {task_name}: {e}")
                except Exception as trace_err:
                    logger.error(f"[trace_task] Failed to finalize sync error trace/span for {task_name}: {trace_err}")
                raise
        
        # Return appropriate wrapper based on function type
        if is_async:
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator
