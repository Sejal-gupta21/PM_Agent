from typing import Any
import json


class A2AStarletteApplication:
    """Minimal ASGI application wrapper used by the Host Agent.

    The real project uses Starlette/FastAPI to build a richer HTTP
    surface, but for local integration tests we only need a simple
    ASGI app so `uvicorn` can start and serve health checks.
    """

    def __init__(self, agent_card: Any, http_handler: Any):
        self.agent_card = agent_card
        self.http_handler = http_handler

    def build(self):
        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            path = scope.get("path", "/")
            method = scope.get("method", "GET").upper()
            if path == "/" or path == "/health":
                body = b"OK"
                await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": body})
                return

            # Delegate POST /task to the provided http_handler
            if path == "/task" and method == "POST":
                try:
                    body_bytes = b""
                    while True:
                        message = await receive()
                        if message["type"] == "http.request":
                            body_bytes += message.get("body", b"") or b""
                            if not message.get("more_body", False):
                                break

                    try:
                        payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else None
                    except Exception:
                        payload = body_bytes.decode("utf-8", errors="ignore")

                    result = await self.http_handler.handle_request(payload)
                    resp_text = json.dumps(result)
                    resp_bytes = resp_text.encode("utf-8")
                    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json; charset=utf-8")]})
                    await send({"type": "http.response.body", "body": resp_bytes})
                    return
                except Exception as exc:  # pragma: no cover - surface errors to caller
                    err = str(exc).encode("utf-8")
                    await send({"type": "http.response.start", "status": 500, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                    await send({"type": "http.response.body", "body": err})
                    return

            # Default reply
            body = b"Not Implemented"
            await send({"type": "http.response.start", "status": 501, "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})

        return app
