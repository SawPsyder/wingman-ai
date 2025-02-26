import asyncio
import os
import random
from typing import TYPE_CHECKING
from mutagen import MutagenError
from api.enums import WingmanInitializationErrorType
from api.interface import WingmanInitializationError
from services.audio_player import AudioPlayer

try:
    from skills.radio_station.provider.audio_provider import AudioProvider
except ModuleNotFoundError:
    from radio_station.provider.audio_provider import AudioProvider

if TYPE_CHECKING:
    try:
        from skills.radio_station.main import RadioStation
    except ModuleNotFoundError:
        from radio_station.main import RadioStation


class MusicFile:
    def __init__(self, path: str):
        self.path = str(path)

        self._name: str | None = None
        self._title: str | None = None
        self._artists: list[str] | None = None
        self._year: int | None = None
        self._album: str | None = None

        self.__read_title_details()

    def __read_title_details(self):
        try:
            from mutagen import File
            audio = File(self.path)
            if audio:
                self._title = audio.get('TIT2', [None])[0]
                self._artists = audio.get('TPE1', [None])[0]
                self._year = audio.get('TDRC', [None])[0]
                self._album = audio.get('TALB', [None])[0]

                if self._title or self._artists or self._album or self._year:
                    details = []
                    if self._title:
                        details.append(f"Title: {self._title}")
                    if self._artists:
                        details.append(f"Artists: {self._artists}")
                    if self._album:
                        details.append(f"Album: {self._album}")
                    if self._year:
                        details.append(f"Year: {self._year}")
                    self._name = ", ".join(details)
        except MutagenError as e:
            print(f"Error reading audio file details: {e}")
            pass

        if not self._name:
            self._name = ".".join(os.path.basename(self.path).split(".")[:-1])

    def get_name(self) -> str:
        return self._name

    def get_title(self) -> str | None:
        return self._title

    def get_artists(self) -> list[str] | None:
        return self._artists

    def get_year(self) -> int | None:
        return self._year


class LocalPlayback(AudioProvider):
    def __init__(self, radio_station: "RadioStation", volume: list[float]):
        super().__init__(radio_station, volume)

        self.path = None
        self.music_files: list[MusicFile] = []
        self.remaining_files: list[MusicFile] = []
        self.currently_played: MusicFile | None = None
        self.audio_player: AudioPlayer | None = None

    def set_source(self, path: str):
        self.path = path

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()

        if not self.path or not os.path.isdir(self.path):
            errors.append(WingmanInitializationError(
                wingman_name=self.radio_station.wingman.name,
                message=f"Invalid path: {self.path}. Path does not exist or is not a directory.",
                error_type=WingmanInitializationErrorType.INVALID_CONFIG,
            ))

        return errors

    async def prepare(self):
        for root, _, files in os.walk(self.path):
            for file in files:
                if file.endswith(('.mp3', '.wav')):
                    music_file = MusicFile(str(os.path.join(root, file)))
                    self.music_files.append(music_file)

        self.audio_player = AudioPlayer(
            asyncio.Queue(), None, None
        )

    async def on_playback_stopped(self, **kwargs):
        self.currently_played = None
        await super().on_playback_stopped(**kwargs)

    async def play(self):
        pass

    async def stop(self):
        await self.audio_player.stop_playback()
        self.currently_played = None
        pass

    async def play_next_song(self):
        song = self.remaining_files.pop(0)
        self.currently_played = song
        await self.on_playback_started()

        # check if song is mp3 or wav
        if song.path.endswith('.mp3'):
            self.audio_player.play_mp3(song.path, volume=self.volume)
        elif song.path.endswith('.wav'):
            self.audio_player.play_wav(song.path, volume=self.volume)

        await self.on_playback_stopped()

    async def get_next_songs(self, count: int) -> list[str]:
        if count > len(self.remaining_files):
            self.remaining_files = self.music_files.copy()
            random.shuffle(self.remaining_files)
            if count > len(self.remaining_files):
                count = len(self.remaining_files)

        next_songs = []
        for i in range(count):
            next_songs.append(self.remaining_files[i].get_name())

        return next_songs

    async def get_current_song(self) -> str | None:
        if self.currently_played:
            return self.currently_played.get_name()
