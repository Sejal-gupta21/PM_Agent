"""
Azure DevOps MCP Connector - Direct subprocess version

Connects to the Microsoft Azure DevOps MCP Server via subprocess.
Uses direct JSON-RPC communication over stdin/stdout.
"""

import asyncio
import json
import os
import sys
import subprocess
import re
from typing import Any, Dict, Optional
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class MCPConnector:
    """MCP connector using direct subprocess communication."""

    def __init__(self, org_name: str, pat_token: str):
        """
        Initialize MCP connector.

        Args:
            org_name: Azure DevOps organization name (e.g., 'Stratagen')
            pat_token: Personal Access Token for Azure DevOps
        """
        self.org_name = org_name
        self.pat_token = pat_token
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 1
        self.tools_cache: Dict[str, Any] = {}
        self._initialized = False
        # FIX #7: Add lock to serialize MCP requests - stdin/stdout cannot handle parallel requests
        # Do NOT create the asyncio.Lock() here — creating it binds it to the current
        # event loop which may differ from the loop used when the connector is later
        # used (e.g. Streamlit/controller runs). Create it lazily inside the running
        # event loop to avoid 'Lock bound to a different event loop' errors.
        self._request_lock = None

    async def _get_request_lock(self) -> asyncio.Lock:
        """Return the request lock, creating it in the current event loop if needed.

        This ensures the lock is bound to the currently running event loop.
        Handles cross-loop scenarios (e.g., Streamlit) by detecting event loop changes.
        """
        # Get the current event loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, create one
            current_loop = asyncio.get_event_loop()
        
        # Check if lock exists and is bound to current loop
        if self._request_lock is not None:
            try:
                # Try to check if lock is bound to a different loop
                # If the lock's loop differs from current loop, we need to recreate it
                lock_loop = getattr(self._request_lock, '_loop', None)
                if lock_loop is not None and lock_loop != current_loop:
                    logger.debug(f"[MCP] Lock bound to different event loop, recreating...")
                    self._request_lock = None
            except Exception:
                # If we can't check, recreate to be safe
                self._request_lock = None
        
        # Create new lock if needed
        if self._request_lock is None:
            self._request_lock = asyncio.Lock()
            logger.debug(f"[MCP] Created new lock in event loop {id(current_loop)}")
        
        return self._request_lock

    def _extract_first_valid_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Extract the first valid JSON object from mixed text (logs + JSON).
        Handles MCP output with embedded log lines and partial JSON chunks.
        
        Args:
            text: Raw output from MCP server (may contain logs and partial JSON)
        
        Returns:
            First valid JSON object found, or None if no valid JSON exists
        """
        if not text or not isinstance(text, str):
            return None
        
        # Strategy 1: Try to find the start of a JSON object/array
        start_idx = -1
        for i, char in enumerate(text):
            if char in ('{', '['):
                start_idx = i
                break
        
        if start_idx == -1:
            return None
        
        test_text = text[start_idx:]
        
        # Strategy 2: Find matching braces/brackets
        depth = 0
        in_string = False
        escape_next = False
        end_idx = -1
        
        for i, char in enumerate(test_text):
            if escape_next:
                escape_next = False
                continue
            
            if char == '\\':
                escape_next = True
                continue
            
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            
            if in_string:
                continue
            
            if char in ('{', '['):
                depth += 1
            elif char in ('}', ']'):
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        
        if end_idx == -1:
            return None
        
        candidate = test_text[:end_idx]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            logger.debug(f"[MCP] Failed to parse extracted JSON candidate: {e}")
            return None

    async def initialize(self) -> None:
        """Initialize MCP connection by spawning the server process."""
        # Guard against double-initialization (e.g. pre-initialized connector injected into PMAgent)
        if self._initialized and self.process and self.process.poll() is None:
            logger.info("MCP connector already initialized — skipping re-initialization")
            return
        try:
            logger.info(f"Starting Azure DevOps MCP server for org: {self.org_name}")
            
            # Prepare environment - the MCP server looks for ADO_MCP_AUTH_TOKEN
            server_env = dict(os.environ)
            server_env["ADO_MCP_AUTH_TOKEN"] = self.pat_token
            # Also set these for compatibility
            server_env["AZURE_DEVOPS_EXT_PAT"] = self.pat_token
            server_env["ADO_PAT"] = self.pat_token
            
            # Build command - use envvar authentication which reads ADO_MCP_AUTH_TOKEN
            # Environment variables are passed via server_env dict to subprocess
            cmd = ["npx", "-y", "@azure-devops/mcp", self.org_name, "-a", "envvar"]
            
            # On Windows, we need shell=True to properly resolve npx from PATH
            # Otherwise subprocess.Popen can't find npx.exe
            import platform
            use_shell = platform.system() == "Windows"
            
            # If using shell on Windows, convert list to string
            if use_shell:
                # Properly quote arguments that might contain spaces
                cmd_str = " ".join(f'"{arg}"' if " " in arg else arg for arg in cmd)
            else:
                cmd_str = cmd
            
            # Spawn the server process
            self.process = subprocess.Popen(
                cmd_str if use_shell else cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=server_env,
                encoding='utf-8',
                errors='replace',
                shell=use_shell,
            )
            
            logger.debug("MCP server process started")
            
            # Give the server time to start up (it downloads/starts npx packages)
            # Increased from 2.0s to 5.0s to allow npx download on first run
            # and to give the server enough time to initialize
            await asyncio.sleep(5.0)
            
            # Check if process died during startup
            poll_result = self.process.poll()
            if poll_result is not None:
                # Process died, capture stderr
                stderr_output = ""
                try:
                    if self.process.stderr:
                        # Read all available stderr
                        stderr_output = self.process.stderr.read()
                except Exception as e:
                    stderr_output = f"Could not read stderr: {e}"
                
                if not stderr_output:
                    stderr_output = "No stderr output available"
                
                logger.error(f"MCP process died during startup with exit code {poll_result}")
                logger.error(f"MCP stderr: {stderr_output}")
                raise RuntimeError(f"MCP process terminated during startup with code {poll_result}: {stderr_output}")
            
            # Initialize the session
            await self._send_initialize_request()
            
            # Load available tools
            await self._load_tools()
            
            self._initialized = True
            logger.info("MCP connection initialized")

        except Exception as e:
            logger.error(f"Failed to initialize MCP: {e}")
            self._initialized = False
            if self.process:
                self.process.terminate()
                self.process = None
            raise

    async def _send_initialize_request(self) -> None:
        """Send initialization request to MCP server."""
        init_request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "pm-agent",
                    "version": "1.0.0",
                },
            },
        }
        
        response = await self._send_request(init_request)
        self.request_id += 1
        
        if "error" in response:
            raise RuntimeError(f"Initialization failed: {response['error']}")
        
        logger.debug("Server initialization successful")

    async def _load_tools(self) -> None:
        """Load available tools from the server."""
        list_tools_request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "tools/list",
            "params": {},
        }
        
        response = await self._send_request(list_tools_request)
        self.request_id += 1
        
        if "error" in response:
            raise RuntimeError(f"Failed to list tools: {response['error']}")
        
        tools = response.get("result", {}).get("tools", [])
        self.tools_cache = {tool["name"]: tool for tool in tools}
        
        logger.info(f"Loaded {len(self.tools_cache)} tools")
        if self.tools_cache:
            tool_names = list(self.tools_cache.keys())[:5]
            logger.debug(f"Sample tools: {tool_names}")

    @property
    def is_initialized(self) -> bool:
        """Check if the MCP connector is initialized and process is alive."""
        if not self._initialized:
            return False
        if not self.process:
            return False
        # Check if process is still alive
        poll_result = self.process.poll()
        if poll_result is not None:
            logger.warning(f"MCP process died with exit code {poll_result}")
            self._initialized = False
            return False
        return True

    async def ensure_initialized(self) -> None:
        """Ensure MCP connector is initialized, reinitialize if needed."""
        if not self.is_initialized:
            logger.info("MCP not initialized or process died, reinitializing...")
            await self.initialize()

    async def _send_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for response.
        
        FIX #7: Uses lock to serialize requests - MCP subprocess stdin/stdout
        cannot handle concurrent requests correctly.
        """
        if not self.process:
            raise RuntimeError("MCP process not initialized")
        
        # FIX #7: Serialize all MCP requests to prevent stdin/stdout interleaving
        # Acquire a lock that is guaranteed to be created in the active event loop
        # to avoid cross-loop binding errors.
        lock = await self._get_request_lock()
        async with lock:
            # Check if subprocess is still alive
            poll_result = self.process.poll()
            if poll_result is not None:
                # Process terminated, try to capture stderr
                stderr_output = ""
                try:
                    stderr_output = self.process.stderr.read() if self.process.stderr else "No stderr available"
                except:
                    stderr_output = "Could not read stderr"
                raise RuntimeError(f"MCP process has terminated with code {poll_result}. Stderr: {stderr_output}")
            
            try:
                # Send request
                request_json = json.dumps(request)
                try:
                    self.process.stdin.write(request_json + "\n")
                    self.process.stdin.flush()
                except OSError as e:
                    raise RuntimeError(f"Failed to write to MCP process stdin (process may have died): {e}")
                
                # FIX #1 & #2: Robust JSON extraction with higher timeouts and better error handling
                # ISSUE: MCP emits mixed JSON + log lines; first valid JSON may come after multiple non-JSON lines
                # SOLUTION: Accumulate output, extract first valid JSON object, use 120s total timeout
                # FIX #9: Fixed timeout calculation - use per-line timeout consistently, don't let remaining go negative
                import time
                loop = asyncio.get_event_loop()
                max_attempts = 100  # Increased from 50 to handle more log lines
                timeout_per_line = 10.0  # Increased from 5.0s to 10.0s per line for better reliability
                total_timeout = 120.0
                accumulated_output = ""
                start_time = time.time()
                
                for attempt in range(max_attempts):
                    elapsed = time.time() - start_time
                    if elapsed > total_timeout:
                        # Log accumulated output for debugging
                        logger.error(f"[MCP] Timeout after {elapsed:.1f}s. Accumulated output ({len(accumulated_output)} chars): {accumulated_output[:500]}")
                        raise RuntimeError(f"Request timed out after {elapsed:.1f}s (total={total_timeout}s). Check MCP server logs.")
                    
                    # FIX #9: Calculate remaining time properly, ensure we always have a valid timeout
                    remaining = max(0.0, total_timeout - elapsed)
                    # Use minimum of per-line timeout or remaining time, but ensure at least 2.0s
                    # This prevents the "0.0s timeout" bug where remaining becomes very small
                    current_timeout = max(2.0, min(timeout_per_line, remaining)) if remaining > 0 else timeout_per_line
                    
                    try:
                        response_line = await asyncio.wait_for(
                            loop.run_in_executor(None, self.process.stdout.readline),
                            timeout=current_timeout,
                        )
                    except asyncio.TimeoutError:
                        # On timeout, check if we have accumulated valid JSON
                        parsed = self._extract_first_valid_json(accumulated_output)
                        if parsed:
                            logger.info(f"[MCP] Extracted JSON from buffer after line timeout (attempt {attempt})")
                            return parsed
                        # No valid JSON yet, continue to next attempt unless we're out of time
                        if elapsed >= total_timeout:
                            logger.error(f"[MCP] Total timeout reached after {elapsed:.1f}s, attempt {attempt+1}/{max_attempts}. Accumulated: {len(accumulated_output)} chars")
                            logger.debug(f"[MCP] Accumulated output: {accumulated_output[:300]}")
                            raise RuntimeError(f"Request timed out after {elapsed:.1f}s waiting for response. No valid JSON found in {len(accumulated_output)} chars of output.")
                        # Still have time, continue
                        logger.debug(f"[MCP] Line timeout at {elapsed:.1f}s, attempt {attempt+1}/{max_attempts}, continuing...")
                        continue
                    
                    if not response_line:
                        # EOF reached
                        parsed = self._extract_first_valid_json(accumulated_output)
                        if parsed:
                            logger.info(f"[MCP] Extracted JSON from buffer after EOF")
                            return parsed
                        raise RuntimeError("Server closed connection")
                    
                    accumulated_output += response_line
                    response_line = response_line.strip()
                    if not response_line:
                        continue
                    
                    try:
                        response = json.loads(response_line)
                        logger.debug(f"[MCP] Found JSON on attempt {attempt+1}")
                        return response
                    except json.JSONDecodeError:
                        logger.debug(f"[MCP] Line {attempt+1}: Not JSON - {response_line[:80]}...")
                        continue
                
                # Exhausted max_attempts
                parsed = self._extract_first_valid_json(accumulated_output)
                if parsed:
                    logger.info(f"[MCP] Extracted JSON from buffer after {max_attempts} attempts")
                    return parsed
                raise RuntimeError(f"No valid JSON after {max_attempts} attempts. Check MCP server output.")
                
            except asyncio.TimeoutError:
                raise RuntimeError("Request timed out waiting for server response")
            except Exception as e:
                raise RuntimeError(f"Failed to send request: {e}")

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Call an MCP tool.

        Args:
            tool_name: Name of the tool to invoke
            arguments: Tool arguments

        Returns:
            Tool result as string
        """
        # Ensure MCP is initialized before calling tool
        await self.ensure_initialized()
        
        if not self.process:
            raise RuntimeError("MCP process not initialized")

        call_tool_request = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        
        # FIX #2: Increase timeout for large payloads (work_get_iteration_work_items with nested relations)
        try:
            response = await asyncio.wait_for(
                self._send_request(call_tool_request),
                timeout=120.0  # Increased from 90s to 120s to handle large iteration work items
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"Tool call '{tool_name}' timed out after 120 seconds")
        except asyncio.CancelledError:
            # FIX #32: CancelledError is BaseException in Python 3.9+; convert to RuntimeError
            # so callers with `except Exception` can handle it gracefully
            raise RuntimeError(f"Tool call '{tool_name}' was cancelled (possible connection issue)")
        
        self.request_id += 1
        
        if "error" in response:
            raise RuntimeError(f"Tool call failed: {response['error']}")
        
        # Extract text from response
        result = response.get("result", {})
        if isinstance(result, dict) and "content" in result:
            content_list = result["content"]
            is_error = result.get("isError", False)
            
            # Collect all text content parts
            text_parts = []
            for content_item in content_list:
                if isinstance(content_item, dict):
                    if content_item.get("type") == "text":
                        text_parts.append(content_item.get("text", ""))
                    elif "text" in content_item:
                        # Some MCP responses have text without explicit type
                        text_parts.append(content_item["text"])
            
            if text_parts:
                combined_text = "\n".join(text_parts)
                if is_error:
                    logger.warning(f"[MCP] Tool {tool_name} returned error content: {combined_text[:500]}")
                    raise RuntimeError(f"Tool call '{tool_name}' returned error: {combined_text}")
                return combined_text
            
            # No text found in content items — check if there's isError flag
            if is_error:
                try:
                    error_detail = json.dumps(content_list, indent=2)[:1000]
                except Exception:
                    error_detail = str(content_list)[:1000]
                logger.warning(f"[MCP] Tool {tool_name} returned error with non-text content: {error_detail}")
                raise RuntimeError(f"Tool call '{tool_name}' returned error: {error_detail}")

        # If we reached here, there was no textual content; dump the raw result for debugging
        try:
            pretty = json.dumps(result, indent=2)
        except Exception:
            pretty = str(result)
        logger.debug(f"Tool {tool_name} returned non-text result: {pretty}")
        return pretty

    async def execute_query(self, prompt: str) -> str:
        """Execute a natural language query. Use search_workitem with SEARCH TEXT PREFIXES.
        
        Azure DevOps Search API works best with search text prefixes:
        - s:<state> for state (e.g., s:New, s:Active)
        - area:"<area path>" for area path (quotes for multi-word)
        - t:<type> for work item type (e.g., t:Bug, t:"User Story")
        - a:<email> for assigned to
        """
        if not self.process:
            raise RuntimeError("MCP process not initialized")

        logger.debug(f"Processing query: {prompt}")
        
        from config import config as app_config
        prompt_lower = prompt.lower()
        project = app_config.ado_project

        # ══════════════════════════════════════════════════════════════════
        # PRIORITY 1: Check for specific work item ID in the query
        # ══════════════════════════════════════════════════════════════════
        # Match patterns like "73944", "#73944", "WI 73944", "work item 73944", "bug 73944", "details of 73944"
        id_match = re.search(r'(?:^|[^\d])(\d{4,6})(?:[^\d]|$)', prompt)
        if id_match:
            work_item_id = int(id_match.group(1))
            logger.info(f"Detected specific work item ID: {work_item_id}")
            try:
                # Use wit_get_work_item to fetch the specific item
                # MCP schema requires: id (number), project (string)
                if "wit_get_work_item" in self.tools_cache:
                    result = await self.call_tool("wit_get_work_item", {
                        "id": work_item_id,
                        "project": project
                    })
                    if result and result.strip() and result.strip() != 'null':
                        return result
            except Exception as e:
                logger.warning(f"wit_get_work_item failed for ID {work_item_id}: {e}")
                # Fall through to search

        # Try specific tools for non-work-item queries
        if "project" in prompt_lower and "list" in prompt_lower:
            return await self.call_tool("core_list_projects", {})
        elif "repo" in prompt_lower or "repository" in prompt_lower:
            return await self.call_tool("repo_list_repos_by_project", {"project": project})
        elif "build" in prompt_lower:
            return await self.call_tool("pipelines_get_builds", {"project": project})
        elif "test plan" in prompt_lower:
            return await self.call_tool("testplan_list_test_plans", {"project": project})
        elif "team" in prompt_lower:
            return await self.call_tool("core_list_project_teams", {"project": project})
        elif "iteration" in prompt_lower and "work" not in prompt_lower:
            return await self.call_tool("work_list_iterations", {"project": project})

        # For work-item queries: build searchText with PREFIXES (not structured filters)
        if "search_workitem" in self.tools_cache:
            search_parts = []
            
            # ══════════════════════════════════════════════════════════════════
            # Extract iteration/sprint and add iteration: prefix
            # ══════════════════════════════════════════════════════════════════
            # Match patterns like "sprint 25.25", "iteration 25.25", "in sprint 25.25"
            iteration_match = re.search(r'(?:sprint|iteration)\s*(\d+(?:\.\d+)?)', prompt, re.IGNORECASE)
            if iteration_match:
                iteration_num = iteration_match.group(1)
                # ADO iteration path format varies; try common patterns
                search_parts.append(f'iteration:"Sprint {iteration_num}"')
                logger.debug(f"Extracted iteration: Sprint {iteration_num}")
            
            # Extract state - use centralized config for known states
            from config import config as _cfg
            _all_known = _cfg.get_all_known_states()
            for known_state in _all_known:
                if known_state.lower() in prompt_lower:
                    search_parts.append(f"s:{known_state}")
                    logger.debug(f"Extracted state: {known_state}")
                    break
            
            # Extract area path - capture multi-word names
            # Match patterns like "area path XOPS Bugs Enhancement" or "in area XOPS"
            area_match = re.search(r"area\s+(?:path\s+)?([A-Za-z0-9][\w\s]*?)(?:\s+(?:for|in|on|and|with|state|assigned)|$)", prompt, re.IGNORECASE)
            if area_match:
                area_name = area_match.group(1).strip()
                # Use quotes for multi-word area paths
                if " " in area_name:
                    search_parts.append(f'area:"{area_name}"')
                else:
                    search_parts.append(f"area:{area_name}")
                logger.debug(f"Extracted area path: {area_name}")
            
            # Extract work item type and add t: prefix
            type_patterns = [
                ("user stor", '"User Story"'),
                ("bug", "Bug"),
                ("enhancement", "Enhancement"),
                ("task", "Task"),
                ("feature", "Feature"),
            ]
            for pattern, wi_type in type_patterns:
                if pattern in prompt_lower:
                    search_parts.append(f"t:{wi_type}")
                    logger.debug(f"Extracted work item type: {wi_type}")
                    break
            
            # Extract assignee and add a: prefix
            assignee_match = re.search(r"assigned\s+to\s+([^\s,]+@[^\s,]+)", prompt, re.IGNORECASE)
            if assignee_match:
                assignee = assignee_match.group(1).strip()
                search_parts.append(f"a:{assignee}")
                logger.debug(f"Extracted assignee: {assignee}")
            
            # Build final searchText
            if search_parts:
                search_text = " ".join(search_parts)
            else:
                # Fallback: use keywords from the prompt
                search_text = prompt
            
            args = {
                "searchText": search_text,
                "project": [project]
            }
            
            logger.info(f"Calling search_workitem with searchText: {search_text}")
            try:
                res = await self.call_tool("search_workitem", args)
                if res and res.strip() and res.strip() != 'null':
                    return res
            except Exception as e:
                logger.warning(f"search_workitem failed: {e}")

        # Fallback: try to call wit_my_work_items
        try:
            return await self.call_tool("wit_my_work_items", {"project": project})
        except Exception as e:
            logger.warning(f"wit_my_work_items fallback failed: {e}")
            return f"[ERROR] Could not process query: {prompt}. No suitable tool found."

    async def cleanup(self) -> None:
        """Clean up MCP resources."""
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
                logger.debug("MCP process terminated")
            except subprocess.TimeoutExpired:
                self.process.kill()
                logger.debug("MCP process killed (timeout)")
            except Exception as e:
                logger.warning(f"Error closing MCP process: {e}")
            finally:
                self.process = None


async def get_mcp_connector() -> MCPConnector:
    """
    Factory function to create and initialize an MCP connector.

    Returns:
        Initialized MCPConnector instance
    """
    from config import config as app_config
    # Extract org name from ADO_ORG_URL
    ado_org_url = app_config.ado_org_url
    org_name = ado_org_url.split("/")[-1].strip()

    # Get PAT token
    pat_token = app_config.ado_pat

    if not pat_token:
        raise RuntimeError("ADO_PAT not configured in config.yaml")

    logger.info(f"Creating MCP connector for org: {org_name}")

    connector = MCPConnector(org_name=org_name, pat_token=pat_token)
    await connector.initialize()

    return connector
