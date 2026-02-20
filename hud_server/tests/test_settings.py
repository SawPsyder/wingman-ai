"""
Test Settings - Test dynamic settings updates on running HUD server.
"""

import asyncio
from hud_server.tests.test_session import TestSession


# =============================================================================
# Test Cases
# =============================================================================

async def test_framerate_update(session: TestSession, delay: float = 2.0):
    """Test updating framerate while server is running."""
    print(f"[{session.name}] Testing framerate update...")

    # Draw some content first
    await session.draw_assistant_message("Testing framerate update")
    await asyncio.sleep(delay)

    # Show loader, update framerate, hide loader
    await session.set_loading(True)
    await session.update_settings(framerate=30)
    await asyncio.sleep(1)
    await session.set_loading(False)

    # Draw more content - should still work
    await session.draw_assistant_message("Content after framerate change to 30")
    await asyncio.sleep(delay)

    # Update framerate again with loader
    await session.set_loading(True)
    await session.update_settings(framerate=120)
    await asyncio.sleep(1)
    await session.set_loading(False)

    await session.draw_assistant_message("Content after framerate change to 120")
    await asyncio.sleep(delay)

    await session.hide()
    print(f"[{session.name}] Framerate update test complete")


async def test_layout_margin_update(session: TestSession, delay: float = 2.0):
    """Test updating layout_margin while server is running."""
    print(f"[{session.name}] Testing layout_margin update...")

    # Draw messages at different positions
    await session.draw_assistant_message("First message at default margin")
    await asyncio.sleep(delay)

    # Update margin
    await session.update_settings(layout_margin=50)
    await asyncio.sleep(1)

    await session.draw_assistant_message("Second message at 50px margin")
    await asyncio.sleep(delay)

    # Update margin again
    await session.update_settings(layout_margin=10)
    await asyncio.sleep(1)

    await session.draw_assistant_message("Third message at 10px margin")
    await asyncio.sleep(delay)

    await session.hide()
    print(f"[{session.name}] Layout margin update test complete")


async def test_layout_spacing_update(session: TestSession, delay: float = 2.0):
    """Test updating layout_spacing while server is running."""
    print(f"[{session.name}] Testing layout_spacing update...")

    # Draw multiple messages
    await session.draw_assistant_message("Message 1")
    await asyncio.sleep(0.5)
    await session.draw_assistant_message("Message 2")
    await asyncio.sleep(0.5)
    await session.draw_assistant_message("Message 3")
    await asyncio.sleep(delay)

    # Update spacing
    await session.update_settings(layout_spacing=50)
    await asyncio.sleep(1)

    await session.draw_assistant_message("Message after large spacing")
    await asyncio.sleep(delay)

    await session.hide()
    print(f"[{session.name}] Layout spacing update test complete")


async def test_screen_change(session: TestSession, delay: float = 3.0):
    """Test changing screens while server is running."""
    print(f"[{session.name}] Testing screen change...")

    # Draw initial content
    await session.draw_assistant_message("Screen 1 - Current monitor")
    await asyncio.sleep(delay)

    # Show loader and change to screen 2
    await session.set_loading(True)
    await session.update_settings(screen=2)
    await asyncio.sleep(1)
    await session.set_loading(False)

    await session.draw_assistant_message("Screen 2 - Should be on different monitor")
    await asyncio.sleep(delay)

    # Change back to screen 1
    await session.set_loading(True)
    await session.update_settings(screen=1)
    await asyncio.sleep(1)
    await session.set_loading(False)

    await session.draw_assistant_message("Screen 1 - Back to first monitor")
    await asyncio.sleep(delay)

    await session.hide()
    print(f"[{session.name}] Screen change test complete")


async def test_combined_settings_update(session: TestSession, delay: float = 2.0):
    """Test updating multiple settings at once."""
    print(f"[{session.name}] Testing combined settings update...")

    # Draw initial content
    await session.draw_assistant_message("Initial message")
    await asyncio.sleep(delay)

    # Update multiple settings at once
    await session.update_settings(
        framerate=45,
        layout_margin=30,
        layout_spacing=25
    )
    await asyncio.sleep(1)

    await session.draw_assistant_message("After combined settings change")
    await asyncio.sleep(delay)

    # Update again with different values
    await session.update_settings(
        framerate=90,
        layout_margin=15,
        layout_spacing=5
    )
    await asyncio.sleep(1)

    await session.draw_assistant_message("After second combined change")
    await asyncio.sleep(delay)

    await session.hide()
    print(f"[{session.name}] Combined settings update test complete")


async def test_preserve_content_on_settings_change(session: TestSession, delay: float = 2.0):
    """Test that existing content is preserved when settings change."""
    print(f"[{session.name}] Testing content preservation...")

    # Draw persistent content
    await session.draw_assistant_message("This message should persist")
    await asyncio.sleep(delay)

    # Update settings multiple times
    for i in range(3):
        await session.update_settings(
            framerate=30 + i * 30,
            layout_margin=20 + i * 10,
            layout_spacing=15 + i * 5
        )
        await asyncio.sleep(1)

    # Original message should still be visible
    await session.draw_assistant_message("New message after settings changes")
    await asyncio.sleep(delay)

    await session.hide()
    print(f"[{session.name}] Content preservation test complete")


# =============================================================================
# Test Runner
# =============================================================================

async def run_all_settings_tests(session: TestSession):
    """Run all settings update tests."""
    print("\n" + "=" * 60)
    print("Running Settings Update Tests")
    print("=" * 60 + "\n")

    await test_framerate_update(session)
    await asyncio.sleep(2)

    await test_layout_margin_update(session)
    await asyncio.sleep(2)

    await test_layout_spacing_update(session)
    await asyncio.sleep(2)

    await test_screen_change(session)
    await asyncio.sleep(2)

    await test_combined_settings_update(session)
    await asyncio.sleep(2)

    await test_preserve_content_on_settings_change(session)

    print("\n" + "=" * 60)
    print("All Settings Tests Complete!")
    print("=" * 60 + "\n")
