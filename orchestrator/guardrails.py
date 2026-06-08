"""
Guardrails - Input validation and safety layer for the orchestrator.

This module provides:
1. Input validation (length, encoding, format)
2. Prompt injection detection
3. PII detection and masking (optional)
4. Content policy enforcement
5. Rate limiting signals

Architecture Position:
    User Query → Controller → Orchestrator → [GUARDRAILS] → Routing Engine → Agents

All queries pass through guardrails BEFORE routing to ensure safety.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple
from enum import Enum

logger = logging.getLogger("orchestrator.guardrails")


class GuardrailAction(Enum):
    """Actions guardrails can take."""
    ALLOW = "allow"          # Query is safe, proceed
    BLOCK = "block"          # Query is blocked, return error
    SANITIZE = "sanitize"    # Query sanitized, proceed with modified query
    WARN = "warn"            # Query allowed but flagged for monitoring


@dataclass
class GuardrailResult:
    """Result from guardrail check."""
    action: GuardrailAction
    original_query: str
    sanitized_query: Optional[str] = None
    blocked_reason: Optional[str] = None
    warnings: List[str] = None
    risk_score: float = 0.0  # 0.0 = safe, 1.0 = high risk
    
    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []
        if self.sanitized_query is None:
            self.sanitized_query = self.original_query


class Guardrails:
    """
    Input validation and safety layer.
    
    Checks all incoming queries for:
    - Prompt injection attempts
    - Excessive length
    - Malformed content
    - Potential security risks
    """
    
    # Configuration
    MAX_QUERY_LENGTH = 5000  # Characters
    MIN_QUERY_LENGTH = 2     # Characters
    
    # Prompt injection patterns
    INJECTION_PATTERNS = [
        # System prompt overrides
        r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)",
        r"disregard\s+(all\s+)?instructions?",
        r"forget\s+(everything|all|your\s+instructions?)",
        r"you\s+are\s+now\s+(a\s+)?different",
        r"act\s+as\s+if\s+you\s+(have\s+no|don'?t\s+have)",
        r"pretend\s+(that\s+)?you\s+(are|have)\s+no\s+(restrictions?|rules?)",
        
        # Role manipulation
        r"you\s+are\s+(DAN|jailbreak|evil|unrestricted)",
        r"enter\s+(DAN|developer|admin)\s+mode",
        r"unlock\s+(hidden|secret|admin)\s+(mode|features?)",
        
        # Instruction extraction
        r"(show|reveal|display|print)\s+(your|the|system)\s+(prompt|instructions?)",
        r"what\s+(are|were)\s+your\s+(original\s+)?instructions?",
        r"repeat\s+(your|the)\s+system\s+(prompt|message)",
        
        # Code injection attempts
        r"```\s*(python|bash|shell|cmd|powershell)",
        r"import\s+os\s*[;\n]",
        r"eval\s*\(",
        r"exec\s*\(",
        r"subprocess\s*\.",
    ]
    
    # Sensitive patterns to mask
    SENSITIVE_PATTERNS = [
        # API keys / tokens
        (r"(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{20,})['\"]?", "***REDACTED***"),
        # Personal access tokens
        (r"(pat|bearer)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{30,})['\"]?", "***REDACTED***"),
    ]
    
    def __init__(self, strict_mode: bool = False):
        """
        Initialize guardrails.
        
        Args:
            strict_mode: If True, blocks more aggressively. Default False.
        """
        self.strict_mode = strict_mode
        self._compile_patterns()
        logger.info(f"Guardrails initialized (strict_mode={strict_mode})")
    
    def _compile_patterns(self):
        """Pre-compile regex patterns for performance."""
        self.injection_regexes = [
            re.compile(p, re.IGNORECASE) for p in self.INJECTION_PATTERNS
        ]
        self.sensitive_regexes = [
            (re.compile(p, re.IGNORECASE), repl) for p, repl in self.SENSITIVE_PATTERNS
        ]
    
    def check(self, query: str, session_id: Optional[str] = None) -> GuardrailResult:
        """
        Run all guardrail checks on a query.
        
        Args:
            query: User query to validate
            session_id: Optional session ID for rate limiting
            
        Returns:
            GuardrailResult with action and details
        """
        warnings = []
        risk_score = 0.0
        sanitized = query
        
        # Check 1: Empty or too short
        if not query or len(query.strip()) < self.MIN_QUERY_LENGTH:
            return GuardrailResult(
                action=GuardrailAction.BLOCK,
                original_query=query,
                blocked_reason="Query is too short or empty",
                risk_score=0.0
            )
        
        # Check 2: Too long
        if len(query) > self.MAX_QUERY_LENGTH:
            if self.strict_mode:
                return GuardrailResult(
                    action=GuardrailAction.BLOCK,
                    original_query=query,
                    blocked_reason=f"Query exceeds maximum length ({len(query)} > {self.MAX_QUERY_LENGTH})",
                    risk_score=0.3
                )
            else:
                # Truncate in non-strict mode
                sanitized = query[:self.MAX_QUERY_LENGTH]
                warnings.append(f"Query truncated from {len(query)} to {self.MAX_QUERY_LENGTH} characters")
                risk_score = max(risk_score, 0.2)
        
        # Check 3: Prompt injection detection
        injection_detected, injection_pattern = self._detect_injection(query)
        if injection_detected:
            risk_score = max(risk_score, 0.8)
            if self.strict_mode:
                logger.warning(f"[GUARDRAILS] Blocked prompt injection attempt: {query[:100]}...")
                return GuardrailResult(
                    action=GuardrailAction.BLOCK,
                    original_query=query,
                    blocked_reason="Potential prompt injection detected",
                    risk_score=risk_score
                )
            else:
                warnings.append("Potential prompt injection pattern detected")
                logger.warning(f"[GUARDRAILS] Warning: prompt injection pattern in query: {injection_pattern}")
        
        # Check 4: Sanitize sensitive data
        sanitized, masked_count = self._mask_sensitive(sanitized)
        if masked_count > 0:
            warnings.append(f"Masked {masked_count} sensitive pattern(s)")
            risk_score = max(risk_score, 0.3)
        
        # Determine final action
        if warnings and self.strict_mode:
            action = GuardrailAction.WARN
        elif sanitized != query:
            action = GuardrailAction.SANITIZE
        else:
            action = GuardrailAction.ALLOW
        
        return GuardrailResult(
            action=action,
            original_query=query,
            sanitized_query=sanitized,
            warnings=warnings,
            risk_score=risk_score
        )
    
    def _detect_injection(self, query: str) -> Tuple[bool, Optional[str]]:
        """
        Detect prompt injection attempts.
        
        Returns:
            Tuple of (detected: bool, matched_pattern: str or None)
        """
        for regex in self.injection_regexes:
            match = regex.search(query)
            if match:
                return (True, match.group(0))
        return (False, None)
    
    def _mask_sensitive(self, query: str) -> Tuple[str, int]:
        """
        Mask sensitive information in query.
        
        Returns:
            Tuple of (sanitized_query, number_of_replacements)
        """
        result = query
        total_count = 0
        
        for regex, replacement in self.sensitive_regexes:
            result, count = regex.subn(replacement, result)
            total_count += count
        
        return (result, total_count)
    
    def is_safe(self, query: str) -> bool:
        """
        Quick check if query is safe to process.
        
        Returns:
            True if query passes all checks
        """
        result = self.check(query)
        return result.action in (GuardrailAction.ALLOW, GuardrailAction.SANITIZE, GuardrailAction.WARN)


# Singleton instance
_guardrails: Optional[Guardrails] = None


def get_guardrails(strict_mode: bool = False) -> Guardrails:
    """Get or create guardrails singleton."""
    global _guardrails
    if _guardrails is None:
        _guardrails = Guardrails(strict_mode=strict_mode)
    return _guardrails
