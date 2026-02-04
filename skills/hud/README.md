# HUD Skill

Display messages, information panels, progress bars, and timers on a transparent overlay.

## Overview

The HUD Skill integrates with the [HUD Server](/hud_server/README.md) to display visual elements on screen. It automatically shows conversation messages (user and assistant), and provides tools for the AI to display persistent information, progress bars, and countdown timers.

## Prerequisites

The HUD Server must be enabled in Wingman AI global settings:

1. Open Wingman AI settings
2. Navigate to **HUD Server** section
3. Enable the HUD Server
4. (Optional) Adjust port, framerate, and layout settings

## Features

### Automatic Chat Display

When enabled, the skill automatically displays:
- **User messages** with configurable color (default: green)
- **Assistant responses** with accent color (default: blue)
- **Tool calls** showing which skills/tools are being used
- **Loading indicator** while processing

Messages are synchronized with audio playback - they hide when the assistant finishes speaking.

### AI-Controllable Tools

The skill provides these tools that the AI can use:

| Tool | Description |
|------|-------------|
| `hud_add_info` | Add a persistent information panel |
| `hud_update_info` | Update an existing info panel |
| `hud_remove_info` | Remove an info panel |
| `hud_list_info` | List all active info panels |
| `hud_clear_all` | Remove all HUD elements |
| `hud_show_progress` | Show a progress bar |
| `hud_update_progress` | Update progress bar values |
| `hud_show_timer` | Show a countdown timer |

### Persistence

Info panels and progress bars are saved to disk and restored when Wingman AI restarts (configurable).

## Configuration

All settings are available as custom properties in the skill configuration:

### Visual Settings

| Property | Default | Description |
|----------|---------|-------------|
| `user_color` | `#4cd964` | Color for user messages (green) |
| `accent_color` | `#00aaff` | Color for assistant messages (blue) |
| `bg_color` | `#1e212b` | Background color |
| `text_color` | `#f0f0f0` | Main text color |
| `opacity` | `0.85` | Window transparency (0.0-1.0) |
| `border_radius` | `12` | Corner roundness in pixels |
| `content_padding` | `16` | Padding inside windows |
| `font_size` | `16` | Text size in pixels |
| `font_family` | `Segoe UI` | Font name |

### Layout Settings

| Property | Default | Description |
|----------|---------|-------------|
| `chat_anchor` | `top_left` | Screen position for chat messages |
| `chat_priority` | `20` | Stacking priority for chat window |
| `hud_width` | `400` | Chat window width in pixels |
| `hud_max_height` | `600` | Maximum chat window height |
| `persistent_anchor` | `top_left` | Screen position for info panels |
| `persistent_priority` | `10` | Stacking priority for info panels |
| `persistent_width` | `400` | Info panel width in pixels |

### Behavior Settings

| Property | Default | Description |
|----------|---------|-------------|
| `max_display_time` | `5` | Seconds to wait for audio before auto-hide |
| `typewriter_effect` | `true` | Animate text character-by-character |
| `restore_persistent_items` | `true` | Restore items on restart |
| `show_chat_messages` | `true` | Display conversation messages |
| `display_tool_names` | `false` | Show function names instead of skill names |

### Anchor Positions

Available anchor positions:
- `top_left`, `top_center`, `top_right`
- `left_center`, `center`, `right_center`
- `bottom_left`, `bottom_center`, `bottom_right`

Higher priority windows appear closer to the anchor point.

## Example Usage

### Display System Status

Ask the AI: *"Show my current system status on the HUD"*

The AI might use:
```
hud_add_info(
    title="System Status",
    description_markdown="**CPU:** 45%\n**RAM:** 8.2 GB\n**Disk:** 234 GB free",
    duration=30  # Auto-remove after 30 seconds
)
```

### Track Progress

Ask the AI: *"Show a progress bar for my download"*

The AI might use:
```
hud_show_progress(
    title="Download Progress",
    current=0,
    maximum=100,
    description_markdown="Downloading update...",
    auto_close=True  # Remove when complete
)
```

### Set a Timer

Ask the AI: *"Set a 5-minute timer for my break"*

The AI might use:
```
hud_show_timer(
    title="Break Timer",
    duration_seconds=300,
    description_markdown="Time for a break!",
    color="#00ff00"
)
```

## Markdown Support

Info panels and descriptions support Markdown:

- **Bold** and *italic* text
- `Code blocks`
- Bullet lists
- Numbered lists
- Headers (h1-h6)
- Images
- Emoji 🎉

## Troubleshooting

### HUD not appearing

1. Verify HUD Server is enabled in global settings
2. Check that the server started (look for "HUD Server started" in logs)
3. Ensure the skill is activated for your wingman

### Messages not hiding

The skill waits for audio playback to finish. If no audio plays:
- Messages auto-hide after `max_display_time` seconds
- Use a shorter timeout if needed

### Multiple wingmen overlapping

Each wingman gets its own HUD groups. Adjust `chat_priority` and `persistent_priority` to control stacking order. Higher priority = closer to screen edge.

## Files

```
skills/hud/
├── main.py              # Skill implementation
├── default_config.yaml  # Default configuration
├── logo.png             # Skill icon
└── README.md            # This file
```

## Data Storage

Persistent items are stored in:
- **Windows**: `%APPDATA%/ShipBit/WingmanAI/[version]/skills/hud/data/`
- **macOS**: `~/Library/Application Support/WingmanAI/skills/hud/data/`

Each wingman has its own persistence file: `persistent_info_{wingman_name}.json`
