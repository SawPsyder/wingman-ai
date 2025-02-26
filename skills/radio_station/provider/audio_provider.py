from typing import TYPE_CHECKING
from api.interface import WingmanInitializationError

if TYPE_CHECKING:
    try:
        from skills.radio_station.main import RadioStation
    except ModuleNotFoundError:
        from radio_station.main import RadioStation


class AudioProvider:

    def __init__(self, radio_station: "RadioStation", volume: list[float]):
        self.radio_station = radio_station
        # volume is a single float value in a list. This allows for easier manipulation of the volume from outside
        self.volume = volume

    async def validate(self) -> list[WingmanInitializationError]:
        return []

    async def prepare(self):
        pass

    async def play(self):
        pass

    async def stop(self):
        pass

    async def play_next_song(self):
        pass

    async def on_playback_started(self, *args, **kwargs):
        await self.radio_station.on_song_started()

    async def on_playback_stopped(self, *args, **kwargs):
        await self.radio_station.on_song_finished()

    async def get_next_songs(self, count: int) -> list[str] | None:
        """
            Should return a list of song names that are played next for the radio station host to know about.
            May return None if not supported.
        """
        return None

    async def get_current_song(self) -> str | None:
        """Should contain information about currently played song on None if there is no playback"""
        return None
