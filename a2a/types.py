from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class AgentCard:
    name: str = "local-agent"
    description: str = "Local development agent"
    base_url: Optional[str] = None
    skills: Optional[List[Any]] = None
    capabilities: Optional[Any] = None
    url: Optional[str] = None
    version: Optional[str] = None
    default_input_modes: Optional[List[str]] = None
    default_output_modes: Optional[List[str]] = None
    
    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        result = {}
        for k, v in self.__dict__.items():
            if v is None and exclude_none:
                continue
            # serialize skills if they provide model_dump
            if k == 'skills' and v is not None:
                serialized = []
                for s in v:
                    if hasattr(s, 'model_dump'):
                        serialized.append(s.model_dump(exclude_none=exclude_none))
                    else:
                        serialized.append(getattr(s, '__dict__', s))
                result[k] = serialized
                continue
            result[k] = v
        return result


@dataclass
class TextPart:
    text: str


@dataclass
class Part:
    root: TextPart


@dataclass
class Message:
    role: str
    message_id: str
    parts: List[Part]


@dataclass
class MessageSendParams:
    message: Message


@dataclass
class SendMessageRequest:
    id: str
    params: MessageSendParams


class DummyModel:
    """Simple object exposing a `model_dump` method similar to pydantic.

    Used for responses returned by the local `A2AClient` shim so calling
    code can call `model_dump(mode="json", exclude_none=True)`.
    """

    def __init__(self, payload: Any):
        self._payload = payload

    def model_dump(self, mode: str = "json", exclude_none: bool = True):
        return self._payload


@dataclass
class AgentSkill:
    name: str
    id: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    examples: Optional[List[str]] = None


@dataclass
class AgentCapabilities:
    streaming: bool = False
    multi_turn: bool = False


class TaskState:
    working = "working"
    completed = "completed"
    failed = "failed"
