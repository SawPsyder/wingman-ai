# services/tts_queue.py
import queue


class TTSQueue:
    """Manages TTS requests with thread-safe queueing.

    Uses queue.Queue instead of asyncio.Queue so sentences can be enqueued
    from the main event loop and consumed from the dedicated TTS thread's
    event loop without cross-loop errors.
    """

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._closed = False
        self._consumer_done = False

    def reset(self):
        """Reset queue state for a new streaming session."""
        self._queue = queue.Queue()
        self._closed = False
        self._consumer_done = False

    def enqueue_sync(self, text: str) -> None:
        """Thread-safe enqueue — callable from any thread or event loop."""
        if self._closed:
            return
        self._queue.put_nowait(text)

    async def enqueue(self, text: str) -> None:
        """Async wrapper for enqueue (non-blocking, safe from any loop)."""
        self.enqueue_sync(text)

    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return self._queue.empty()

    async def clear(self) -> None:
        """Drain all pending TTS requests."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
