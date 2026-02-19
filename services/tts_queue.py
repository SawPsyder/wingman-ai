# services/tts_queue.py
import asyncio
from typing import AsyncIterator, Optional, Callable, Awaitable

class TTSQueue:
    """Manages TTS requests with queueing and streaming."""

    def __init__(self):
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False
        self._consumer_task: asyncio.Task | None = None
        self._audio_callback: Callable[[bytes], Awaitable[None]] | None = None
        # For pre-generated audio
        self._audio_buffers: dict[str, bytes] = {}
        self._pending_generation: set[str] = set()
        self._generation_lock = asyncio.Lock()

    def set_audio_callback(self, callback: Callable[[bytes], Awaitable[None]]) -> None:
        """Set callback for audio chunks to be played."""
        self._audio_callback = callback

    async def enqueue(self, text: str) -> None:
        """Add text to TTS queue and start background audio generation."""
        if self._closed:
            return
        await self._queue.put(text)

        # Start background audio generation immediately
        asyncio.create_task(self._generate_audio_background(text))

    async def _generate_audio_background(self, text: str, retry_count: int = 0) -> None:
        """Generate audio in background with retry."""
        max_retries = 1  # Retry once

        if text in self._audio_buffers or text in self._pending_generation:
            return

        async with self._generation_lock:
            if text in self._audio_buffers or text in self._pending_generation:
                return
            self._pending_generation.add(text)

        try:
            audio_bytes = b""
            async for chunk in self._generate_audio(text):
                if chunk:
                    audio_bytes += chunk

            if audio_bytes:
                self._audio_buffers[text] = audio_bytes
        except Exception as e:
            print(f"Background audio generation failed (attempt {retry_count + 1}): {e}")
            if retry_count < max_retries:
                # Retry after brief delay
                await asyncio.sleep(0.5)
                return await self._generate_audio_background(text, retry_count + 1)
        finally:
            self._pending_generation.discard(text)

    async def clear(self) -> None:
        """Clear all pending TTS requests."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return self._queue.empty()

    async def get_stream(self) -> AsyncIterator[bytes]:
        """Yield audio chunks from sentences in exponentially growing batches.

        Batch sizes: 1, 2, 4, 8... capped at 10 sentences.
        This enables faster initial response while batching longer sentences.
        """
        batch_size = 1
        max_batch_size = 10
        pending_sentences: list[str] = []
        consecutive_timeouts = 0
        max_consecutive_timeouts = 5

        def get_pending_text() -> str:
            """Combine pending sentences with space separation."""
            return " ".join(pending_sentences)

        def process_batch():
            """Process current pending sentences as a batch."""
            nonlocal batch_size
            if pending_sentences:
                batch_text = get_pending_text()
                if batch_text:
                    return batch_text
            return None

        # Keep processing until queue is empty AND closed
        while not self._closed or not self._queue.empty():
            try:
                text = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                consecutive_timeouts = 0

                if text and text.strip():
                    pending_sentences.append(text.strip())

                # Check if we have enough for current batch
                if len(pending_sentences) >= batch_size:
                    # Generate audio for the batch
                    batch_text = process_batch()
                    if batch_text:
                        async for chunk in self._generate_audio(batch_text):
                            if chunk:
                                yield chunk

                    # Clear pending and increase batch size
                    pending_sentences.clear()
                    batch_size = min(batch_size * 2, max_batch_size)

            except asyncio.TimeoutError:
                consecutive_timeouts += 1

                # If we have pending sentences and queue is settling, process them
                if pending_sentences and self._queue.empty():
                    # Process remaining as final batch
                    batch_text = process_batch()
                    if batch_text:
                        async for chunk in self._generate_audio(batch_text):
                            if chunk:
                                yield chunk
                    pending_sentences.clear()
                    batch_size = 1  # Reset for next stream

                # Exit if queue empty for too long and closed
                if consecutive_timeouts >= max_consecutive_timeouts and self._queue.empty():
                    break

        # Final flush - play any remaining sentences
        if pending_sentences:
            batch_text = get_pending_text()
            if batch_text:
                async for chunk in self._generate_audio(batch_text):
                    if chunk:
                        yield chunk

    async def _generate_audio(self, text: str) -> AsyncIterator[bytes]:
        """Generate audio for text. Override in subclass or inject provider."""
        # Placeholder - will be connected to actual TTS in integration
        yield b""

    async def close(self) -> None:
        """Close the queue."""
        self._closed = True
        await self.clear()
