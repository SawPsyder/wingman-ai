import asyncio
import base64
import datetime
import json

import pyaudio
from hume.client import AsyncHumeClient
from hume.empathic_voice import (
    ToolResponseMessage,
    AssistantInput,
    PauseAssistantMessage,
    ResumeAssistantMessage,
    ToolErrorMessage,
    SessionSettings,
    Tool,
)
from hume.empathic_voice.chat.socket_client import ChatConnectOptions, ChatWebsocketConnection
from hume.empathic_voice.chat.types import SubscribeEvent
from hume.empathic_voice.types import UserInput
from hume import MicrophoneInterface, Stream

from api.enums import LogType
from services.printr import Printr

printr = Printr()


class HumeAi:
    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        config_id: str = "",
        on_open: callable = None,
        on_message: callable = None,
        on_close: callable = None,
        on_error: callable = None,
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._config_id = config_id
        self._on_open = on_open
        self._on_message = on_message
        self._on_close = on_close
        self._on_error = on_error
        self._websocket_handler: WebSocketHandler | None = None
        self._active = False
        self._client: AsyncHumeClient | None = None
        self._options: ChatConnectOptions | None = None

    async def prepare(
            self,
            chat_group_id: str = None,
            microphone_index: int = None,
    ):
        # Initialize the asynchronous client, authenticating with your API key
        self._client = AsyncHumeClient(api_key=self._api_key)

        # Define options for the WebSocket connection, such as an EVI config id and a secret key for token authentication
        # See the full list of query parameters here: https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#request.query
        self._options = ChatConnectOptions(
            config_id=self._config_id,
            secret_key=self._secret_key,
            resumed_chat_group_id=chat_group_id,
        )

        # Instantiate the WebSocketHandler
        self._websocket_handler = WebSocketHandler()

        # Open the WebSocket connection with the configuration options and the handler's functions
        async with self._client.empathic_voice.chat.connect_with_callbacks(
            options=self._options,
            on_open=self.__on_open,
            on_message=self.__on_message,
            on_close=self.__on_close,
            on_error=self.__on_error,
        ) as socket:
            # Set the socket instance in the handler
            self._websocket_handler.set_socket(socket)

            # Create an asynchronous task to continuously detect and process input from the microphone, as well as play audio
            microphone_task = asyncio.create_task(
                MicrophoneInterface.start(
                    socket,
                    byte_stream=self._websocket_handler.byte_strs,
                    allow_user_interrupt=True,
                    device=microphone_index,
                )
            )

            # Create an asynchronous task to send messages over the WebSocket connection
            message_sending_task = asyncio.create_task(self.sending_handler(socket))

            # Schedule the coroutines to occur simultaneously
            await asyncio.gather(microphone_task, message_sending_task)

    async def __on_open(self):
        print("Hume webSocket connection opened.")
        self._active = True
        await self._websocket_handler.on_open()
        if self._on_open:
            if asyncio.iscoroutinefunction(self._on_open):
                await self._on_open()
            else:
                self._on_open()

    async def __on_message(self, message: SubscribeEvent):
        await self._websocket_handler.on_message(message)
        if self._on_message:
            if asyncio.iscoroutinefunction(self._on_message):
                await self._on_message(message)
            else:
                self._on_message(message)

    async def __on_close(self):
        print("Hume webSocket connection closed.")
        self._active = False
        await self._websocket_handler.on_close()
        if self._on_close:
            if asyncio.iscoroutinefunction(self._on_close):
                await self._on_close()
            else:
                self._on_close()

    async def __on_error(self, error):
        print(f"Error: {error}")
        await self._websocket_handler.on_error(error)
        if self._on_error:
            if asyncio.iscoroutinefunction(self._on_error):
                await self._on_error(error)
            else:
                self._on_error(error)

    async def sending_handler(self, socket: ChatWebsocketConnection):
        """Handle sending a message over the socket.

        This method waits 3 seconds and sends a UserInput message, which takes a `text` parameter as input.
        - https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#send.User%20Input.type

        See the full list of messages to send [here](https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#send).

        Args:
            socket (ChatWebsocketConnection): The WebSocket connection used to send messages.
        """
        # Wait 3 seconds before executing the rest of the method
        await asyncio.sleep(3)

        # while self._active:
        #     await asyncio.sleep(0.05) # Sleep for 50ms
        #     if self._tasks:
        #         await asyncio.gather(*self._tasks)

        # Construct a user input message
        # user_input_message = UserInput(text="Hello there!")

        # Send the user input as text to the socket
        # await socket.send_user_input(user_input_message)

    async def pause(self):
        if not self._websocket_handler.socket:
            return

        if self._active:
            self._active = False
            await self._websocket_handler.socket.send_pause_assistant(PauseAssistantMessage())

    async def resume(self):
        if not self._websocket_handler.socket:
            return

        if not self._active:
            await self._websocket_handler.socket.send_resume_assistant(ResumeAssistantMessage())
            self._active = True

    async def send_user_input(self, text: str):
        if not self._websocket_handler.socket:
            return

        user_input_message = UserInput(text=text)
        await self._websocket_handler.socket.send_user_input(user_input_message)

    async def send_assistant_input(self, text: str):
        if not self._websocket_handler.socket:
            return

        assistant_input_message = AssistantInput(text=text)
        await self._websocket_handler.socket.send_assistant_input(assistant_input_message)

    async def send_tool_response(
        self,
        tool_call_id: str,
        content: str,
        tool_name: str = None,
        tool_type: str = "function",
    ):
        if not self._websocket_handler.socket:
            await printr.print_async("Tool response: Websocket handler has no active socket.", LogType.ERROR)
            return

        tool_response = ToolResponseMessage(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_type=tool_type,
            content=content,
        )
        await self._websocket_handler.socket.send_tool_response(tool_response)

    async def send_tool_error(
        self,
        tool_call_id: str,
        content: str,
        error: str,
        error_code: str = None,
        tool_type: str = "function",
    ):
        if not self._websocket_handler.socket:
            await printr.print_async("Tool error: Websocket handler has no active socket.", LogType.ERROR)
            return

        tool_error = ToolErrorMessage(
            tool_call_id=tool_call_id,
            content=content,
            error=error,
            code=error_code,
            tool_type=tool_type,
        )
        await self._websocket_handler.socket.send_tool_error(tool_error)

    async def update_system_prompt(self, text: str):
        if not self._websocket_handler.socket:
            await printr.print_async("Update prompt: Websocket handler has no active socket.", LogType.ERROR)
            return

        settings = SessionSettings(
            system_prompt=text
        )
        await self._websocket_handler.socket.send_session_settings(settings)


    async def set_tools(self, tools: list[dict]):
        if not self._websocket_handler.socket:
            await printr.print_async("Set tools: Websocket handler has no active socket.", LogType.ERROR)
            return

        hume_tools = []
        for tool in tools:
            try:
                name = tool.get("function", {}).get("name", "")
                parameters = tool.get("function", {}).get("parameters", {})
                parameters.pop("optional", None)
                parameters = str(json.dumps(parameters))
                tool_description = tool.get("function", {}).get("description", "")
                fallback = "Unable to process tool at this time, please try again later."
                hume_tool = Tool(
                    type="function",
                    name=name,
                    parameters=parameters,
                    description=tool_description,
                    fallback_content=fallback,
                )
                print(hume_tool)
                hume_tools.append(hume_tool)
            except Exception as e:
                await printr.print_async(f"Error creating tool: {e}")

        settings = SessionSettings(
            tools=hume_tools
        )
        await self._websocket_handler.socket.send_session_settings(settings)


class WebSocketHandler:
    """Handler for containing the EVI WebSocket and associated socket handling behavior."""

    def __init__(self):
        """Construct the WebSocketHandler, initially assigning the socket to None and the byte stream to a new Stream object."""
        self.socket: ChatWebsocketConnection | None = None
        self.byte_strs = Stream.new()

    def set_socket(self, socket: ChatWebsocketConnection):
        """Set the socket.

        This method assigns the provided asynchronous WebSocket connection
        to the instance variable `self.socket`. It is invoked after successfully
        establishing a connection using the client's connect method.

        Args:
            socket (ChatWebsocketConnection): EVI asynchronous WebSocket returned by the client's connect method.
        """
        self.socket = socket

    async def on_open(self):
        """Logic invoked when the WebSocket connection is opened."""
        await printr.print_async("Hume webSocket connection opened.")

    async def on_message(self, message: SubscribeEvent):
        """Callback function to handle a WebSocket message event.

        This asynchronous method decodes the message, determines its type, and
        handles it accordingly. Depending on the type of message, it
        might log metadata, handle user or assistant messages, process
        audio data, raise an error if the message type is "error", and more.

        This method interacts with the following message types to demonstrate logging output to the terminal:
        - [chat_metadata](https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#receive.Chat%20Metadata.type)
        - [user_message](https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#receive.User%20Message.type)
        - [assistant_message](https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#receive.Assistant%20Message.type)
        - [audio_output](https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#receive.Audio%20Output.type)

        Args:
            data (SubscribeEvent): This represents any type of message that is received through the EVI WebSocket, formatted in JSON. See the full list of messages in the API Reference [here](https://dev.hume.ai/reference/empathic-voice-interface-evi/chat/chat#receive).
        """

        # Create an empty dictionary to store expression inference scores
        scores = {}

        if message.type == "chat_metadata":
            message_type = message.type.upper()
            chat_id = message.chat_id
            chat_group_id = message.chat_group_id
            text = f"<{message_type}> Chat ID: {chat_id}, Chat Group ID: {chat_group_id}"
        elif message.type in ["user_message", "assistant_message"]:
            role = message.message.role.upper()
            message_text = message.message.content
            text = f"{role}: {message_text}"
            if message.from_text is False:
                scores = dict(message.models.prosody.scores)
        elif message.type == "audio_output":
            message_str: str = message.data
            audio_bytes = base64.b64decode(message_str.encode("utf-8"))
            await self.byte_strs.put(audio_bytes)
            return
        elif message.type == "error":
            error_message: str = message.message
            error_code: str = message.code
            text = f"Error: {error_message} (Code: {error_code})"
        else:
            message_type = message.type.upper()
            text = f"<{message_type}>"

        # Print the formatted message
        self._print_prompt(text)

        # Extract and print the top 3 emotions inferred from user and assistant expressions
        if len(scores) > 0:
            top_3_emotions = self._extract_top_n_emotions(scores, 3)
            self._print_emotion_scores(top_3_emotions)
            print("")
        else:
            print("")

    async def on_close(self):
        """Logic invoked when the WebSocket connection is closed."""
        await printr.print_async("Hume webSocket connection closed.")

    async def on_error(self, error):
        """Logic invoked when an error occurs in the WebSocket connection.

        See the full list of errors [here](https://dev.hume.ai/docs/resources/errors).

        Args:
            error (Exception): The error that occurred during the WebSocket communication.
        """
        print(f"Error: {error}")

    def _print_prompt(self, text: str) -> None:
        """Print a formatted message with a timestamp.

        Args:
            text (str): The message text to be printed.
        """
        return
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        now_str = now.strftime("%H:%M:%S")
        print(f"[{now_str}] {text}")

    def _extract_top_n_emotions(self, emotion_scores: dict, n: int) -> dict:
        """
        Extract the top N emotions based on confidence scores.

        Args:
            emotion_scores (dict): A dictionary of emotions and their corresponding confidence scores.
            n (int): The number of top emotions to extract.

        Returns:
            dict: A dictionary containing the top N emotions as keys and their raw scores as values.
        """
        # Convert the dictionary into a list of tuples and sort by the score in descending order
        sorted_emotions = sorted(emotion_scores.items(), key=lambda item: item[1], reverse=True)

        # Extract the top N emotions
        top_n_emotions = {emotion: score for emotion, score in sorted_emotions[:n]}

        return top_n_emotions

    def _print_emotion_scores(self, emotion_scores: dict) -> None:
        """
        Print the emotions and their scores in a formatted, single-line manner.

        Args:
            emotion_scores (dict): A dictionary of emotions and their corresponding confidence scores.
        """
        return
        # Format the output string
        formatted_emotions = ' | '.join([f"{emotion} ({score:.2f})" for emotion, score in emotion_scores.items()])

        # Print the formatted string
        print(f"|{formatted_emotions}|")