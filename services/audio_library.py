import asyncio
import os
import threading
import time
from os import path
from random import randint
from api.interface import AudioFile, AudioFileConfig
from services.printr import Printr
from services.audio_player import AudioPlayer
from services.file import get_writable_dir

printr = Printr()
DIR_AUDIO_LIBRARY = "audio_library"

class AudioLibrary:
    def __init__(
        self,
        callback_playback_started: callable = None,
        callback_playback_finished: callable = None,
    ):
        # Configurable settings
        self.callback_playback_started = callback_playback_started  # Parameters: AudioFileConfig, AudioPlayer, volume(float)
        self.callback_playback_finished = (
            callback_playback_finished  # Parameters: AudioFileConfig
        )

        # Internal settings
        self.audio_library_path = get_writable_dir(DIR_AUDIO_LIBRARY)
        self.current_playbacks = {}

    async def handle_action(self, audio_file: AudioFile | AudioFileConfig, volume_modifier: float = 1.0):
        audio_config = self.__get_audio_file_config(audio_file)

        if audio_config.stop:
            # setting `remove_playback` to false combines stop and pause in one action
            await self.stop_playback(audio_config, remove_playback=False)
        else:
            if audio_config.wait:
                await self.start_playback(audio_config, volume_modifier)
            else:
                self.__threaded_execution(self.start_playback, audio_config, volume_modifier)

    async def audio_library_toggle_play(self, audio_file: AudioFile | AudioFileConfig, volume_modifier: float = 1.0):
        audio_config = self.__get_audio_file_config(audio_file)

        # Check if any of the configured files are currently playing
        current_playbacks = [
            file
            for file in audio_config.files
            if self.get_playback_status(file)[1] and self.get_playback_status(file)[0]  # has player & is playing
        ]

        if current_playbacks:
            return await self.stop_playback(audio_config, 0.1, remove_playback=True)

        await self.start_playback(audio_config, volume_modifier)

    ##########################
    ### Playback functions ###
    ##########################

    async def start_playback(
        self,
        audio_file: AudioFile | AudioFileConfig,
        volume_modifier: float = 1.0,
    ):
        audio_file = self.__get_audio_file_config(audio_file)

        if audio_file.resume:
            current_playbacks = [
                True
                for file in audio_file.files
                if self.get_playback_status(file)[1] and self.get_playback_status(file)[0] # audio player & playing
            ]
            if current_playbacks:
                # still playing, nothing to do
                return

            paused_playbacks = [
                self.get_playback_status(file)
                for file in audio_file.files
                if self.get_playback_status(file)[1]
            ]
            if paused_playbacks:
                # resume random playback
                size = len(paused_playbacks)
                if size > 1:
                    index = randint(0, size - 1)
                else:
                    index = 0
                selected_file = paused_playbacks[index]
                audio_player = selected_file[1]
                await audio_player.resume_playback()
                self.__threaded_execution(
                    self.fade_in,
                    audio_player,
                    selected_file[2],
                    (audio_file.volume or 1.0) * volume_modifier,
                    0.5,
                    20,
                )
                return

        selected_file = self.__get_random_audio_file_from_config(audio_file)
        # skip if file does not exist
        if not path.exists(path.join(self.audio_library_path, selected_file.path, selected_file.name)):
            printr.toast_error(
                f"Skipping playback of {selected_file.name} as it does not exist (anymore) in the audio library."
            )
            return

        # stop running playbacks of configured files
        await self.stop_playback(audio_file, 0.1, remove_playback=True)

        playback_key = self.__get_playback_key(selected_file)
        audio_player = AudioPlayer(
            asyncio.get_event_loop(), self.on_playback_started, self.on_playback_finish
        )

        async def actual_start_playback(
            audio_file: AudioFile,
            audio_player: AudioPlayer,
            volume: list,
        ):
            full_path = path.join(
                self.audio_library_path, audio_file.path, audio_file.name
            )
            await audio_player.play_audio_file(
                filename=full_path,
                volume=volume,
                wingman_name=playback_key,
                publish_event=False,
            )

        volume = [(audio_file.volume or 1.0) * volume_modifier]
        self.current_playbacks[playback_key] = [
            audio_player,
            volume,
            selected_file,
        ]
        self.__threaded_execution(
            actual_start_playback, selected_file, audio_player, volume
        )
        if audio_file.wait:
            while True:
                time.sleep(0.1)
                status = self.get_playback_status(selected_file)
                if not status[1]: # no audio player
                    break

    async def stop_playback(
        self,
        audio_file: AudioFile | AudioFileConfig,
        fade_out_time: float = 0.5,
        fade_out_resolution: int = 20,
        remove_playback: bool = True,
    ):

        for file in self.__get_audio_file_config(audio_file).files:
            status = self.get_playback_status(file)
            audio_player = status[1]
            volume = status[2]
            if audio_player:
                if fade_out_time > 0:
                    self.__threaded_execution(
                        self.fade_out,
                        audio_player,
                        volume,
                        fade_out_time,
                        fade_out_resolution,
                        not remove_playback,
                    )
                else:
                    audio_player.on_playback_finished = None
                    await audio_player.stop_playback()

                self.notify_playback_finished(self.__get_playback_key(file))

                if remove_playback:
                    self.current_playbacks.pop(self.__get_playback_key(file), None)

    def get_playback_status(
        self, audio_file: AudioFile
    ) -> list[bool, AudioPlayer | None, list[float] | None]:
        playback_key = self.__get_playback_key(audio_file)

        if playback_key in self.current_playbacks:
            audio_player = self.current_playbacks[playback_key][0]
            return [
                audio_player.is_playing, # Is playing
                audio_player, # AudioPlayer
                self.current_playbacks[playback_key][1], # Current Volume list
            ]
        return [False, None, None]

    async def change_playback_volume(
        self, audio_file: AudioFile | AudioFileConfig, volume: float
    ):
        audio_file = self.__get_audio_file_config(audio_file)
        playback_keys = [
            self.__get_playback_key(current_file)
            for current_file in audio_file.files
        ]

        for playback_key in playback_keys:
            if playback_key in self.current_playbacks:
                self.current_playbacks[playback_key][1][0] = volume

    async def on_playback_started(self, file_path: str):
        self.notify_playback_started(file_path)
        # Placeholder for future implementations

    async def on_playback_finish(self, file_path: str):
        self.notify_playback_finished(file_path)
        self.current_playbacks.pop(file_path, None)

    def notify_playback_started(self, file_path: str):
        if self.callback_playback_started:
            # Give the callback the audio file that started playing and current volume
            audio_file = self.current_playbacks[file_path][2]
            audio_player = self.current_playbacks[file_path][0]
            volume = self.current_playbacks[file_path][1][0]
            self.callback_playback_started(audio_file, audio_player, volume)

    def notify_playback_finished(self, file_path: str):
        if self.callback_playback_finished:
            # Give the callback the audio file that finished playing
            audio_file = self.current_playbacks[file_path][2]
            self.callback_playback_finished(audio_file)

    ###############################
    ### Audio Library functions ###
    ###############################

    def get_audio_files(self) -> list[AudioFile]:
        audio_files = []
        try:
            for root, _, files in os.walk(self.audio_library_path):
                for file in files:
                    if file.endswith((".wav", ".mp3")):
                        rel_path = path.relpath(root, self.audio_library_path)
                        rel_path = "" if rel_path == "." else rel_path
                        audio_files.append(AudioFile(path=rel_path, name=file))
        except Exception:
            pass
        return audio_files

    ########################
    ### Helper functions ###
    ########################

    async def fade_out(
        self,
        audio_player: AudioPlayer,
        volume: list[float],
        fade_out_time: int,
        fade_out_resolution: int,
        pause: bool = False,
    ):
        original_volume = volume[0]
        step_size = original_volume / fade_out_resolution
        step_duration = fade_out_time / fade_out_resolution
        while audio_player.is_playing and volume[0] > 0.0001:
            volume[0] -= step_size
            await asyncio.sleep(step_duration)
        await asyncio.sleep(0.05)  # 50ms grace period
        audio_player.on_playback_finished = None
        if pause:
            await audio_player.pause_playback()
        else:
            await audio_player.stop_playback()

    async def fade_in(
        self,
        audio_player: AudioPlayer,
        volume: list[float],
        new_volume: float,
        fade_in_time: int,
        fade_in_resolution: int,
    ):
        new_volume = min(new_volume, 1.0)
        original_volume = volume[0]
        step_size = (new_volume - original_volume) / fade_in_resolution
        step_duration = fade_in_time / fade_in_resolution
        while audio_player.is_playing and volume[0] < new_volume:
            volume[0] += step_size
            await asyncio.sleep(step_duration)

    def __threaded_execution(self, function, *args) -> threading.Thread:
        """Execute a function in a separate thread."""

        def start_thread(function, *args):
            if asyncio.iscoroutinefunction(function):
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                new_loop.run_until_complete(function(*args))
                new_loop.close()
            else:
                function(*args)

        thread = threading.Thread(target=start_thread, args=(function, *args))
        thread.start()
        return thread

    def __get_audio_file_config(
        self, audio_file: AudioFile | AudioFileConfig
    ) -> AudioFileConfig:
        if isinstance(audio_file, AudioFile):
            return AudioFileConfig(
                files=[audio_file],
                volume=1,
                wait=False,
                stop=False,
                resume=False,
            )
        return audio_file

    def __get_random_audio_file_from_config(
        self, audio_file: AudioFileConfig
    ) -> AudioFile:
        size = len(audio_file.files)
        if size > 1:
            index = randint(0, size - 1)
        else:
            index = 0

        return audio_file.files[index]

    def __get_playback_key(self, audio_file: AudioFile) -> str:
        return path.join(audio_file.path, audio_file.name)