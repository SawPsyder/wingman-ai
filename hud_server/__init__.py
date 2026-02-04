"""
HUD Server - Integrated HTTP server for HUD overlay control.

This server provides a REST API to control HUD overlays from any client.
It runs independently and can be used by multiple applications simultaneously.

Included modules:
- server.py: FastAPI HTTP server
- hud_manager.py: State management for HUD groups
- http_client.py: HTTP client for skills to use
- models.py: Pydantic models for API requests/responses
- overlay/overlay.py: PIL-based overlay renderer (Windows)
- rendering/markdown.py: Markdown rendering
- platform/win32.py: Win32 API definitions
- layout/manager.py: Automatic window layout and positioning
"""

from hud_server.server import HudServer
from hud_server.http_client import HudHttpClient, HudHttpClientSync
from hud_server.models import (
    HudServerSettings,
    GroupState,
    MessageRequest,
    ChatMessageRequest,
    ProgressRequest,
    TimerRequest,
    ItemRequest,
    StateRestoreRequest,
    HealthResponse,
    GroupStateResponse,
)

__all__ = [
    "HudServer",
    "HudHttpClient",
    "HudHttpClientSync",
    "HudServerSettings",
    "GroupState",
    "MessageRequest",
    "ChatMessageRequest",
    "ProgressRequest",
    "TimerRequest",
    "ItemRequest",
    "StateRestoreRequest",
    "HealthResponse",
    "GroupStateResponse",
]

