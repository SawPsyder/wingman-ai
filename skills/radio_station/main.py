import sys
from os import path

# add skill to sys path
sys.path.append(path.dirname(path.dirname(path.abspath(__file__))))

import asyncio
import time
from random import randrange
from typing import TYPE_CHECKING
from api.interface import (
    SettingsConfig,
    SkillConfig,
    VoiceSelection,
    WingmanInitializationError, SoundConfig,
)
from api.enums import (
    WingmanInitializationErrorType,
    TtsProvider,
)
from services.audio_player import AudioPlayer
from services.benchmark import Benchmark
from services.file import get_writable_dir
from skills.skill_base import Skill

try:
    from skills.radio_station.provider.audio_provider import AudioProvider
    from skills.radio_station.provider.local_playback import LocalPlayback
except ModuleNotFoundError:
    from radio_station.provider.audio_provider import AudioProvider
    from radio_station.provider.local_playback import LocalPlayback


if TYPE_CHECKING:
    from wingmen.open_ai_wingman import OpenAiWingman


class RadioStation(Skill):

    def __init__(
        self,
        config: SkillConfig,
        settings: SettingsConfig,
        wingman: "OpenAiWingman",
    ) -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)

        self.file_path = get_writable_dir(path.join("skills", "radio_station", "data"))

        # configuration options
        self.prompt: str | None = None
        self.voice: VoiceSelection | None = None
        self.voice_volume: float | None = None
        self.source_local_path: str | None = None
        self.song_announcement: bool | None = None
        self.song_count_min: int | None = None
        self.song_count_max: int | None = None
        self.song_volume: list[float] | None = None

        # data
        self.sound_config: SoundConfig | None = None
        self.announcement_generation: bool | None = None
        self.announcement_message: str | None = None
        self.message_history: list = []
        self.radio_station_status: bool = False # False = off, True = on
        self.loaded: bool = False
        self.provider: AudioProvider | None = None
        self.audio_player: AudioPlayer | None = None
        self.remaining_songs: int | None = None

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()

        self.prompt = self.retrieve_custom_property_value("prompt", errors)

        voice: VoiceSelection = self.retrieve_custom_property_value(
            "voice", errors
        )
        if not voice:
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid value for 'voice'. Expected a voice selection, but its empty.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )
        else:
            # set voice
            self.voice = voice

            # find and initialize the voice provider
            if (
                self.voice.provider == TtsProvider.OPENAI
                and not self.wingman.openai
            ):
                await self.wingman.validate_and_set_openai(errors)
            elif (
                self.voice.provider == TtsProvider.AZURE
                and not self.wingman.openai_azure
            ):
                await self.wingman.validate_and_set_azure(errors)
            elif (
                self.voice.provider == TtsProvider.ELEVENLABS
                and not self.wingman.elevenlabs
            ):
                await self.wingman.validate_and_set_elevenlabs(errors)
            elif (
                self.voice.provider == TtsProvider.WINGMAN_PRO
                and not self.wingman.wingman_pro
            ):
                await self.wingman.validate_and_set_wingman_pro()
            elif (
                self.voice.provider == TtsProvider.XVASYNTH
                and not self.wingman.xvasynth
            ):
                pass # no initialization needed

        voice_volume = self.retrieve_custom_property_value("voice_volume", errors)
        if voice_volume is not None and (voice_volume < 0 or voice_volume > 1):
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid value for 'voice_volume'. Expected a number between 0 and 1, {voice_volume} given.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )
        self.voice_volume = voice_volume

        self.source_local_path = self.retrieve_custom_property_value("source_local_path", errors)
        if not self.source_local_path:
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message="No path to local music files provided.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )

        self.song_announcement = self.retrieve_custom_property_value("song_announcement", errors)
        if self.song_announcement is not None and not isinstance(self.song_announcement, bool):
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid value for 'song_announcement'. Expected a boolean, {self.song_announcement} given.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )

        self.song_count_min = self.retrieve_custom_property_value("song_count_min", errors)
        if self.song_count_min is not None and self.song_count_min < 1:
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid value for 'song_count_min'. Expected a number of one or larger, {self.song_count_min} given.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )

        self.song_count_max = self.retrieve_custom_property_value("song_count_max", errors)
        if self.song_count_max is not None and (self.song_count_max < 1 or (self.song_count_min is not None and self.song_count_max < self.song_count_min)):
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid value for 'song_count_max'. Expected a number greater than or equal to 'song_count_min', {self.song_count_max} given.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )

        song_volume = self.retrieve_custom_property_value("song_volume", errors)
        if song_volume is not None and (song_volume < 0 or song_volume > 1):
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.wingman.name,
                    message=f"Invalid value for 'volume_modifier'. Expected a number between 0 and 1, {song_volume} given.",
                    error_type=WingmanInitializationErrorType.INVALID_CONFIG,
                )
            )
        self.song_volume = [song_volume]

        # TODO: add switch for other providers here once phase 2 is reached
        if not errors:
            self.provider = LocalPlayback(self, self.song_volume)
            self.provider.set_source(self.source_local_path)
            errors = await self.provider.validate()

        return errors

    async def prepare(self) -> None:
        self.loaded = True
        self.radio_station_status = False
        await self.provider.prepare()
        self.audio_player = AudioPlayer(
            asyncio.Queue(), self.on_talk_started, self.on_talk_finished
        )
        self.sound_config = SoundConfig(
            volume=self.voice_volume,
            effects=[],
            play_beep=False,
            play_beep_apollo=False,
        )

    async def unload(self) -> None:
        self.loaded = False
        self.radio_station_status = False

    async def get_prompt(self) -> str | None:
        prompt = await super().get_prompt() or ""
        prompt += f"\nCurrent radio status: {'on' if self.radio_station_status else 'off'}"
        prompt += f"\nCurrent music volume: {self.song_volume[0]*100}%"

        provider_prompt = await self.provider.get_current_song() or "No song playing."
        prompt += f"\nCurrent title: {provider_prompt}"

        return prompt

    def get_tools(self) -> list[tuple[str, dict]]:
        tools = [
            (
                "radio_station_turn_on",
                {
                    "type": "function",
                    "function": {
                        "name": "radio_station_turn_on",
                        "description": "Turn the music radio station on to play some music and get news.",
                    },
                },
            ),
            (
                "radio_station_turn_off",
                {
                    "type": "function",
                    "function": {
                        "name": "radio_station_turn_off",
                        "description": "Turn the music radio station off to stop the music and no longer get news.",
                    },
                },
            ),
            (
                "radio_station_music_volume_up",
                {
                    "type": "function",
                    "function": {
                        "name": "radio_station_music_volume_up",
                        "description": "Increase the volume of the music radio station by 5%",
                    },
                },
            ),
            (
                "radio_station_music_volume_down",
                {
                    "type": "function",
                    "function": {
                        "name": "radio_station_music_volume_down",
                        "description": "Decrease the volume of the music radio station by 5%",
                    },
                },
            ),
        ]
        return tools

    async def execute_tool(
        self, tool_name: str, parameters: dict[str, any], benchmark: Benchmark
    ) -> tuple[str, str]:
        function_response = ""
        instant_response = ""
        benchmark.start_snapshot(f"Radio Station: {tool_name}")

        if tool_name == "radio_station_turn_on":
            if self.radio_station_status:
                function_response = "Music radio station is already on."
            else:
                self.radio_station_status = True
                self.threaded_execution(self.simulate_radio)
                function_response = "Music radio station is now on."

        if tool_name == "radio_station_turn_off":
            if self.radio_station_status:
                self.radio_station_status = False
                function_response = "Music radio station is now off."
            else:
                function_response = "Music radio station is already off."

        if tool_name == "radio_station_music_volume_up":
            new_song_volume = round(self.song_volume[0] + 0.05, 3)
            # check if it would be too loud
            if new_song_volume > 1:
                function_response = "Volume is already at maximum."
            else:
                self.song_volume[0] = new_song_volume
                function_response = f"Volume increased by 5% to {self.song_volume[0]*100}%."

        if tool_name == "radio_station_music_volume_down":
            new_song_volume = round(self.song_volume[0] - 0.05, 3)
            # check if it would be too quiet
            if new_song_volume < 0:
                function_response = "Volume is already at minimum."
            else:
                self.song_volume[0] = new_song_volume
                function_response = f"Volume decreased by 5% to {self.song_volume[0]*100}%."

        benchmark.finish_snapshot()
        return function_response, instant_response

    def is_active(self) -> bool:
        return self.radio_station_status and self.loaded

    def randrange(self, start, stop=None):
        if start == stop:
            return start
        random = randrange(start, stop)
        return random

    async def simulate_radio(self):
        self.threaded_execution(self.control_loop)
        while self.is_active():
            # calculate next song batch size
            next_batch_size = self.randrange(self.song_count_min, self.song_count_max + 1)

            # get next song batch
            if self.song_announcement:
                next_batch = await self.provider.get_next_songs(next_batch_size)
                if next_batch is not None:
                    next_batch_size = len(next_batch) # possibly fewer songs than requested
                    self.add_user_message(
                        f"Music stopped playing. Do your radio break announcements now. After you did that, the next {next_batch_size} songs will start playing: {'; '.join(next_batch)}"
                    )
                else:
                    self.add_user_message(
                        f"Music stopped playing. Do your radio break announcements now. After you did that, the next {next_batch_size} songs will start playing."
                    )
            else:
                self.add_user_message(
                    f"Music stopped playing. Do your radio break announcements now. After you did that, music will start playing again."
                )

            # set number of songs to play before next batch
            self.remaining_songs = next_batch_size

            # get AI generated announcement
            announcement = await self.get_next_announcement()
            if announcement:
                await self.wingman.play_to_user(
                    text=announcement,
                    no_interrupt=False,
                    sound_config=self.sound_config,
                    audio_player=self.audio_player
                )
                time.sleep(1)
                while self.audio_player.is_playing:
                    time.sleep(0.25)

            # play next song batch
            await self.play_next_song()

    async def play_next_song(self):
        remaining_songs = self.remaining_songs
        if remaining_songs > 0:
            self.threaded_execution(self.provider.play_next_song)
            while remaining_songs > 0:
                time.sleep(0.25)
                if self.remaining_songs != remaining_songs:
                    remaining_songs = self.remaining_songs
                    if self.remaining_songs > 0:
                        self.threaded_execution(self.provider.play_next_song)

    async def get_next_announcement(self):
        message_history = [{
            "role": "system",
            "content": self.prompt
        }]
        message_history.extend(self.message_history)

        message = None
        retries = 2
        while not message and retries > 0:
            retries -= 1
            try:
                completion = await self.llm_call(message_history)
                message = completion.choices[0].message.content
                self.add_assistant_message(message)
            except Exception as e:
                pass

        return message

    async def on_song_finished(self, *args, **kwargs):
        if self.remaining_songs:
            self.remaining_songs -= 1

    async def on_talk_finished(self, *args, **kwargs):
        pass

    async def on_song_started(self, *args, **kwargs):
        pass

    async def on_talk_started(self, *args, **kwargs):
        pass

    def add_assistant_message(self, message: str):
        self.message_history.append({"role": "assistant", "content": message})

    def add_user_message(self, message: str) -> None:
        self.message_history.append({"role": "user", "content": message})

    async def control_loop(self):
        while self.is_active():
            time.sleep(0.5)

        self.message_history = []
        self.remaining_songs = 0
        await self.provider.stop()
        await self.audio_player.stop_playback()
