import asyncio
import json
import traceback
import base64
from api.interface import (
    WingmanInitializationError,
)
from api.enums import (
    LogType,
    LogSource,
    WingmanInitializationErrorType,
)
from providers.hume_ai import HumeAi
from services.benchmark import Benchmark
from services.printr import Printr
from skills.skill_base import Skill
from wingmen.open_ai_wingman import OpenAiWingman
from hume.empathic_voice.chat.types import SubscribeEvent

printr = Printr()


class HumeAiWingman(OpenAiWingman):
    """A Hume AI wingman, specifically for EVI (Empathic Voice Interface) - your realtime emotional wingman."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # hume ai specific
        self.hume: HumeAi | None = None
        self.hume_chat_id: str | None = None
        self.hume_chat_group_id: str | None = None
        self.hume_config_id: str | None = None
        self.active = True

    async def validate(self):
        errors = await super().validate()

        try:
            errors = await self.validate_and_set_hume(errors)
        except Exception as e:
            errors.append(
                WingmanInitializationError(
                    wingman_name=self.name,
                    message=f"Error during provider validation: {str(e)}",
                    error_type=WingmanInitializationErrorType.UNKNOWN,
                )
            )
            printr.print(
                f"Error during provider validation: {str(e)}",
                color=LogType.ERROR,
                server_only=True,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

        return errors

    async def unload(self):
        await super().unload()
        self.active = False

    async def prepare(self):
        await super().prepare()
        try:
            self.threaded_execution(self.hume.prepare, self.hume_chat_group_id)
            await asyncio.sleep(5)
            self.active = True
            self.threaded_execution(self.hume_update_loop)
        except Exception as e:
            await printr.print_async(
                f"Error while preparing wingman '{self.name}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

    async def validate_and_set_hume(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("hume_api", errors)
        secret_key = await self.retrieve_secret("hume_secret", errors)
        config_id = self.config.hume.config_id
        if api_key and secret_key and config_id:
            self.hume = HumeAi(
                api_key=api_key,
                secret_key=secret_key,
                config_id=config_id,
                on_open=self.hume_on_open,
                on_close=self.hume_on_close,
                on_message=self.hume_on_message,
                on_error=self.hume_on_error,
            )
        return errors

    async def hume_update_loop(self):
        last_prompt = ""
        last_tools = []
        while self.active:
            await asyncio.sleep(1)

            # update prompt
            new_prompt = await self.get_context()
            if last_prompt != new_prompt:
                await self.hume.update_system_prompt(new_prompt)
            last_prompt = new_prompt

            await asyncio.sleep(1)

            # update tools
            new_tools = self.build_tools()
            if last_tools != new_tools:
                await self.hume.set_tools(new_tools)
            last_tools = new_tools

    async def hume_on_open(self):
        pass

    async def hume_on_close(self):
        pass

    async def hume_on_message(self, message: SubscribeEvent):
        # Create an empty dictionary to store expression inference scores
        if message.type == "chat_metadata":
            # can be used to continue a conversation later on
            self.hume_chat_id = message.chat_id
            self.hume_chat_group_id = message.chat_group_id
            pass
        elif message.type == "assistant_message":
            role = message.message.role.upper()
            message_text = message.message.content
            if message_text:
                await self.hume_process_assistant_message(message_text)
            tool_call = message.message.tool_call
            if tool_call:
                self.threaded_execution(self.hume_process_tool_call, tool_call)
        elif message.type == "tool_call":
            self.threaded_execution(self.hume_process_tool_call, message)
        elif message.type == "assistant_end":
            # signifies the end of the response from the assistant
            pass
        elif message.type == "user_message":
            role = message.message.role.upper()
            message_text = message.message.content
            await self.hume_process_user_message(message_text)
        elif message.type == "audio_output":
            # audio is currently handled by the client itself
            pass
        elif message.type == "error":
            error_message: str = message.message
            error_code: str = message.code
            await self.hume_on_error(f"({error_code}) {error_message}")

    async def hume_on_error(self, error):
        printr.print(f"Hume error: {error}", color=LogType.ERROR)

    async def hume_process_tool_call(self, tool_call):
        tool_name = tool_call.name
        tool_call_id = tool_call.tool_call_id
        tool_type = tool_call.tool_type
        parameters = tool_call.parameters
        response_required = tool_call.response_required

        if tool_type == "builtin" or not response_required:
            # built in tools don't need Wingman AI handling
            return

        try:
            # parameters is a stringified JSON schema
            parameters = json.loads(parameters)
            function_response, instant_response, skill = await self.hume_execute_command_by_function_call(
                tool_name,
                parameters,
            )

            await self.hume.send_tool_response(
                tool_call_id=tool_call_id,
                content=str(function_response),
                tool_name=tool_name,
            )
        except Exception as e:
            await self.hume.send_tool_error(
                tool_call_id=tool_call_id,
                content=f"Error while processing tool call.",
                error=str(e),
            )
            await printr.print_async(
                f"Error while processing tool call: {str(e)}", color=LogType.ERROR
            )
            printr.print(
                traceback.format_exc(), color=LogType.ERROR, server_only=True
            )

    async def hume_execute_command_by_function_call(
        self, function_name: str, function_args: dict[str, any]
    ) -> tuple[str, str | None, Skill | None] | None:
        function_response = ""
        instant_response = ""

        used_skill = None
        if function_name == "execute_command":
            # get the command based on the argument passed by the LLM
            command = self.get_command(function_args["command_name"])
            # execute the command
            function_response = await self._execute_command(command)
            # if the command has responses, we have to play one of them
            if command and command.responses:
                instant_response = self._select_command_response(command)
                await self.hume.send_assistant_input(instant_response)

        # Go through the skills and check if the function name matches any of the tools
        if function_name in self.tool_skills:
            skill = self.tool_skills[function_name]

            benchmark = Benchmark(f"Processing Skill '{skill.name}'")
            await printr.print_async(
                f"Processing Skill '{skill.name}'",
                color=LogType.INFO,
                skill_name=skill.name,
            )

            try:
                function_response, instant_response = await skill.execute_tool(
                    tool_name=function_name,
                    parameters=function_args,
                    benchmark=benchmark,
                )
                used_skill = skill
                if instant_response:
                    await self.hume.send_assistant_input(instant_response)
            except Exception as e:
                await printr.print_async(
                    f"Error while processing Skill '{skill.name}': {str(e)}",
                    color=LogType.ERROR,
                )
                printr.print(
                    traceback.format_exc(), color=LogType.ERROR, server_only=True
                )
                function_response = (
                    "ERROR DURING PROCESSING"  # hints to AI that there was an error
                )
                instant_response = None
            finally:
                await printr.print_async(
                    f"Finished processing Skill '{skill.name}'",
                    color=LogType.INFO,
                    benchmark_result=benchmark.finish(),
                    skill_name=skill.name,
                )

        return function_response, instant_response, used_skill

    async def hume_process_user_message(self, message: str):
        await self.add_user_message(message)
        await printr.print_async(
            f"{message}",
            color=LogType.PURPLE,
            source_name="User",
            source=LogSource.USER,
        )

    async def hume_process_assistant_message(self, message: str):
        await self.add_assistant_message(message)
        await printr.print_async(
            f"{message}",
            color=LogType.POSITIVE,
            source=LogSource.WINGMAN,
            source_name=self.name,
            skill_name="",
        )

    async def unload_skills(self):
        await super().unload_skills()
        self.tool_skills = {}
        self.skill_tools = []
        await self.hume.set_tools(self.build_tools())

    async def prepare_skill(self, skill: Skill):
        # prepare the skill and skill tools
        try:
            for tool_name, tool in skill.get_tools():
                self.tool_skills[tool_name] = skill
                self.skill_tools.append(tool)
        except Exception as e:
            await printr.print_async(
                f"Error while preparing skill '{skill.name}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

        # init skill methods
        skill.llm_call = self.actual_llm_call

    async def add_user_message(self, content: str):
        """Shortens the conversation history if needed and adds a user message to it.

        Args:
            content (str): The message content to add.
        """
        # call skill hooks
        for skill in self.skills:
            await skill.on_add_user_message(content)

        msg = {"role": "user", "content": content}
        await self._cleanup_conversation_history()
        self.messages.append(msg)

    async def add_assistant_message(self, content: str):
        """Adds an assistant message to the conversation history.

        Args:
            content (str): The message content to add.
        """
        # call skill hooks
        for skill in self.skills:
            await skill.on_add_assistant_message(content, [])

        msg = {"role": "assistant", "content": content}
        self.messages.append(msg)

    async def _try_instant_activation(self, transcript: str) -> (str, bool):
        """Not relevant for Hume AI Wingman."""
        return None, False

    async def get_context(self):
        """build the context and inserts it into the messages"""
        skill_prompts = ""
        for skill in self.skills:
            prompt = await skill.get_prompt()
            if prompt:
                skill_prompts += "\n\n" + skill.name + "\n\n" + prompt

        context = self.config.prompts.system_prompt.format(
            backstory=self.config.prompts.backstory, skills=skill_prompts
        )
        return context

    def build_tools(self) -> list[dict]:
        """
        Builds a tool for each command that is not instant_activation.

        Returns:
            list[dict]: A list of tool descriptors in OpenAI format.
        """
        commands = [
            command.name
            for command in self.config.commands
            if not command.force_instant_activation
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "execute_command",
                    "description": "Executes a command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command_name": {
                                "type": "string",
                                "description": "The name of the command to execute",
                                "enum": commands,
                            },
                        },
                        "required": ["command_name"],
                    },
                },
            },
        ]

        # extend with skill tools
        for tool in self.skill_tools:
            tools.append(tool)

        return tools
