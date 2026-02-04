# HUD Server

The HUD Server is an integrated HTTP server that provides a REST API for controlling transparent overlay windows. It enables real-time display of messages, information panels, progress bars, timers, and chat windows on screen.

## Overview

The HUD Server runs as part of Wingman AI Core and can be enabled in the global settings. Once enabled, it listens on `http://127.0.0.1:7862` (configurable) and accepts HTTP requests to control HUD elements.

### Key Features

- **Multiple HUD Groups**: Support for independent HUD areas (messages, persistent info, chat windows)
- **Automatic Layout**: Smart positioning with anchor-based stacking to prevent overlapping
- **Rich Content**: Markdown support with syntax highlighting, images, and emoji
- **Visual Effects**: Typewriter animations, fade transitions, and loading indicators
- **State Persistence**: Save and restore HUD state across sessions
- **Thread-Safe**: Designed for concurrent access from multiple clients

## Architecture

```
hud_server/
├── __init__.py           # Package exports
├── server.py             # FastAPI HTTP server with REST endpoints
├── hud_manager.py        # State management for all HUD groups
├── http_client.py        # Async/sync HTTP client for skills
├── models.py             # Pydantic models for API requests/responses
├── layout/               # Automatic window positioning
│   ├── manager.py        # Layout manager for anchor-based stacking
│   └── README.md         # Layout system documentation
├── overlay/              # Windows overlay rendering
│   └── overlay.py        # PIL-based transparent window renderer
├── rendering/            # Content rendering
│   └── markdown.py       # Markdown-to-image renderer
├── platform/             # Platform-specific code
│   └── win32.py          # Windows API bindings
└── tests/                # Test suites
    └── README.md         # Test documentation
```

## Configuration

Enable the HUD Server in Wingman AI global settings:

```yaml
hud_server:
  enabled: true
  host: "127.0.0.1"    # Use "0.0.0.0" for LAN access
  port: 7862
  framerate: 60
  layout_margin: 20
  layout_spacing: 15
```

## API Endpoints

### Health & Status

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Check server health and list active groups |
| GET | `/` | Same as `/health` |

### Groups

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/groups` | Create or update a HUD group |
| PUT/PATCH | `/groups/{name}` | Update group properties |
| DELETE | `/groups/{name}` | Delete a group |
| GET | `/groups` | List all group names |

### Messages

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/message` | Show a message in a group |
| POST | `/message/append` | Append content (for streaming) |
| POST | `/message/hide/{group}` | Hide the current message |

### Items (Persistent Info)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/items` | Add a persistent item |
| PUT | `/items` | Update an existing item |
| DELETE | `/items/{group}/{title}` | Remove an item |
| DELETE | `/items/{group}` | Clear all items in a group |

### Progress & Timers

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/progress` | Show/update a progress bar |
| POST | `/timer` | Show a countdown timer |
| POST | `/loader` | Show/hide loading animation |

### Chat Windows

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/chat/window` | Create a chat window |
| DELETE | `/chat/window/{name}` | Delete a chat window |
| POST | `/chat/message` | Send a message to a chat window |
| DELETE | `/chat/messages/{name}` | Clear chat messages |
| POST | `/chat/show/{name}` | Show a hidden chat window |
| POST | `/chat/hide/{name}` | Hide a chat window |

### State Persistence

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/state/{group}` | Get group state for persistence |
| POST | `/state/restore` | Restore a group's state |

## Usage Examples

### Python (Async)

```python
from hud_server.http_client import HudHttpClient

async with HudHttpClient() as client:
    # Create a group with custom properties
    await client.create_group("my_wingman", props={
        "anchor": "top_left",
        "priority": 20,
        "width": 400,
        "bg_color": "#1e212b",
        "opacity": 0.85
    })
    
    # Show a message
    await client.show_message(
        group_name="my_wingman",
        title="Assistant",
        content="Hello! How can I help you?",
        color="#00aaff"
    )
    
    # Show a progress bar
    await client.show_progress(
        group_name="my_wingman",
        title="Download",
        current=50,
        maximum=100,
        description="Downloading file..."
    )
```

### Python (Sync)

```python
from hud_server.http_client import HudHttpClientSync

with HudHttpClientSync() as client:
    client.show_message("my_group", "Title", "Content")
```

### HTTP (cURL)

```bash
# Create a group
curl -X POST http://127.0.0.1:7862/groups \
  -H "Content-Type: application/json" \
  -d '{"group_name": "test", "props": {"anchor": "top_right"}}'

# Show a message
curl -X POST http://127.0.0.1:7862/message \
  -H "Content-Type: application/json" \
  -d '{"group_name": "test", "title": "Hello", "content": "World!"}'
```

## Group Properties

When creating or updating groups, you can specify these properties:

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `anchor` | string | `"top_left"` | Screen position anchor |
| `priority` | int | `10` | Stacking priority (higher = closer to anchor) |
| `layout_mode` | string | `"auto"` | `"auto"`, `"manual"`, or `"hybrid"` |
| `width` | int | `400` | Window width in pixels |
| `max_height` | int | `600` | Maximum window height |
| `x`, `y` | int | `20` | Manual position (when layout_mode="manual") |
| `bg_color` | string | `"#1e212b"` | Background color |
| `text_color` | string | `"#f0f0f0"` | Text color |
| `accent_color` | string | `"#00aaff"` | Accent/title color |
| `opacity` | float | `0.85` | Window opacity (0.0-1.0) |
| `border_radius` | int | `12` | Corner roundness |
| `font_size` | int | `16` | Text size |
| `font_family` | string | `"Segoe UI"` | Font name |
| `typewriter_effect` | bool | `true` | Animate text appearance |
| `auto_fade` | bool | `true` | Auto-hide after delay |
| `fade_delay` | float | `8.0` | Seconds before fade starts |

## Layout System

The layout system automatically positions HUD windows to prevent overlap:

- **9 Anchor Points**: `top_left`, `top_center`, `top_right`, `left_center`, `center`, `right_center`, `bottom_left`, `bottom_center`, `bottom_right`
- **Priority Stacking**: Higher priority windows appear closer to the anchor
- **Dynamic Reflow**: Windows reposition when content height changes

See `layout/README.md` for detailed documentation.

## Running Tests

```bash
# Quick integration test
python -m hud_server.tests.run_tests

# Run specific test suites
python -m hud_server.tests.run_tests --messages
python -m hud_server.tests.run_tests --progress
python -m hud_server.tests.run_tests --chat
python -m hud_server.tests.run_tests --layout

# Run all tests
python -m hud_server.tests.run_tests --all
```

## API Documentation

When the server is running, interactive API documentation is available at:

- Swagger UI: `http://127.0.0.1:7862/docs`
- ReDoc: `http://127.0.0.1:7862/redoc`

## Dependencies

Required:
- FastAPI
- Uvicorn
- httpx
- Pydantic

Optional (for overlay rendering):
- Pillow (PIL)
- pywin32 (Windows only)
