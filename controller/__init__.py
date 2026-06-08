"""
Controller package - Thin entry points for UI → Orchestrator
"""

from .chatbot_controller import ChatbotController, get_controller

__all__ = ["ChatbotController", "get_controller"]
