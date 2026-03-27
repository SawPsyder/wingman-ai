import json
import re
import time
import asyncio
import queue as thread_queue
import random
import threading
import traceback
import uuid
from datetime import datetime
from typing import (
    Mapping,
    Optional,
)
from openai import NOT_GIVEN
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessage,
    ChatCompletionMessageToolCall,
    ParsedFunction,
)
import requests
from api.interface import (
    OpenRouterEndpointResult,
    SettingsConfig,
    SoundConfig,
    WingmanInitializationError,
    CommandConfig,
)
from api.enums import (
    ImageGenerationProvider,
    LogType,
    LogSource,
    TtsProvider,
    SttProvider,
    ConversationProvider,
    WingmanProSttProvider,
    WingmanProTtsProvider,
    WingmanInitializationErrorType,
    CommandTag,
)
from providers.edge import Edge
from providers.elevenlabs import ElevenLabs
from providers.google import GoogleGenAI
from core.sentence_splitter import SentenceSplitter
from core.thinking_filter import ThinkBlockFilter
from services.tts_queue import TTSQueue
from providers.open_ai import OpenAi, OpenAiAzure, OpenAiCompatibleTts
from providers.hume import Hume
from providers.inworld import Inworld
from providers.open_ai import OpenAi, OpenAiAzure
from providers.x_ai import XAi
from providers.wingman_pro import WingmanPro
from api.commands import McpStateChangedCommand
from services.benchmark import Benchmark
from services.markdown import cleanup_text
from services.printr import Printr
from services.skill_registry import SkillRegistry
from services.mcp_client import McpClient
from services.mcp_registry import McpRegistry
from services.capability_registry import CapabilityRegistry
from skills.skill_base import Skill
from wingmen.wingman import Wingman

printr = Printr()


class OpenAiWingman(Wingman):
    """Our OpenAI Wingman base gives you everything you need to interact with OpenAI's various APIs.

    It transcribes speech to text using Whisper, uses the Completion API for conversation and implements the Tools API to execute functions.
    """

    AZURE_SERVICES = {
        "tts": TtsProvider.AZURE,
        "whisper": [SttProvider.AZURE, SttProvider.AZURE_SPEECH],
        "conversation": ConversationProvider.AZURE,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.edge_tts = Edge()

        # validate will set these:
        self.openai: OpenAi | None = None
        self.mistral: OpenAi | None = None
        self.groq: OpenAi | None = None
        self.cerebras: OpenAi | None = None
        self.openrouter: OpenAi | None = None
        self.openrouter_model_supports_tools = False
        self.local_llm: OpenAi | None = None
        self.openai_azure: OpenAiAzure | None = None
        self.elevenlabs: ElevenLabs | None = None
        self.openai_compatible_tts: OpenAiCompatibleTts | None = None
        self.hume: Hume | None = None
        self.inworld: Inworld | None = None
        self.wingman_pro: WingmanPro | None = None
        self.google: GoogleGenAI | None = None
        self.perplexity: OpenAi | None = None
        self.xai: XAi | None = None

        # tool queue
        self.pending_tool_calls = []
        self.last_gpt_call = None

        # generated addional content
        self.instant_responses = []
        self.last_used_instant_responses = []

        self.messages = []
        """The conversation history that is used for the GPT calls"""

        self.azure_api_keys = {key: None for key in self.AZURE_SERVICES}

        self.tool_skills: dict[str, Skill] = {}
        self.skill_tools: list[dict] = []

        # Progressive tool disclosure registry (MCP-inspired token optimization)
        # Only meta-tools are sent to LLM initially; skills activated on-demand
        self.skill_registry = SkillRegistry()

        # MCP (Model Context Protocol) support
        # Allows connecting to external MCP servers that provide additional tools
        self.mcp_client = McpClient(wingman_name=self.name)
        self.mcp_registry = McpRegistry(
            self.mcp_client,
            wingman_name=self.name,
            on_state_changed=self._broadcast_mcp_state_changed,
        )

        # Unified capability registry - combines skill and MCP discovery
        # From the LLM's perspective, both are just "capabilities"
        self.capability_registry = CapabilityRegistry(
            self.skill_registry, self.mcp_registry
        )

        # Streaming support for LLM response with TTS
        # These are initialized in validate() once config is available
        self.sentence_splitter: SentenceSplitter | None = None
        self.tts_queue: TTSQueue | None = None

        # Dedicated TTS thread — runs its own event loop so TTS API calls and
        # audio playback are completely isolated from the main event loop and
        # cannot be stalled by LLM streaming or other work on the main loop.
        self._tts_thread: threading.Thread | None = None
        self._tts_loop: asyncio.AbstractEventLoop | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._tts_consumer_task: asyncio.Task | None = None
        self._message_complete_fired: bool = False

    def _broadcast_mcp_state_changed(self):
        """Broadcast MCP state change to UI via WebSocket."""
        if printr._connection_manager:
            printr.ensure_async(
                printr._connection_manager.broadcast(
                    McpStateChangedCommand(wingman_name=self.name)
                )
            )

    async def validate(self):
        errors = await super().validate()

        try:
            if self.uses_provider("whispercpp"):
                self.whispercpp.validate(self.name, errors)

            if self.uses_provider("fasterwhisper"):
                self.fasterwhisper.validate(errors)

            if self.uses_provider("pocket_tts"):
                self.pocket_tts.validate(errors)

            if self.uses_provider("openai"):
                await self.validate_and_set_openai(errors)

            if self.uses_provider("mistral"):
                await self.validate_and_set_mistral(errors)

            if self.uses_provider("groq"):
                await self.validate_and_set_groq(errors)

            if self.uses_provider("cerebras"):
                await self.validate_and_set_cerebras(errors)

            if self.uses_provider("google"):
                await self.validate_and_set_google(errors)

            if self.uses_provider("openrouter"):
                await self.validate_and_set_openrouter(errors)

            if self.uses_provider("local_llm"):
                await self.validate_and_set_local_llm(errors)

            if self.uses_provider("elevenlabs"):
                await self.validate_and_set_elevenlabs(errors)

            if self.uses_provider("openai_compatible"):
                await self.validate_and_set_openai_compatible_tts(errors)

            if self.uses_provider("azure"):
                await self.validate_and_set_azure(errors)

            if self.uses_provider("wingman_pro"):
                await self.validate_and_set_wingman_pro()

            if self.uses_provider("perplexity"):
                await self.validate_and_set_perplexity(errors)

            if self.uses_provider("xai"):
                await self.validate_and_set_xai(errors)

            if self.uses_provider("hume"):
                await self.validate_and_set_hume(errors)

            if self.uses_provider("inworld"):
                await self.validate_and_set_inworld(errors)

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

        # Initialize streaming components if enabled
        if hasattr(self.config.features, 'streaming') and self.config.features.streaming.enabled:
            self.sentence_splitter = SentenceSplitter(
                min_sentence_length=self.config.features.streaming.min_sentence_length
            )
            self.tts_queue = TTSQueue()
            self.think_filter = ThinkBlockFilter()  # Stateful filter for think blocks

        return errors

    def uses_provider(self, provider_type: str):
        if provider_type == "openai":
            return any(
                [
                    self.config.features.tts_provider == TtsProvider.OPENAI,
                    self.config.features.stt_provider == SttProvider.OPENAI,
                    self.config.features.conversation_provider
                    == ConversationProvider.OPENAI,
                    self.config.features.image_generation_provider
                    == ImageGenerationProvider.OPENAI,
                ]
            )
        elif provider_type == "mistral":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.MISTRAL,
                ]
            )
        elif provider_type == "groq":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.GROQ,
                    self.config.features.stt_provider == SttProvider.GROQ,
                ]
            )
        elif provider_type == "cerebras":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.CEREBRAS,
                ]
            )
        elif provider_type == "google":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.GOOGLE,
                ]
            )
        elif provider_type == "openrouter":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.OPENROUTER,
                ]
            )
        elif provider_type == "local_llm":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.LOCAL_LLM,
                ]
            )
        elif provider_type == "azure":
            return any(
                [
                    self.config.features.tts_provider == TtsProvider.AZURE,
                    self.config.features.stt_provider == SttProvider.AZURE,
                    self.config.features.stt_provider == SttProvider.AZURE_SPEECH,
                    self.config.features.conversation_provider
                    == ConversationProvider.AZURE,
                ]
            )
        elif provider_type == "edge_tts":
            return self.config.features.tts_provider == TtsProvider.EDGE_TTS
        elif provider_type == "elevenlabs":
            return self.config.features.tts_provider == TtsProvider.ELEVENLABS
        elif provider_type == "openai_compatible":
            return self.config.features.tts_provider == TtsProvider.OPENAI_COMPATIBLE
        elif provider_type == "pocket_tts":
            return self.config.features.tts_provider == TtsProvider.POCKET_TTS
        elif provider_type == "hume":
            return self.config.features.tts_provider == TtsProvider.HUME
        elif provider_type == "inworld":
            return self.config.features.tts_provider == TtsProvider.INWORLD
        elif provider_type == "xvasynth":
            return self.config.features.tts_provider == TtsProvider.XVASYNTH
        elif provider_type == "whispercpp":
            return self.config.features.stt_provider == SttProvider.WHISPERCPP
        elif provider_type == "fasterwhisper":
            return self.config.features.stt_provider == SttProvider.FASTER_WHISPER
        elif provider_type == "wingman_pro":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.WINGMAN_PRO,
                    self.config.features.tts_provider == TtsProvider.WINGMAN_PRO,
                    self.config.features.stt_provider == SttProvider.WINGMAN_PRO,
                    self.config.features.image_generation_provider
                    == ImageGenerationProvider.WINGMAN_PRO,
                ]
            )
        elif provider_type == "perplexity":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.PERPLEXITY,
                ]
            )
        elif provider_type == "xai":
            return any(
                [
                    self.config.features.conversation_provider
                    == ConversationProvider.XAI,
                ]
            )
        return False

    async def prepare(self):
        try:
            if self.config.features.use_generic_instant_responses:
                printr.print(
                    "Generating AI instant responses...",
                    color=LogType.WARNING,
                    server_only=True,
                )
                self.threaded_execution(self._generate_instant_responses)
        except Exception as e:
            await printr.print_async(
                f"Error while preparing wingman '{self.name}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

    async def unload_skills(self):
        await super().unload_skills()
        self.tool_skills = {}
        self.skill_tools = []
        self.skill_registry.clear()

    async def unload_mcps(self):
        """Disconnect from all MCP servers."""
        await self.mcp_registry.clear()

    async def enable_mcp(self, mcp_name: str) -> tuple[bool, str]:
        """Enable and connect to a single MCP server without reinitializing all MCPs.

        Args:
            mcp_name: The name of the MCP server to enable

        Returns:
            (success, message) tuple
        """
        # Check if MCP SDK is available
        if not self.mcp_client.is_available:
            return False, "MCP SDK not installed."

        # Check if already connected
        if mcp_name in self.mcp_registry.get_connected_server_names():
            return True, f"MCP server '{mcp_name}' is already connected."

        # Find the MCP config from central mcp.yaml
        central_mcp_config = self.tower.config_manager.mcp_config
        mcp_configs = central_mcp_config.servers if central_mcp_config else []

        mcp_config = None
        for cfg in mcp_configs:
            if cfg.name == mcp_name:
                mcp_config = cfg
                break

        if not mcp_config:
            return False, f"MCP server '{mcp_name}' not found in mcp.yaml."

        try:
            # Build headers with secrets (same logic as init_mcps)
            headers = {}
            if mcp_config.headers:
                headers.update(mcp_config.headers)

            # Check for API key in secrets
            secret_key = f"mcp_{mcp_config.name}"
            api_key = await self.secret_keeper.retrieve(
                requester=self.name,
                key=secret_key,
                prompt_if_missing=False,
            )
            if api_key:
                if not any(
                    k.lower() in ["authorization", "api-key", "x-api-key"]
                    for k in headers.keys()
                ):
                    headers["Authorization"] = f"Bearer {api_key}"

            # Connect with timeout
            default_timeout = 60.0 if mcp_config.type.value == "stdio" else 30.0
            timeout = (
                float(mcp_config.timeout) if mcp_config.timeout else default_timeout
            )

            connection = await asyncio.wait_for(
                self.mcp_registry.register_server(
                    config=mcp_config,
                    headers=headers if headers else None,
                ),
                timeout=timeout,
            )

            if connection.is_connected:
                tool_count = len(connection.tools)
                return True, f"MCP server '{mcp_name}' enabled with {tool_count} tools."
            else:
                error = connection.error or "Connection failed."
                return False, f"MCP server '{mcp_name}' failed to connect: {error}"

        except asyncio.TimeoutError:
            error_msg = f"Connection timed out ({int(timeout)}s)."
            self.mcp_registry.set_server_error(mcp_name, error_msg)
            return False, f"MCP server '{mcp_name}': {error_msg}"

        except Exception as e:
            error_msg = f"Error enabling MCP '{mcp_name}': {str(e)}"
            await printr.print_async(error_msg, color=LogType.ERROR)
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
            return False, error_msg

    async def disable_mcp(self, mcp_name: str) -> tuple[bool, str]:
        """Disable and disconnect from a single MCP server without affecting other MCPs.

        Args:
            mcp_name: The name of the MCP server to disable

        Returns:
            (success, message) tuple
        """
        # Check if the MCP is connected
        if mcp_name not in self.mcp_registry.get_connected_server_names():
            return True, f"MCP server '{mcp_name}' is already disconnected."

        try:
            await self.mcp_registry.unregister_server(mcp_name)
            return True, f"MCP server '{mcp_name}' disabled."

        except Exception as e:
            error_msg = f"Error disabling MCP '{mcp_name}': {str(e)}"
            await printr.print_async(error_msg, color=LogType.ERROR)
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
            return False, error_msg

    async def prepare_skill(self, skill: Skill):
        # prepare the skill and skill tools
        try:
            for tool_name, tool in skill.get_tools():
                self.tool_skills[tool_name] = skill
                self.skill_tools.append(tool)

            # Register with the progressive disclosure registry
            self.skill_registry.register_skill(skill)

            # Auto-activated skills need to be validated/prepared immediately
            # so their hooks (like on_play_to_user) will work
            if skill.config.auto_activate:
                success, message = await skill.ensure_activated()
                if not success:
                    await printr.print_async(
                        f"Auto-activated skill '{skill.config.display_name}' failed to activate: {message}",
                        color=LogType.ERROR,
                    )
        except Exception as e:
            await printr.print_async(
                f"Error while preparing skill '{skill.name}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

        # init skill methods
        skill.llm_call = self.actual_llm_call

    async def unprepare_skill(self, skill: Skill):
        """Remove a skill's tools and registrations when it's disabled."""
        try:
            # Remove tool mappings
            for tool_name, _ in skill.get_tools():
                self.tool_skills.pop(tool_name, None)
                # Remove from skill_tools list
                self.skill_tools = [
                    t
                    for t in self.skill_tools
                    if t.get("function", {}).get("name") != tool_name
                ]

            # Unregister from the progressive disclosure registry
            self.skill_registry.unregister_skill(skill.name)
        except Exception as e:
            await printr.print_async(
                f"Error while unpreparing skill '{skill.name}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

    async def init_mcps(self) -> list[WingmanInitializationError]:
        """
        Initialize MCP (Model Context Protocol) server connections.

        Loads MCP servers from central mcp.yaml config, only connecting those in wingman's discoverable_mcps.
        MCP servers provide external tools similar to skills.

        Returns:
            list[WingmanInitializationError]: Errors encountered (non-fatal, wingman still loads)
        """
        errors = []

        # Check if MCP SDK is available
        if not self.mcp_client.is_available:
            printr.print(
                f"[{self.name}] MCP SDK not installed, skipping MCP initialization.",
                color=LogType.WARNING,
                server_only=True,
            )
            return errors

        # Disconnect existing MCP servers
        await self.unload_mcps()

        # Get MCP configs from central mcp.yaml
        central_mcp_config = self.tower.config_manager.mcp_config
        mcp_configs = central_mcp_config.servers if central_mcp_config else []
        if not mcp_configs:
            return errors

        # Get discoverable MCPs list (whitelist) from wingman config
        discoverable_mcps = self.config.discoverable_mcps

        # Filter to only discoverable MCPs
        mcps_to_connect = [mcp for mcp in mcp_configs if mcp.name in discoverable_mcps]

        if not mcps_to_connect:
            return errors

        # Prepare connection tasks for parallel execution
        async def connect_mcp(mcp_config):
            """Connect to a single MCP server. Returns (success, connection_info, errors)."""
            local_errors = []
            try:
                # Build headers with secrets
                headers = {}
                if mcp_config.headers:
                    headers.update(mcp_config.headers)

                # Check for API key in secrets (using mcp_ prefix)
                secret_key = f"mcp_{mcp_config.name}"
                api_key = await self.secret_keeper.retrieve(
                    requester=self.name,
                    key=secret_key,
                    prompt_if_missing=False,
                )
                if api_key:
                    printr.print(
                        f"MCP secret '{secret_key}' found ({len(api_key)} chars)",
                        color=LogType.INFO,
                        source_name=self.name,
                        server_only=True,
                    )
                    if not any(
                        k.lower() in ["authorization", "api-key", "x-api-key"]
                        for k in headers.keys()
                    ):
                        headers["Authorization"] = f"Bearer {api_key}"

                # Connect with timeout
                default_timeout = 60.0 if mcp_config.type.value == "stdio" else 30.0
                timeout = (
                    float(mcp_config.timeout) if mcp_config.timeout else default_timeout
                )

                try:
                    connection = await asyncio.wait_for(
                        self.mcp_registry.register_server(
                            config=mcp_config,
                            headers=headers if headers else None,
                        ),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    error_msg = f"MCP '{mcp_config.display_name}' connection timed out ({int(timeout)}s)."
                    printr.print(
                        error_msg,
                        color=LogType.WARNING,
                        source_name=self.name,
                        server_only=True,
                    )
                    local_errors.append(
                        WingmanInitializationError(
                            wingman_name=self.name,
                            message=error_msg,
                            error_type=WingmanInitializationErrorType.MCP_CONNECTION_FAILED,
                        )
                    )
                    return (False, None, local_errors)

                if connection.is_connected:
                    return (
                        True,
                        f"{mcp_config.display_name} ({len(connection.tools)} tools)",
                        local_errors,
                    )
                else:
                    error_msg = f"MCP '{mcp_config.display_name}' failed to connect: {connection.error}"
                    local_errors.append(
                        WingmanInitializationError(
                            wingman_name=self.name,
                            message=error_msg,
                            error_type=WingmanInitializationErrorType.MCP_CONNECTION_FAILED,
                        )
                    )
                    return (False, None, local_errors)

            except Exception as e:
                error_msg = f"MCP '{mcp_config.name}' initialization error: {str(e)}"
                printr.print(
                    error_msg,
                    color=LogType.ERROR,
                    source_name=self.name,
                    server_only=True,
                )
                printr.print(
                    traceback.format_exc(), color=LogType.ERROR, server_only=True
                )
                local_errors.append(
                    WingmanInitializationError(
                        wingman_name=self.name,
                        message=error_msg,
                        error_type=WingmanInitializationErrorType.MCP_CONNECTION_FAILED,
                    )
                )
                return (False, None, local_errors)

        # Connect to all MCPs in parallel
        connection_tasks = [connect_mcp(mcp) for mcp in mcps_to_connect]
        results = await asyncio.gather(*connection_tasks)

        # Collect results
        connected_count = 0
        connected_names = []
        for success, connection_info, mcp_errors in results:
            if success:
                connected_count += 1
                connected_names.append(connection_info)
            errors.extend(mcp_errors)

        # Log consolidated MCP status for this wingman
        if connected_count > 0:
            await printr.print_async(
                f"Discoverable MCP servers connected ({connected_count}): {', '.join(connected_names)}",
                color=LogType.WINGMAN,
                source=LogSource.WINGMAN,
                source_name=self.name,
                server_only=not self.settings.debug_mode,
            )

        return errors

    async def validate_and_set_openai(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("openai", errors)
        if api_key:
            self.openai = OpenAi(
                api_key=api_key,
                organization=self.config.openai.organization,
                base_url=self.config.openai.base_url,
            )

    async def validate_and_set_mistral(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("mistral", errors)
        if api_key:
            # TODO: maybe use their native client (or LangChain) instead of OpenAI(?)
            self.mistral = OpenAi(
                api_key=api_key,
                organization=self.config.openai.organization,
                base_url=self.config.mistral.endpoint,
            )

    async def validate_and_set_groq(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("groq", errors)
        if api_key:
            # TODO: maybe use their native client (or LangChain) instead of OpenAI(?)
            self.groq = OpenAi(
                api_key=api_key,
                base_url=self.config.groq.endpoint,
            )

    async def validate_and_set_cerebras(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("cerebras", errors)
        if api_key:
            # TODO: maybe use their native client (or LangChain) instead of OpenAI(?)
            self.cerebras = OpenAi(
                api_key=api_key,
                base_url=self.config.cerebras.endpoint,
            )

    async def validate_and_set_google(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("google", errors)
        if api_key:
            self.google = GoogleGenAI(api_key=api_key)

    async def validate_and_set_openrouter(
        self, errors: list[WingmanInitializationError]
    ):
        api_key = await self.retrieve_secret("openrouter", errors)

        async def does_openrouter_model_support_tools(model_id: str):
            if not model_id:
                return False
            response = requests.get(
                url=f"https://openrouter.ai/api/v1/models/{model_id}/endpoints",
                timeout=10,
            )
            response.raise_for_status()
            content = response.json()
            result = OpenRouterEndpointResult(**content.get("data", {}))
            supports_tools = any(
                all(
                    p in (endpoint.supported_parameters or [])
                    for p in ["tools", "tool_choice"]
                )
                for endpoint in result.endpoints
            )
            if not supports_tools:
                printr.print(
                    f"{self.name}: OpenRouter model {model_id} does not support tools, so they'll be omitted from calls.",
                    source=LogSource.WINGMAN,
                    source_name=self.name,
                    color=LogType.WARNING,
                    server_only=True,
                )
            return supports_tools

        if api_key:
            self.openrouter = OpenAi(
                api_key=api_key,
                base_url=self.config.openrouter.endpoint,
            )
            self.openrouter_model_supports_tools = (
                await does_openrouter_model_support_tools(
                    self.config.openrouter.conversation_model
                )
            )

    async def validate_and_set_local_llm(
        self, errors: list[WingmanInitializationError]
    ):
        api_key = await self.retrieve_secret("local_llm", errors)
        if api_key:
            self.local_llm = OpenAi(
                api_key=api_key,
                base_url=self.config.local_llm.endpoint,
            )

    async def validate_and_set_elevenlabs(
        self, errors: list[WingmanInitializationError]
    ):
        api_key = await self.retrieve_secret("elevenlabs", errors)
        if api_key:
            self.elevenlabs = ElevenLabs(
                api_key=api_key,
                wingman_name=self.name,
            )
            self.elevenlabs.validate_config(
                config=self.config.elevenlabs, errors=errors
            )

    async def validate_and_set_openai_compatible_tts(
        self, errors: list[WingmanInitializationError]
    ):
        if (
            self.config.openai_compatible_tts.base_url
            and self.config.openai_compatible_tts.api_key
        ):
            self.openai_compatible_tts = OpenAiCompatibleTts(
                api_key=self.config.openai_compatible_tts.api_key,
                base_url=self.config.openai_compatible_tts.base_url,
            )
            printr.print(
                f"Wingman {self.name}: Initialized OpenAI-compatible TTS with base URL {self.config.openai_compatible_tts.base_url} and API key {self.config.openai_compatible_tts.api_key}",
                server_only=True,
            )

    async def validate_and_set_hume(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("hume", errors)
        if api_key:
            self.hume = Hume(
                api_key=api_key,
                wingman_name=self.name,
            )
            self.hume.validate_config(config=self.config.hume, errors=errors)

    async def validate_and_set_inworld(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("inworld", errors)
        if api_key:
            self.inworld = Inworld(
                api_key=api_key,
                wingman_name=self.name,
            )
            self.inworld.validate_config(config=self.config.inworld, errors=errors)

    async def validate_and_set_azure(self, errors: list[WingmanInitializationError]):
        for key_type in self.AZURE_SERVICES:
            if self.uses_provider("azure"):
                api_key = await self.retrieve_secret(f"azure_{key_type}", errors)
                if api_key:
                    self.azure_api_keys[key_type] = api_key
        if len(errors) == 0:
            self.openai_azure = OpenAiAzure()

    async def validate_and_set_wingman_pro(self):
        self.wingman_pro = WingmanPro(
            wingman_name=self.name, settings=self.settings.wingman_pro
        )

    async def validate_and_set_perplexity(
        self, errors: list[WingmanInitializationError]
    ):
        api_key = await self.retrieve_secret("perplexity", errors)
        if api_key:
            self.perplexity = OpenAi(
                api_key=api_key,
                base_url=self.config.perplexity.endpoint,
            )

    async def validate_and_set_xai(self, errors: list[WingmanInitializationError]):
        api_key = await self.retrieve_secret("xai", errors)
        if api_key:
            self.xai = XAi(
                api_key=api_key,
                base_url=self.config.xai.endpoint,
            )

    # overrides the base class method
    async def update_settings(self, settings: SettingsConfig):
        """Update the settings of the Wingman. This method should always be called when the user Settings have changed."""
        try:
            await super().update_settings(settings)

            if self.uses_provider("wingman_pro"):
                await self.validate_and_set_wingman_pro()
                printr.print(
                    f"Wingman {self.name}: reinitialized Wingman Pro with new settings",
                    server_only=True,
                )
        except Exception as e:
            await printr.print_async(
                f"Error while updating settings for wingman '{self.name}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

    async def _generate_instant_responses(self) -> None:
        """Generates general instant responses based on given context."""
        context = await self.get_context()
        messages = [
            {
                "role": "system",
                "content": """
                Generate a list in JSON format of at least 20 short direct text responses.
                Make sure the response only contains the JSON, no additional text.
                They must fit the described character in the given context by the user.
                Every generated response must be generally usable in every situation.
                Responses must show its still in progress and not in a finished state.
                The user request this response is used on is unknown. Therefore it must be generic.
                Good examples:
                    - "Processing..."
                    - "Stand by..."

                Bad examples:
                    - "Generating route..." (too specific)
                    - "I'm sorry, I can't do that." (too negative)

                Response example:
                [
                    "OK",
                    "Generating results...",
                    "Roger that!",
                    "Stand by..."
                ]
            """,
            },
            {"role": "user", "content": context},
        ]
        try:
            completion = await self.actual_llm_call(messages)
            if completion is None:
                return
            if completion.choices[0].message.content:
                retry_limit = 3
                retry_count = 1
                valid = False
                while not valid and retry_count <= retry_limit:
                    try:
                        responses = json.loads(completion.choices[0].message.content)
                        valid = True
                        for response in responses:
                            if response not in self.instant_responses:
                                self.instant_responses.append(str(response))
                    except json.JSONDecodeError:
                        messages.append(completion.choices[0].message)
                        messages.append(
                            {
                                "role": "user",
                                "content": "It was tried to handle the response in its entirety as a JSON string. Fix response to be a pure, valid JSON, it was not convertable.",
                            }
                        )
                        if retry_count <= retry_limit:
                            completion = await self.actual_llm_call(messages)
                        retry_count += 1
        except Exception as e:
            await printr.print_async(
                f"Error while generating instant responses: {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

    async def _transcribe(self, audio_input_wav: str) -> str | None:
        """Transcribes the recorded audio to text using the OpenAI Whisper API.

        Args:
            audio_input_wav (str): The path to the audio file that contains the user's speech. This is a recording of what you you said.

        Returns:
            str | None: The transcript of the audio file or None if the transcription failed.
        """
        transcript = None

        try:
            if self.config.features.stt_provider == SttProvider.AZURE:
                transcript = self.openai_azure.transcribe_whisper(
                    filename=audio_input_wav,
                    api_key=self.azure_api_keys["whisper"],
                    config=self.config.azure.whisper,
                )
            elif self.config.features.stt_provider == SttProvider.AZURE_SPEECH:
                transcript = self.openai_azure.transcribe_azure_speech(
                    filename=audio_input_wav,
                    api_key=self.azure_api_keys["tts"],
                    config=self.config.azure.stt,
                )
            elif self.config.features.stt_provider == SttProvider.WHISPERCPP:
                transcript = self.whispercpp.transcribe(
                    filename=audio_input_wav, config=self.config.whispercpp
                )
            elif self.config.features.stt_provider == SttProvider.FASTER_WHISPER:
                hotwords: list[str] = []
                # add my name
                hotwords.append(self.name)
                # add default hotwords
                default_hotwords = self.config.fasterwhisper.hotwords
                if default_hotwords and len(default_hotwords) > 0:
                    hotwords.extend(default_hotwords)
                # and my additional hotwords
                wingman_hotwords = self.config.fasterwhisper.additional_hotwords
                if wingman_hotwords and len(wingman_hotwords) > 0:
                    hotwords.extend(wingman_hotwords)

                transcript = self.fasterwhisper.transcribe(
                    filename=audio_input_wav,
                    config=self.config.fasterwhisper,
                    hotwords=list(set(hotwords)),
                )
            elif self.config.features.stt_provider == SttProvider.WINGMAN_PRO:
                if (
                    self.config.wingman_pro.stt_provider
                    == WingmanProSttProvider.WHISPER
                ):
                    transcript = self.wingman_pro.transcribe_whisper(
                        filename=audio_input_wav
                    )
                elif (
                    self.config.wingman_pro.stt_provider
                    == WingmanProSttProvider.AZURE_SPEECH
                ):
                    transcript = self.wingman_pro.transcribe_azure_speech(
                        filename=audio_input_wav, config=self.config.azure.stt
                    )
            elif self.config.features.stt_provider == SttProvider.OPENAI:
                transcript = self.openai.transcribe(filename=audio_input_wav)
            elif self.config.features.stt_provider == SttProvider.GROQ:
                transcript = self.groq.transcribe(
                    filename=audio_input_wav, model="whisper-large-v3-turbo"
                )
        except Exception as e:
            await printr.print_async(
                f"Error during transcription using '{self.config.features.stt_provider}': {str(e)}",
                color=LogType.ERROR,
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

        result = None
        if transcript:
            # Wingman Pro might returns a serialized dict instead of a real Azure Speech transcription object
            result = (
                transcript.get("_text")
                if isinstance(transcript, dict)
                else transcript.text
            )

        return result

    async def _get_response_for_transcript(
        self, transcript: str, benchmark: Benchmark
    ) -> tuple[str | None, str | None, Skill | None, bool]:
        """Gets the response for a given transcript.

        This function interprets the transcript, runs instant commands if triggered,
        calls the OpenAI API when needed, processes any tool calls, and generates the final response.

        Args:
            transcript (str): The user's spoken text transcribed.

        Returns:
            tuple[str | None, str | None, Skill | None, bool]: A tuple containing the final response, the instant response (if any), the skill that was used, and a boolean indicating whether the current audio should be interrupted.
        """
        await self.add_user_message(transcript)

        benchmark.start_snapshot("Instant activation commands")
        instant_response, instant_command_executed = await self._try_instant_activation(
            transcript=transcript
        )
        if instant_response:
            await self.add_assistant_message(instant_response)
            benchmark.finish_snapshot()
            if (
                instant_response == "."
            ):  # thats for the "The UI should not give a response" option in commands
                instant_response = None
            return instant_response, instant_response, None, True
        benchmark.finish_snapshot()

        # Track cumulative times for proper aggregation
        llm_processing_time_ms = 0.0
        tool_execution_time_ms = 0.0
        tool_timings: list[tuple[str, float]] = (
            []
        )  # (label, time_ms) for individual tools

        # make a GPT call with the conversation history
        # if an instant command got executed, prevent tool calls to avoid duplicate executions
        llm_start = time.perf_counter()
        completion = await self._llm_call(instant_command_executed is False)
        llm_processing_time_ms += (time.perf_counter() - llm_start) * 1000

        if completion is None:
            self._add_benchmark_snapshot(
                benchmark, "LLM Processing", llm_processing_time_ms
            )
            return None, None, None, True

        response_message, tool_calls = await self._process_completion(completion, instant_command_executed is False)

        # add message and dummy tool responses to conversation history
        is_waiting_response_needed, is_summarize_needed = await self._add_gpt_response(
            response_message, tool_calls
        )
        interrupt = True  # initial answer should be awaited if exists

        while tool_calls:
            if is_waiting_response_needed:
                message = None
                if response_message.content:
                    message = response_message.content
                elif self.instant_responses:
                    message = self._get_random_filler()
                    is_summarize_needed = True
                if message:
                    # Skip play_to_user if streaming already played audio for this content
                    if not getattr(self, '_streaming_played_audio', False):
                        self.threaded_execution(self.play_to_user, message, interrupt)
                    # Reset for subsequent iterations
                    self._streaming_played_audio = False
                    await printr.print_async(
                        f"{message}",
                        color=LogType.POSITIVE,
                        source=LogSource.WINGMAN,
                        source_name=self.name,
                        skill_name="",
                    )
                    interrupt = False
                else:
                    is_summarize_needed = True
            else:
                is_summarize_needed = True

            # Time tool execution and collect individual timings
            tool_start = time.perf_counter()
            instant_response, skill, iteration_timings = await self._handle_tool_calls(
                tool_calls
            )
            tool_execution_time_ms += (time.perf_counter() - tool_start) * 1000
            tool_timings.extend(iteration_timings)

            if instant_response:
                # Add snapshots before returning
                self._add_benchmark_snapshot(
                    benchmark, "LLM Processing", llm_processing_time_ms
                )
                if tool_execution_time_ms > 0:
                    self._add_tool_execution_snapshot(
                        benchmark, tool_execution_time_ms, tool_timings
                    )
                return None, instant_response, None, interrupt

            if is_summarize_needed:
                # Time the follow-up LLM call
                llm_start = time.perf_counter()
                completion = await self._llm_call(True)
                llm_processing_time_ms += (time.perf_counter() - llm_start) * 1000

                if completion is None:
                    self._add_benchmark_snapshot(
                        benchmark, "LLM Processing", llm_processing_time_ms
                    )
                    if tool_execution_time_ms > 0:
                        self._add_tool_execution_snapshot(
                            benchmark, tool_execution_time_ms, tool_timings
                        )
                    return None, None, None, True

                response_message, tool_calls = await self._process_completion(
                    completion
                )
                is_waiting_response_needed, is_summarize_needed = (
                    await self._add_gpt_response(response_message, tool_calls)
                )
                if tool_calls:
                    interrupt = False
            elif is_waiting_response_needed:
                self._add_benchmark_snapshot(
                    benchmark, "LLM Processing", llm_processing_time_ms
                )
                if tool_execution_time_ms > 0:
                    self._add_tool_execution_snapshot(
                        benchmark, tool_execution_time_ms, tool_timings
                    )
                return None, None, None, interrupt

        # Add final snapshots
        self._add_benchmark_snapshot(
            benchmark, "LLM Processing", llm_processing_time_ms
        )
        if tool_execution_time_ms > 0:
            self._add_tool_execution_snapshot(
                benchmark, tool_execution_time_ms, tool_timings
            )

        content = response_message.content
        # If streaming already played TTS audio, return None as process_result
        # to prevent duplicate play_to_user in the base class, but pass the content
        # as instant_response so it still appears in the UI log.
        if getattr(self, '_streaming_played_audio', False):
            self._streaming_played_audio = False
            self._message_complete_fired = False

            # Push the completed message to the client immediately - don't wait
            # for _fire_message_complete / TTS to finish first.
            # Also check for whitespace-only content to avoid showing empty messages
            if content and content.strip():
                await printr.print_async(
                    f"{content}",
                    color=LogType.POSITIVE,
                    source=LogSource.WINGMAN,
                    source_name=self.name,
                    skill_name="",
                    benchmark_result=benchmark.finish(),
                )

            await printr.print_async(
                f"[STREAMING] Streaming return path: content_len={len(content) if content else 0}, about to call _fire_message_complete",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )
            # Notify client and skills that the streamed message is fully complete
            await self._fire_message_complete()
            # Return (None, None) so wingman.py doesn't print or play_to_user again
            return None, None, None, interrupt
        # Check for whitespace-only content to avoid sending empty messages
        display_content = content if content and content.strip() else None
        await printr.print_async(
            f"[STREAMING] Non-streaming return path: content_len={len(display_content) if display_content else 0}",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )
        return display_content, display_content, None, interrupt

    async def _fire_message_complete(self):
        """Wait for TTS audio to finish, then fire MESSAGE_COMPLETE and on_message_complete.

        The audio wait was moved here from the streaming generator so that the
        client receives the completed message text immediately while audio is
        still playing.  This method blocks until all audio has finished, then
        signals completion to the client and skills (e.g. HUD hide).
        """
        # Wait for the TTS consumer task and audio playback
        consumer_task = getattr(self, '_tts_consumer_task', None)
        try:
            if consumer_task:
                printr.print(
                    f"[STREAMING] Waiting for TTS consumer task to finish...",
                    color=LogType.INFO,
                    source_name=self.name,
                    server_only=True,
                )
                await consumer_task
                printr.print(
                    f"[STREAMING] TTS consumer done. Waiting for audio playback...",
                    color=LogType.INFO,
                    source_name=self.name,
                    server_only=True,
                )
                while self.audio_player.is_playing:
                    await asyncio.sleep(0.1)
                printr.print(
                    f"[STREAMING] Audio playback finished.",
                    color=LogType.INFO,
                    source_name=self.name,
                    server_only=True,
                )
        except (asyncio.CancelledError, Exception) as e:
            if not isinstance(e, asyncio.CancelledError):
                await printr.print_async(
                    f"Error waiting for TTS consumer: {str(e)}", color=LogType.ERROR
                )
        finally:
            self._tts_consumer_task = None

        # Guard against double-firing (abort_streaming_playback may have already fired)
        if self._message_complete_fired:
            return
        self._message_complete_fired = True

        printr.print(
            f"[STREAMING] _fire_message_complete: firing MESSAGE_COMPLETE + on_message_complete",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )
        await printr.print_async(
            "Message complete",
            command_tag=CommandTag.MESSAGE_COMPLETE,
            source_name=self.name,
        )
        for skill in self.skills:
            if skill.is_prepared:
                try:
                    await skill.on_message_complete()
                except Exception as e:
                    printr.print(
                        f"Error calling on_message_complete for skill {skill.name}: {e}",
                        color=LogType.WARNING,
                        source_name=self.name,
                        server_only=True,
                    )

    async def abort_streaming_playback(self):
        """Abort any running TTS streaming playback.

        Called when the user manually stops playback (keybind or API).
        Stops the TTS consumer, clears the queue, and fires on_message_complete
        so the HUD message is hidden.
        """
        consumer_task = self._tts_consumer_task
        had_active_stream = consumer_task is not None and not consumer_task.done()

        if self.tts_queue:
            # Signal no more items and clear remaining sentences
            self.tts_queue._closed = True
            await self.tts_queue.clear()

        # Wait briefly for the consumer to notice the closed+empty queue and exit
        if consumer_task and not consumer_task.done():
            try:
                await asyncio.wait_for(consumer_task, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                consumer_task.cancel()
                try:
                    await consumer_task
                except (asyncio.CancelledError, Exception):
                    pass

        self._tts_consumer_task = None

        # Fire message complete so HUD hides and client gets notified
        if had_active_stream and not self._message_complete_fired:
            self._message_complete_fired = True
            printr.print(
                f"[STREAMING] Playback aborted — firing on_message_complete",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )
            await printr.print_async(
                "Message complete",
                command_tag=CommandTag.MESSAGE_COMPLETE,
                source_name=self.name,
            )
            for skill in self.skills:
                if skill.is_prepared:
                    try:
                        await skill.on_message_complete()
                    except Exception:
                        pass

    def _add_benchmark_snapshot(
        self, benchmark: Benchmark, label: str, execution_time_ms: float
    ):
        """Add a snapshot with the given label and execution time."""
        if execution_time_ms >= 1000:
            formatted_time = f"{execution_time_ms/1000:.1f}s"
        else:
            formatted_time = f"{int(execution_time_ms)}ms"

        from api.interface import BenchmarkResult

        benchmark.snapshots.append(
            BenchmarkResult(
                label=label,
                execution_time_ms=execution_time_ms,
                formatted_execution_time=formatted_time,
            )
        )

    def _add_tool_execution_snapshot(
        self,
        benchmark: Benchmark,
        total_time_ms: float,
        tool_timings: list[tuple[str, float]],
    ):
        """Add a tool execution snapshot with nested individual tool timings."""
        from api.interface import BenchmarkResult

        if total_time_ms >= 1000:
            formatted_time = f"{total_time_ms/1000:.1f}s"
        else:
            formatted_time = f"{int(total_time_ms)}ms"

        # Create nested snapshots for individual tools
        nested_snapshots = []
        for label, time_ms in tool_timings:
            if time_ms >= 1000:
                fmt = f"{time_ms/1000:.1f}s"
            else:
                fmt = f"{int(time_ms)}ms"
            nested_snapshots.append(
                BenchmarkResult(
                    label=label,
                    execution_time_ms=time_ms,
                    formatted_execution_time=fmt,
                )
            )

        benchmark.snapshots.append(
            BenchmarkResult(
                label="Tool Execution",
                execution_time_ms=total_time_ms,
                formatted_execution_time=formatted_time,
                snapshots=nested_snapshots if nested_snapshots else None,
            )
        )

    def _get_random_filler(self):
        # get last two used instant responses
        if len(self.last_used_instant_responses) > 2:
            self.last_used_instant_responses = self.last_used_instant_responses[-2:]

        # get a random instant response that was not used in the last two responses
        random_index = random.randint(0, len(self.instant_responses) - 1)
        while random_index in self.last_used_instant_responses:
            random_index = random.randint(0, len(self.instant_responses) - 1)

        # add the index to the last used list and return
        self.last_used_instant_responses.append(random_index)
        return self.instant_responses[random_index]

    async def _fix_tool_calls(self, tool_calls):
        """Fixes tool calls that have a command name as function name.

        Args:
            tool_calls (list): The tool calls to fix.

        Returns:
            list: The fixed tool calls.
        """
        if tool_calls and len(tool_calls) > 0:
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                function_args = (
                    tool_call.function.arguments
                    # Mistral returns a dict
                    if isinstance(tool_call.function.arguments, dict)
                    # OpenAI returns a string
                    else json.loads(tool_call.function.arguments)
                )

                # try to resolve function name to a command name
                if (len(function_args) == 0 and self.get_command(function_name)) or (
                    len(function_args) == 1
                    and "command_name" in function_args
                    and self.get_command(function_args["command_name"])
                    and function_name == function_args["command_name"]
                ):
                    function_args["command_name"] = function_name
                    function_name = "execute_command"

                    # update the tool call
                    tool_call.function.name = function_name
                    tool_call.function.arguments = json.dumps(function_args)

                    if self.settings.debug_mode:
                        await printr.print_async(
                            "Applied command call fix.", color=LogType.WARNING
                        )

        return tool_calls

    async def _add_gpt_response(self, message, tool_calls) -> (bool, bool):
        """Adds a message from GPT to the conversation history as well as adding dummy tool responses for any tool calls.

        Args:
            message (dict | ChatCompletionMessage): The message to add.
            tool_calls (list): The tool calls associated with the message.
        """
        # call skill hooks (only for prepared/activated skills)
        for skill in self.skills:
            if skill.is_prepared:
                await skill.on_add_assistant_message(
                    message.content, message.tool_calls
                )

        # do not tamper with this message as it will lead to 400 errors!
        self.messages.append(message)

        # adding dummy tool responses to prevent corrupted message history on parallel requests
        # and checks if waiting response should be played
        unique_tools = {}
        is_waiting_response_needed = False
        is_summarize_needed = False

        if tool_calls:
            for tool_call in tool_calls:
                if not tool_call.id:
                    continue
                # adding a dummy tool response to get updated later
                self._add_tool_response(tool_call, "Loading..", False)

                function_name = tool_call.function.name

                # Meta-tools (search_skills, activate_skill, etc.) always need a follow-up
                # LLM call so it can use the newly activated tools
                if self.skill_registry.is_meta_tool(function_name):
                    is_summarize_needed = True
                elif function_name in self.tool_skills:
                    skill = self.tool_skills[function_name]
                    if await skill.is_waiting_response_needed(function_name):
                        is_waiting_response_needed = True
                    if await skill.is_summarize_needed(function_name):
                        is_summarize_needed = True

                unique_tools[function_name] = True

            if len(unique_tools) == 1 and "execute_command" in unique_tools:
                is_waiting_response_needed = True

            # If content is empty or only whitespace but tool calls exist, we need a summary LLM call
            # to provide a response for the tool (especially for commands)
            if tool_calls and (not message.content or not message.content.strip()):
                is_summarize_needed = True

        return is_waiting_response_needed, is_summarize_needed

    def _add_tool_response(self, tool_call, response: str, completed: bool = True):
        """Adds a tool response to the conversation history.

        Args:
            tool_call (dict|ChatCompletionMessageToolCall): The tool call to add the dummy response for.
        """
        msg = {"role": "tool", "content": response}
        if tool_call.id is not None:
            msg["tool_call_id"] = tool_call.id
        if tool_call.function.name is not None:
            msg["name"] = tool_call.function.name
        self.messages.append(msg)

        if tool_call.id and not completed:
            self.pending_tool_calls.append(tool_call.id)

    async def _update_tool_response(self, tool_call_id, response) -> bool:
        """Updates a tool response in the conversation history.

        Args:
            tool_call_id (str): The identifier of the tool call to update the response for.
            response (str): The new response to set.

        Returns:
            bool: True if the response was updated, False if the tool call was not found.
        """
        if not tool_call_id:
            return False

        index = len(self.messages)

        # go through message history to find and update the tool call
        for message in reversed(self.messages):
            index -= 1
            if (
                self.__get_message_role(message) == "tool"
                and message.get("tool_call_id") == tool_call_id
            ):
                message["content"] = str(response)
                if tool_call_id in self.pending_tool_calls:
                    self.pending_tool_calls.remove(tool_call_id)
                return True

        return False

    async def add_user_message(self, content: str):
        """Shortens the conversation history if needed and adds a user message to it.

        Args:
            content (str): The message content to add.
        """
        # call skill hooks (only for prepared/activated skills)
        for skill in self.skills:
            if skill.is_prepared:
                await skill.on_add_user_message(content)

        msg = {"role": "user", "content": content}
        await self._cleanup_conversation_history()
        self.messages.append(msg)

    async def add_assistant_message(self, content: str):
        """Adds an assistant message to the conversation history.

        Args:
            content (str): The message content to add.
        """
        # call skill hooks (only for prepared/activated skills)
        for skill in self.skills:
            if skill.is_prepared:
                await skill.on_add_assistant_message(content, [])

        msg = {"role": "assistant", "content": content}
        self.messages.append(msg)

    async def add_forced_assistant_command_calls(self, commands: list[CommandConfig]):
        """Adds forced assistant command calls to the conversation history.

        Args:
            commands (list[CommandConfig]): The commands to add.
        """

        if not commands:
            return

        message = ChatCompletionMessage(
            content="",
            role="assistant",
            tool_calls=[],
        )
        tool_id_to_command = {}
        for command in commands:
            tool_id = None
            if (
                self.config.features.conversation_provider
                == ConversationProvider.OPENAI
            ) or (
                self.config.features.conversation_provider
                == ConversationProvider.WINGMAN_PRO
                and "gpt" in self.config.wingman_pro.conversation_deployment.lower()
            ):
                tool_id = f"call_{str(uuid.uuid4()).replace('-', '')}"
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.GOOGLE
            ):
                if (
                    self.config.google.conversation_model.startswith("gemini-3")
                    or self.config.google.conversation_model == "gemini-flash-latest"
                    or self.config.google.conversation_model == "gemini-pro-latest"
                    or self.config.google.conversation_model == "gemini-flash-lite-latest"
                ):
                    # gemini 3+ (latest = 3+) needs a thought signature like this, but we cant fake it:
                    # {
                    #     'model_extra': {
                    #         'extra_content': {
                    #             'google': {
                    #                 'thought_signature': 'EjQKMgFyyNp8mNe4bQmQhOua7gGMH0C9RubFWewy6BzYZJs5f4RqDb8CaiR4gjLxoM1iQqP4'
                    #             }
                    #         }
                    #     }
                    # }
                    return
                tool_id = f"function-call-{''.join(random.choices('0123456789', k=20))}"

            # early exit for unsupported providers/models
            if not tool_id:
                return

            tool_call = ChatCompletionMessageToolCall(
                id=tool_id,
                function=ParsedFunction(
                    name="execute_command",
                    arguments=json.dumps({"command_name": command.name}),
                ),
                type="function",
            )
            message.tool_calls.append(tool_call)
            tool_id_to_command[tool_id] = command

        await self._add_gpt_response(message, message.tool_calls)
        for tool_call in message.tool_calls:
            command = tool_id_to_command[tool_call.id]
            await self._update_tool_response(tool_call.id, command.additional_context or "OK")

    async def _cleanup_conversation_history(self):
        """Cleans up the conversation history by removing messages that are too old."""
        remember_messages = self.config.features.remember_messages

        if remember_messages is None or len(self.messages) == 0:
            return 0  # Configuration not set, nothing to delete.

        # Find the cutoff index where to end deletion, making sure to only count 'user' messages towards the limit starting with newest messages.
        cutoff_index = len(self.messages)
        user_message_count = 0
        for message in reversed(self.messages):
            if self.__get_message_role(message) == "user":
                user_message_count += 1
                if user_message_count == remember_messages:
                    break  # Found the cutoff point.
            cutoff_index -= 1

        # If messages below the keep limit, don't delete anything.
        if user_message_count < remember_messages:
            return 0

        total_deleted_messages = cutoff_index  # Messages to delete.

        # Remove the pending tool calls that are no longer needed.
        for mesage in self.messages[:cutoff_index]:
            if (
                self.__get_message_role(mesage) == "tool"
                and mesage.get("tool_call_id") in self.pending_tool_calls
            ):
                self.pending_tool_calls.remove(mesage.get("tool_call_id"))
                if self.settings.debug_mode:
                    await printr.print_async(
                        f"Removing pending tool call {mesage.get('tool_call_id')} due to message history clean up.",
                        color=LogType.WARNING,
                    )

        # Remove the messages before the cutoff index, exclusive of the system message.
        del self.messages[:cutoff_index]

        # Optional debugging printout.
        if self.settings.debug_mode and total_deleted_messages > 0:
            await printr.print_async(
                f"Deleted {total_deleted_messages} messages from the conversation history.",
                color=LogType.WARNING,
            )

        return total_deleted_messages

    def reset_conversation_history(self):
        """Resets the conversation history and skill activation state.

        When the conversation is reset, the LLM loses all memory of which skills
        were activated and why. So we must also reset the skill registry and MCP
        registry to ensure the progressive disclosure state matches the LLM's memory.
        """
        self.messages = []
        self.skill_registry.reset_activations()
        self.mcp_registry.reset_activations()

    async def _try_instant_activation(self, transcript: str) -> (str, bool):
        """Tries to execute an instant activation command if present in the transcript.

        Args:
            transcript (str): The transcript to check for an instant activation command.

        Returns:
            tuple[str, bool]: A tuple containing the response to the instant command and a boolean indicating whether an instant command was executed.
        """
        commands = await self._execute_instant_activation_command(transcript)
        if commands:
            await self.add_forced_assistant_command_calls(commands)
            responses = []
            for command in commands:
                if command.responses:
                    responses.append(self._select_instant_command_response(command))

            if len(responses) == len(commands):
                # clear duplicates
                responses = list(dict.fromkeys(responses))
                responses = [
                    response + "." if not response.endswith(".") else response
                    for response in responses
                ]
                return " ".join(responses), True

            return None, True

        return None, False

    async def get_context(self):
        """Build the context and inserts it into the messages.

        With progressive disclosure, only includes prompts from ACTIVATED skills.
        Skill prompts are auto-generated from @tool descriptions if no custom prompt is set.
        """
        skill_prompts = ""
        active_skill_names = self.skill_registry.active_skill_names

        for skill in self.skills:
            # Only include prompts from activated skills (in progressive mode)
            if skill.name not in active_skill_names:
                continue

            # Get custom prompt if set
            prompt = await skill.get_prompt()

            # Auto-generate prompt from tool descriptions if no custom prompt
            if not prompt:
                tools_desc = skill.get_tools_description()
                if tools_desc:
                    prompt = f"Available tools:\n{tools_desc}"

            if prompt:
                skill_prompts += "\n\n" + skill.name + "\n\n" + prompt

        # Get TTS prompt based on active TTS provider and user preference
        tts_prompt = ""
        if self.config.features.tts_provider == TtsProvider.ELEVENLABS:
            if (
                self.config.elevenlabs.use_tts_prompt
                and self.config.elevenlabs.tts_prompt
            ):
                tts_prompt = self.config.elevenlabs.tts_prompt
        elif self.config.features.tts_provider == TtsProvider.INWORLD or (
            self.config.features.tts_provider == TtsProvider.WINGMAN_PRO
            and self.config.wingman_pro.tts_provider == WingmanProTtsProvider.INWORLD
        ):
            if self.config.inworld.use_tts_prompt and self.config.inworld.tts_prompt:
                tts_prompt = self.config.inworld.tts_prompt
        elif self.config.features.tts_provider == TtsProvider.OPENAI_COMPATIBLE:
            if (
                self.config.openai_compatible_tts.use_tts_prompt
                and self.config.openai_compatible_tts.tts_prompt
            ):
                tts_prompt = self.config.openai_compatible_tts.tts_prompt

        # Add TTS header only if there's a prompt
        if tts_prompt:
            tts_prompt = "# TEXT-TO-SPEECH\n" + tts_prompt

        # Build user context with environment metadata
        user_context = self._build_user_context()

        context = self.config.prompts.system_prompt.format(
            backstory=self.config.prompts.backstory,
            skills=skill_prompts,
            ttsprompt=tts_prompt,
            user_context=user_context,
        )

        return context

    def _build_user_context(self) -> str:
        """Build user context metadata for the system prompt.

        Includes timezone, config context, username, and wingman name.
        """
        context_parts = []
        backstory = self.config.prompts.backstory or ""
        backstory_lower = backstory.lower()

        # Date and timezone information
        try:
            now = datetime.now().astimezone()
            local_tz = now.tzinfo
            tz_name = str(local_tz)
            # Get UTC offset in a readable format
            utc_offset = now.strftime("%z")
            # Format as +HH:MM or -HH:MM
            if len(utc_offset) >= 5:
                utc_offset = f"{utc_offset[:3]}:{utc_offset[3:]}"
            # Include current date for relative date references ("last Sunday", "tomorrow", etc.)
            current_date = now.strftime(
                "%A, %B %d, %Y"
            )  # e.g., "Tuesday, December 09, 2025"
            context_parts.append(f"- Current date: {current_date}")
            context_parts.append(f"- Timezone: {tz_name} (UTC{utc_offset})")
        except Exception:
            context_parts.append("- Timezone: Unknown")

        # Config/context name (e.g., "Star Citizen", "Elite Dangerous")
        # This helps the LLM understand which game/context tools are relevant for
        if self.tower and self.tower.config_dir and self.tower.config_dir.name:
            context_parts.append(f"- Active context: {self.tower.config_dir.name}")

        # Username (only if not explicitly named in backstory)
        if self.settings.user_name:
            # Check if username is mentioned in backstory as a standalone word
            name_pattern = r"\b" + re.escape(self.settings.user_name.lower()) + r"\b"
            if not re.search(name_pattern, backstory_lower):
                context_parts.append(
                    f"- User's name (default): {self.settings.user_name}"
                )

        # Wingman name - always include as it's useful context
        # The system prompt already tells LLM to prioritize backstory names
        if self.name:
            context_parts.append(f"- Your name (default): {self.name}")

        if context_parts:
            return "\n".join(context_parts)
        return "No additional context available."

    async def add_context(self, messages):
        messages.insert(0, {"role": "system", "content": (await self.get_context())})

    async def generate_image(self, text: str) -> str:
        """
        Generates an image from the provided text configured provider.
        """

        if (
            self.config.features.image_generation_provider
            == ImageGenerationProvider.WINGMAN_PRO
        ):
            try:
                return await self.wingman_pro.generate_image(text)
            except Exception as e:
                await printr.print_async(
                    f"Error during image generation: {str(e)}", color=LogType.ERROR
                )
                printr.print(
                    traceback.format_exc(), color=LogType.ERROR, server_only=True
                )

        return ""

    async def actual_llm_call(self, messages, tools: list[dict] = None, stream: bool = False):
        """
        Perform the actual LLM call with the messages provided.
        If stream=True, returns an async iterator of response chunks.
        """
        if stream:
            return self._stream_llm_response(messages, tools)

        try:
            completion = None
            if self.config.features.conversation_provider == ConversationProvider.AZURE:
                completion = self.openai_azure.ask(
                    messages=messages,
                    api_key=self.azure_api_keys["conversation"],
                    config=self.config.azure.conversation,
                    tools=tools,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.OPENAI
            ):
                completion = self.openai.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.openai.conversation_model,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.MISTRAL
            ):
                completion = self.mistral.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.mistral.conversation_model,
                )
            elif (
                self.config.features.conversation_provider == ConversationProvider.GROQ
            ):
                completion = self.groq.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.groq.conversation_model,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.CEREBRAS
            ):
                completion = self.cerebras.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.cerebras.conversation_model,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.GOOGLE
            ):
                completion = self.google.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.google.conversation_model,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.OPENROUTER
            ):
                # OpenRouter throws an error if the model doesn't support tools but we send some
                if self.openrouter_model_supports_tools:
                    completion = self.openrouter.ask(
                        messages=messages,
                        tools=tools,
                        model=self.config.openrouter.conversation_model,
                    )
                else:
                    completion = self.openrouter.ask(
                        messages=messages,
                        model=self.config.openrouter.conversation_model,
                    )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.LOCAL_LLM
            ):
                completion = self.local_llm.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.local_llm.conversation_model,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.WINGMAN_PRO
            ):
                completion = self.wingman_pro.ask(
                    messages=messages,
                    deployment=self.config.wingman_pro.conversation_deployment,
                    tools=tools,
                )
            elif (
                self.config.features.conversation_provider
                == ConversationProvider.PERPLEXITY
            ):
                completion = self.perplexity.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.perplexity.conversation_model.value,
                )
            elif self.config.features.conversation_provider == ConversationProvider.XAI:
                completion = self.xai.ask(
                    messages=messages,
                    tools=tools,
                    model=self.config.xai.conversation_model,
                )
        except Exception as e:
            await printr.print_async(
                f"Error during LLM call: {str(e)}", color=LogType.ERROR
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
            return None

        return completion

    async def _stream_llm_response(self, messages, tools: list[dict] = None):
        """
        Stream LLM response with parallel TTS generation.
        Yields raw ChatCompletionChunk objects as they arrive from the provider.
        Internally feeds content tokens to the sentence splitter and TTS queue.
        """
        # Reset think block filter for new stream
        if hasattr(self, 'think_filter'):
            self.think_filter.reset()

        if not self.sentence_splitter or not self.tts_queue:
            # Streaming not initialized, fall back to non-streaming
            printr.print(
                f"[STREAMING] Streaming not initialized, falling back to non-streaming",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )
            completion = await self.actual_llm_call(messages, tools, stream=False)
            yield completion
            return

        printr.print(
            f"[STREAMING] Starting LLM streaming response",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )

        # Log streaming config for debugging
        stream_config = self.config.features.streaming
        printr.print(
            f"[STREAMING] Config - enabled: {stream_config.enabled}, min_sentence: {stream_config.min_sentence_length}, auto_play: {stream_config.auto_play}",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )

        # Wait for any previous TTS consumer to finish before resetting
        prev_task = self._tts_consumer_task
        if prev_task and not prev_task.done():
            printr.print(
                f"[STREAMING] Waiting for previous TTS consumer to finish before new stream...",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )
            try:
                await prev_task
            except Exception:
                pass
            # Also wait for leftover audio playback
            while self.audio_player.is_playing:
                await asyncio.sleep(0.1)
        self._tts_consumer_task = None

        # Clear any previous audio and reset state
        await self.audio_player.clear_queue()
        self.tts_queue.reset()

        # Track if consumer is already running
        consumer_started = False
        consumer_task = None

        # Markdown filter for TTS — strip syntax that should not be spoken
        from core.markdown_filter import MarkdownTTSFilter
        md_filter = MarkdownTTSFilter()

        try:
            # Get the appropriate provider for streaming
            provider = self._get_streaming_provider()
            if not provider:
                printr.print(
                    f"[STREAMING] No streaming provider found",
                    color=LogType.WARNING,
                    source_name=self.name,
                    server_only=True,
                )
                yield None
                return

            printr.print(
                f"[STREAMING] Using provider: {type(provider).__name__}",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )

            # Get and log the model
            model = self._get_streaming_model()
            printr.print(
                f"[STREAMING] Using model: {model}",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )

            # Get streaming iterator from provider
            # Different providers use different parameter names
            provider_name = type(provider).__name__
            if provider_name == "WingmanPro":
                stream_iter = provider.ask(
                    messages=messages,
                    deployment=self._get_streaming_model(),
                    tools=tools,
                    stream=True,
                )
            else:
                stream_iter = provider.ask(
                    messages=messages,
                    tools=tools,
                    model=self._get_streaming_model(),
                    stream=True,
                )

            # Guard against providers returning None on error
            if stream_iter is None:
                printr.print(
                    f"[STREAMING] Provider returned None - likely an API error",
                    color=LogType.ERROR,
                    source_name=self.name,
                    server_only=True,
                )
                yield None
                return

            # Process chunks as they arrive
            token_count = 0
            sentence_count = 0
            async for chunk in stream_iter:
                if not chunk or not chunk.choices:
                    continue

                delta = chunk.choices[0].delta if hasattr(chunk.choices[0], 'delta') else None

                # Extract content for TTS processing
                if delta and delta.content:
                    token = delta.content
                    token_count += 1

                    # Feed to sentence splitter for TTS - strip think blocks first
                    token_for_splitter = self.think_filter.filter(token)
                    sentence = self.sentence_splitter.feed(token_for_splitter)
                    if sentence:
                        # Strip markdown syntax before TTS
                        sentence = md_filter.filter(sentence)
                        if not sentence:
                            continue
                        # The markdown filter injects ". " between bullet items.
                        # Re-split on sentence boundaries so each bullet item
                        # becomes a separate TTS entry with a natural pause.
                        sub_sentences = re.split(r'(?<=\.)\s+', sentence)
                        for sub in sub_sentences:
                            sub = sub.strip()
                            if not sub:
                                continue
                            sentence_count += 1
                            await self.tts_queue.enqueue(sub)
                            if not consumer_started:
                                if self._main_loop is None:
                                    self._main_loop = asyncio.get_running_loop()
                                self.audio_player._bridge_loop = self._main_loop
                                self._ensure_tts_thread()
                                tts_future = asyncio.run_coroutine_threadsafe(
                                    self._consume_tts_queue(), self._tts_loop
                                )
                                consumer_task = asyncio.wrap_future(tts_future)
                                consumer_started = True
                        # Yield control to allow consumer to run
                        await asyncio.sleep(0)

                # Yield the raw chunk to the caller (_llm_call) for content + tool call accumulation
                yield chunk

            # Flush remaining buffer - strip think blocks and markdown
            remaining = self.think_filter.filter(self.sentence_splitter.flush())
            if remaining:
                remaining = md_filter.filter(remaining)
            if remaining:
                # Re-split on sentence boundaries (bullet items inject ". ")
                sub_sentences = re.split(r'(?<=\.)\s+', remaining)
                for sub in sub_sentences:
                    sub = sub.strip()
                    if not sub:
                        continue
                    await self.tts_queue.enqueue(sub)
                    if not consumer_started:
                        if self._main_loop is None:
                            self._main_loop = asyncio.get_running_loop()
                        self.audio_player._bridge_loop = self._main_loop
                        self._ensure_tts_thread()
                        tts_future = asyncio.run_coroutine_threadsafe(
                            self._consume_tts_queue(), self._tts_loop
                        )
                        consumer_task = asyncio.wrap_future(tts_future)
                        consumer_started = True

            # Signal that no more items will be enqueued so the consumer can exit
            self.tts_queue._closed = True

            # Store the consumer task so the caller can await audio completion
            # separately from the stream iteration. This lets the client receive
            # the completed message text before all TTS audio has finished playing.
            self._tts_consumer_task = consumer_task

            printr.print(
                f"[STREAMING] Generator returning. consumer_started={consumer_started}",
                color=LogType.INFO,
                source_name=self.name,
                server_only=True,
            )


        except Exception as e:
            await printr.print_async(
                f"Error during LLM streaming: {str(e)}", color=LogType.ERROR
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
        finally:
            # Only signal closed — do NOT clear the queue.
            # The consumer task is still running and needs to drain remaining items.
            self.tts_queue._closed = True

    def _get_streaming_provider(self):
        """Get the provider instance for streaming."""
        provider_map = {
            ConversationProvider.AZURE: self.openai_azure,
            ConversationProvider.OPENAI: self.openai,
            ConversationProvider.MISTRAL: self.mistral,
            ConversationProvider.GROQ: self.groq,
            ConversationProvider.CEREBRAS: self.cerebras,
            ConversationProvider.GOOGLE: self.google,
            ConversationProvider.OPENROUTER: self.openrouter,
            ConversationProvider.LOCAL_LLM: self.local_llm,
            ConversationProvider.WINGMAN_PRO: self.wingman_pro,
            ConversationProvider.PERPLEXITY: self.perplexity,
            ConversationProvider.XAI: self.xai,
        }
        return provider_map.get(self.config.features.conversation_provider)

    def _get_streaming_model(self):
        """Get the model name for streaming."""
        model_map = {
            ConversationProvider.OPENAI: self.config.openai.conversation_model,
            ConversationProvider.MISTRAL: self.config.mistral.conversation_model,
            ConversationProvider.GROQ: self.config.groq.conversation_model,
            ConversationProvider.CEREBRAS: self.config.cerebras.conversation_model,
            ConversationProvider.GOOGLE: self.config.google.conversation_model,
            ConversationProvider.OPENROUTER: self.config.openrouter.conversation_model,
            ConversationProvider.LOCAL_LLM: self.config.local_llm.conversation_model,
            ConversationProvider.PERPLEXITY: self.config.perplexity.conversation_model.value,
            ConversationProvider.XAI: self.config.xai.conversation_model,
            ConversationProvider.AZURE: self.config.azure.conversation.deployment_name,
            ConversationProvider.WINGMAN_PRO: self.config.wingman_pro.conversation_deployment,
        }
        return model_map.get(self.config.features.conversation_provider)

    def _ensure_tts_thread(self):
        """Start the dedicated TTS event-loop thread if it is not already running.

        The TTS thread hosts its own asyncio event loop so that TTS API calls,
        blocking HTTP setup, and the playback wait loop run completely independently
        of the main event loop.  Sentences are passed in via the thread-safe
        TTSQueue (queue.Queue) and callbacks are bridged back to the main loop.
        """
        if self._tts_thread is None or not self._tts_thread.is_alive():
            self._tts_loop = asyncio.new_event_loop()
            self._tts_thread = threading.Thread(
                target=self._tts_loop.run_forever,
                daemon=True,
                name=f"tts-{self.name}",
            )
            self._tts_thread.start()

    async def _consume_tts_queue(self):
        """Consume TTS queue and play audio in exponentially growing batches.

        Batch sizes grow: 1, 2, 4, 8, 16 (max). This gives fast initial response
        while reducing pauses between later audio blocks.

        If audio playback of one block finishes and the next batch isn't full yet,
        we flush whatever sentences are available immediately (without advancing
        to the next desired batch size) so there's no unnecessary silence.
        """
        self.tts_queue._consumer_done = False
        max_batch_size = 16
        desired_batch_size = 1
        pending_sentences: list[str] = []
        last_task: asyncio.Task | None = None

        try:
            while not self.tts_queue.is_empty() or not self.tts_queue._closed:
                # Determine how many sentences we still need for the current batch
                needed = desired_batch_size - len(pending_sentences)

                if needed > 0:
                    try:
                        text = self.tts_queue._queue.get_nowait()
                        if text and text.strip():
                            pending_sentences.append(text.strip())

                        # Batch is full — fire it as a background task and keep
                        # collecting. stream_with_effects will wait in the playback
                        # lock so the new batch pre-buffers while current audio plays.
                        if len(pending_sentences) >= desired_batch_size:
                            last_task = asyncio.create_task(
                                self._play_tts_batch(pending_sentences[:])
                            )
                            pending_sentences.clear()
                            desired_batch_size = min(desired_batch_size * 2, max_batch_size)
                        continue

                    except thread_queue.Empty:
                        # No new sentence available right now — yield briefly
                        await asyncio.sleep(0.01)

                # We timed out waiting for more sentences.
                # Decide whether to flush what we have or keep waiting.
                if pending_sentences:
                    # If the queue is closed (stream ended), flush immediately
                    if self.tts_queue._closed:
                        last_task = asyncio.create_task(
                            self._play_tts_batch(pending_sentences[:])
                        )
                        pending_sentences.clear()
                        break

                    # Normal flush: audio stopped AND no task is waiting in the
                    # playback lock (prevents spurious double-fire during the brief
                    # window between lock acquisition and is_playing = True).
                    no_task_in_flight = last_task is None or last_task.done()
                    if not self.audio_player.is_playing and no_task_in_flight:
                        last_task = asyncio.create_task(
                            self._play_tts_batch(pending_sentences[:])
                        )
                        pending_sentences.clear()
                        # Keep desired_batch_size the same — don't grow since
                        # we couldn't fill this batch (LLM is slower than TTS)
                        continue

                    # Early flush: current audio has <= tts_playback_offset seconds
                    # buffered — start next batch now so TTS pre-buffers while A plays.
                    offset = self.config.features.streaming.tts_playback_offset
                    if offset > 0:
                        remaining = self.audio_player.remaining_buffer_duration
                        if (
                            remaining is not None
                            and remaining <= offset
                            and self.audio_player.is_playing
                        ):
                            last_task = asyncio.create_task(
                                self._play_tts_batch(pending_sentences[:])
                            )
                            pending_sentences.clear()
                            continue

                # Nothing pending and queue looks done
                if self.tts_queue._closed and self.tts_queue.is_empty():
                    break

                # Avoid busy-looping when waiting for more sentences
                if not pending_sentences:
                    await asyncio.sleep(0.05)

        except Exception as e:
            await printr.print_async(
                f"Error consuming TTS queue: {str(e)}", color=LogType.ERROR
            )
        finally:
            # Flush any remaining sentences
            if pending_sentences:
                try:
                    last_task = asyncio.create_task(
                        self._play_tts_batch(pending_sentences[:])
                    )
                except Exception as e:
                    await printr.print_async(
                        f"Error playing final TTS batch: {str(e)}", color=LogType.ERROR
                    )
            # Wait for the last (and therefore all) tasks to finish — the playback
            # lock guarantees they execute sequentially.
            if last_task and not last_task.done():
                await last_task
            self.tts_queue._consumer_done = True

    async def _play_tts_batch(self, sentences: list[str]):
        """Combine sentences into a single text and play via the configured TTS provider."""
        text = " ".join(sentences)
        if not text:
            return

        printr.print(
            f"[STREAMING] Playing TTS batch ({len(sentences)} sentence(s), {len(text)} chars)",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )

        sound_config = self.config.sound
        tts_provider = self.config.features.tts_provider

        try:
            if tts_provider == TtsProvider.OPENAI:
                await self.openai.play_audio(
                    text=text,
                    voice=self.config.openai.tts_voice,
                    model=self.config.openai.tts_model,
                    speed=self.config.openai.tts_speed,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                    stream=True,
                )
            elif tts_provider == TtsProvider.ELEVENLABS:
                await self.elevenlabs.play_audio(
                    text=text,
                    config=self.config.elevenlabs,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                    stream=True,
                )
            elif tts_provider == TtsProvider.POCKET_TTS:
                await self.pocket_tts.play_audio(
                    text=text,
                    config=self.config.pocket_tts,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif tts_provider == TtsProvider.AZURE:
                await self.openai_azure.play_audio(
                    text=text,
                    api_key=self.azure_api_keys.get("tts", ""),
                    config=self.config.azure.tts,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif tts_provider == TtsProvider.EDGE_TTS:
                await self.edge_tts.play_audio(
                    text=text,
                    config=self.config.edge_tts,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif tts_provider == TtsProvider.INWORLD:
                await self.inworld.play_audio(
                    text=text,
                    config=self.config.inworld,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif tts_provider == TtsProvider.HUME:
                await self.hume.play_audio(
                    text=text,
                    config=self.config.hume,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif tts_provider == TtsProvider.XVASYNTH:
                await self.xvasynth.play_audio(
                    text=text,
                    config=self.config.xvasynth,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif tts_provider == TtsProvider.OPENAI_COMPATIBLE:
                await self.openai_compatible_tts.play_audio(
                    text=text,
                    voice=self.config.openai_compatible_tts.voice,
                    model=self.config.openai_compatible_tts.model,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                    stream=True,
                )
            elif tts_provider == TtsProvider.WINGMAN_PRO:
                if self.config.wingman_pro.tts_provider.name == "OPENAI":
                    await self.wingman_pro.generate_openai_speech(
                        text=text,
                        voice=self.config.openai.tts_voice,
                        model=self.config.openai.tts_model,
                        speed=self.config.openai.tts_speed,
                        sound_config=sound_config,
                        audio_player=self.audio_player,
                        wingman_name=self.name,
                    )
        except Exception as e:
            await printr.print_async(
                f"Error playing TTS for batch: {str(e)}", color=LogType.ERROR
            )

    async def _llm_call(self, allow_tool_calls: bool = True):
        """Makes the primary LLM call with the conversation history and tools enabled.

        Returns:
            The LLM completion object or None if the call fails.
        """

        # save request time for later comparison
        thiscall = time.time()
        self.last_gpt_call = thiscall

        # build tools
        tools = self.build_tools() if allow_tool_calls else None

        # Check if streaming is enabled
        streaming_enabled = (
            hasattr(self.config.features, 'streaming') and
            self.config.features.streaming.enabled
        )

        if self.settings.debug_mode:
            await printr.print_async(
                f"Calling LLM with {(len(self.messages))} messages (excluding context) and {len(tools) if tools else 0} tools. Streaming: {streaming_enabled}",
                color=LogType.INFO,
            )

        messages = self.messages.copy()
        await self.add_context(messages)

        # DEBUG: Print compiled context (dev-only, remove before release)
        # if messages and messages[0].get("role") == "system":
        #     print("\n" + "=" * 80)
        #     print("COMPILED CONTEXT:")
        #     print("=" * 80)
        #     print(messages[0].get("content", ""))
        #     print("=" * 80 + "\n")

        if streaming_enabled:
            # Use streaming - iterate over the async iterator and collect all content
            stream_iter = await self.actual_llm_call(messages, tools, stream=True)

            full_content = ""
            raw_content = ""
            # Accumulate tool calls across chunks (index-based, as per OpenAI streaming spec)
            tool_calls_accum: dict[int, dict] = {}
            self._streaming_played_audio = False

            # Separate think-block filter for HUD tokens
            # (the TTS filter in _stream_llm_response uses self.think_filter independently)
            hud_think_filter = ThinkBlockFilter()
            was_thinking = False

            async for chunk in stream_iter:
                if chunk:
                    # Handle both ChatCompletionChunk objects and plain strings
                    if hasattr(chunk, 'choices') and chunk.choices:
                        choice = chunk.choices[0]
                        delta = choice.delta if hasattr(choice, 'delta') else None

                        # Accumulate tool call deltas (streamed across multiple chunks)
                        if delta and delta.tool_calls:
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index
                                if idx not in tool_calls_accum:
                                    tool_calls_accum[idx] = {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                acc = tool_calls_accum[idx]
                                if tc_delta.id:
                                    acc["id"] = tc_delta.id
                                if tc_delta.type:
                                    acc["type"] = tc_delta.type
                                if tc_delta.function:
                                    if tc_delta.function.name:
                                        # Function names are never fragmented across
                                        # chunks — assign instead of append. Some
                                        # providers (e.g. Google Gemini via OpenAI
                                        # compat) re-send the full name in every
                                        # chunk which caused duplication with +=.
                                        acc["function"]["name"] = tc_delta.function.name
                                    if tc_delta.function.arguments:
                                        new_args = tc_delta.function.arguments
                                        existing = acc["function"]["arguments"]
                                        # Some providers send the complete arguments
                                        # JSON in each chunk instead of incremental
                                        # fragments. Detect this: if existing args
                                        # look like complete JSON and new args start
                                        # a new JSON object, overwrite instead of
                                        # appending (which would produce invalid
                                        # "Extra data" JSON).
                                        if (
                                            existing
                                            and existing.rstrip().endswith("}")
                                            and new_args.lstrip().startswith("{")
                                        ):
                                            acc["function"]["arguments"] = new_args
                                        else:
                                            acc["function"]["arguments"] += new_args

                                # Capture Google Gemini extra_content (contains
                                # thought_signature) from model_extra on the
                                # tool-call delta.  Required for Gemini 3+
                                # tool-call round-trips.
                                for extra_src in (tc_delta, getattr(tc_delta, "function", None)):
                                    if extra_src and hasattr(extra_src, "model_extra") and extra_src.model_extra:
                                        extra_content = extra_src.model_extra.get("extra_content")
                                        if extra_content and isinstance(extra_content, dict):
                                            google_extra = extra_content.get("google", {})
                                            if google_extra.get("thought_signature"):
                                                acc["extra_content"] = extra_content
                                                printr.print(
                                                    f"[STREAMING] Captured thought_signature from tool-call delta",
                                                    color=LogType.INFO,
                                                    source_name=self.name,
                                                    server_only=True,
                                                )

                        # Also capture extra_content from the choice-level
                        # model_extra (some SDK versions surface it there).
                        if hasattr(choice, "model_extra") and choice.model_extra:
                            extra_content = choice.model_extra.get("extra_content")
                            if extra_content and isinstance(extra_content, dict):
                                google_extra = extra_content.get("google", {})
                                if google_extra.get("thought_signature"):
                                    for idx in sorted(tool_calls_accum.keys(), reverse=True):
                                        tool_calls_accum[idx].setdefault("extra_content", extra_content)
                                        break

                        # Extract content from delta
                        chunk_content = delta.content if delta and delta.content else ""
                    else:
                        # It's already a string (fallback from _stream_llm_response non-streaming path)
                        chunk_content = str(chunk) if chunk else ""

                    if chunk_content:
                        raw_content += chunk_content
                        # Filter think blocks before sending to HUD/skill hooks
                        filtered_for_hud = hud_think_filter.filter(chunk_content)

                        # Detect think-block state transitions and notify skills
                        is_thinking = hud_think_filter.in_think_block
                        if is_thinking != was_thinking:
                            was_thinking = is_thinking
                            for skill in self.skills:
                                if skill.is_prepared:
                                    try:
                                        await skill.on_thinking_state_changed(is_thinking)
                                    except Exception:
                                        pass

                        if filtered_for_hud:
                            # Call skill hooks for each filtered token (for HUD streaming display)
                            for skill in self.skills:
                                if skill.is_prepared:
                                    if hasattr(skill, 'on_llm_token'):
                                        try:
                                            await skill.on_llm_token(filtered_for_hud)
                                        except Exception as e:
                                            printr.print(
                                                f"Error calling on_llm_token for skill {skill.name}: {e}",
                                                color=LogType.WARNING,
                                                source_name=self.name,
                                            )

            # If the stream ended while still in a think block, notify skills
            if was_thinking:
                for skill in self.skills:
                    if skill.is_prepared:
                        try:
                            await skill.on_thinking_state_changed(False)
                        except Exception:
                            pass

            # Strip think blocks from accumulated raw content
            from core.thinking_filter import strip_think_blocks
            full_content = strip_think_blocks(raw_content) if raw_content else ""

            # Debug logging
            if raw_content:
                printr.print(
                    f"[STREAMING] Raw content ({len(raw_content)} chars): {raw_content[:200]}{'...' if len(raw_content) > 200 else ''}",
                    color=LogType.INFO,
                    source_name=self.name,
                    server_only=True,
                )
                if full_content != raw_content:
                    printr.print(
                        f"[STREAMING] Filtered content ({len(full_content)} chars): {full_content[:200]}{'...' if len(full_content) > 200 else ''}",
                        color=LogType.INFO,
                        source_name=self.name,
                        server_only=True,
                    )

            # Build tool_calls list from accumulated deltas
            built_tool_calls = None
            if tool_calls_accum:
                from openai.types.chat.chat_completion_message_tool_call import Function
                built_tool_calls = []
                for idx in sorted(tool_calls_accum.keys()):
                    tc = tool_calls_accum[idx]
                    # Include extra_content if present (contains
                    # thought_signature required for Gemini 3+ tool-call
                    # round-trips via OpenAI compat API).
                    extra_kwargs = {}
                    if "extra_content" in tc:
                        extra_kwargs["extra_content"] = tc["extra_content"]
                    built_tool_calls.append(
                        ChatCompletionMessageToolCall(
                            id=tc["id"],
                            type=tc["type"],
                            function=Function(
                                name=tc["function"]["name"],
                                arguments=tc["function"]["arguments"],
                            ),
                            **extra_kwargs,
                        )
                    )
                printr.print(
                    f"[STREAMING] Accumulated {len(built_tool_calls)} tool call(s): "
                    + ", ".join(tc.function.name for tc in built_tool_calls),
                    color=LogType.INFO,
                    source_name=self.name,
                    server_only=True,
                )

            # Create a proper ChatCompletion object from streamed content and/or tool calls
            if full_content or built_tool_calls:
                from openai.types.chat.chat_completion import Choice
                from typing import cast, Literal
                finish_reason = cast(
                    Literal["stop", "length", "tool_calls", "content_filter", "function_call"],
                    "tool_calls" if built_tool_calls else "stop",
                )
                message = ChatCompletionMessage(
                    content=full_content or None,
                    role="assistant",
                    tool_calls=built_tool_calls,
                )
                completion = ChatCompletion(
                    id="stream-" + str(int(time.time())),
                    choices=[
                        Choice(
                            finish_reason=finish_reason,
                            index=0,
                            message=message,
                        )
                    ],
                    created=int(time.time()),
                    model=self._get_streaming_model() or "gpt-4o",
                    object="chat.completion",
                )
                # Mark that streaming played audio (TTS was handled during stream)
                # so the caller can skip duplicate play_to_user
                # Also check for whitespace-only content to avoid showing empty messages
                if full_content and full_content.strip():
                    self._streaming_played_audio = True
                printr.print(
                    f"[STREAMING] Built completion: has_content={bool(full_content)}, has_tools={bool(built_tool_calls)}, _streaming_played_audio={self._streaming_played_audio}",
                    color=LogType.INFO,
                    source_name=self.name,
                    server_only=True,
                )
            else:
                completion = None
                printr.print(
                    f"[STREAMING] No content and no tool calls - completion is None",
                    color=LogType.WARNING,
                    source_name=self.name,
                    server_only=True,
                )
        else:
            completion = await self.actual_llm_call(messages, tools)

        # if request isnt most recent, ignore the response
        if self.last_gpt_call != thiscall:
            await printr.print_async(
                "LLM call was cancelled due to a new call.", color=LogType.WARNING
            )
            return None

        return completion

    async def _process_completion(self, completion: ChatCompletion, allow_tool_calls: bool = True):
        """Processes the completion returned by the LLM call.

        Args:
            completion: The completion object from an OpenAI call.

        Returns:
            A tuple containing the message response and tool calls from the completion.
        """

        response_message = completion.choices[0].message

        raw_content = response_message.content
        if raw_content is None:
            raw_content = ""

        # Debug logging for think block filtering (non-streaming)
        printr.print(
            f"[NON-STREAMING] Raw content: {raw_content[:200]}{'...' if len(raw_content) > 200 else ''}",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )

        # Filter think blocks from content
        from core.thinking_filter import strip_think_blocks
        filtered_content = strip_think_blocks(raw_content)

        printr.print(
            f"[NON-STREAMING] Filtered content: {filtered_content[:200]}{'...' if len(filtered_content) > 200 else ''}",
            color=LogType.INFO,
            source_name=self.name,
            server_only=True,
        )

        response_message.content = filtered_content if filtered_content else ""

        # remove hallucinated tools, if none were allowed
        if not allow_tool_calls:
            response_message.tool_calls = None

        # temporary fix for tool calls that have a command name as function name
        if response_message.tool_calls:
            response_message.tool_calls = await self._fix_tool_calls(
                response_message.tool_calls
            )

        return response_message, response_message.tool_calls

    async def _handle_tool_calls(self, tool_calls):
        """Processes all the tool calls identified in the response message.

        Args:
            tool_calls: The list of tool calls to process.

        Returns:
            tuple: (instant_response, skill, tool_timings) where tool_timings is a list of (label, time_ms) tuples.
        """
        instant_response = None
        function_response = ""
        tool_timings: list[tuple[str, float]] = []

        skill = None

        for tool_call in tool_calls:
            try:
                function_name = tool_call.function.name
                function_args = (
                    tool_call.function.arguments
                    # Mistral returns a dict
                    if isinstance(tool_call.function.arguments, dict)
                    # OpenAI returns a string
                    else json.loads(tool_call.function.arguments)
                )

                # Time the individual tool execution
                tool_start = time.perf_counter()
                (
                    function_response,
                    instant_response,
                    skill,
                    tool_label,
                ) = await self.execute_command_by_function_call(
                    function_name, function_args
                )
                tool_time_ms = (time.perf_counter() - tool_start) * 1000

                # Add timing if we got a label (actual tool execution, not meta-tool)
                if tool_label:
                    tool_timings.append((tool_label, tool_time_ms))

                if tool_call.id:
                    # updating the dummy tool response with the actual response
                    await self._update_tool_response(tool_call.id, function_response)
                else:
                    # adding a new tool response
                    self._add_tool_response(tool_call, function_response)
            except Exception as e:
                self._add_tool_response(tool_call, "Error")
                await printr.print_async(
                    f"Error while processing tool call: {str(e)}", color=LogType.ERROR
                )
                printr.print(
                    traceback.format_exc(), color=LogType.ERROR, server_only=True
                )
        return instant_response, skill, tool_timings

    async def execute_command_by_function_call(
        self, function_name: str, function_args: dict[str, any]
    ) -> tuple[str, str | None, Skill | None, str | None]:
        """
        Uses an OpenAI function call to execute a command. If it's an instant activation_command, one if its responses will be played.

        Args:
            function_name (str): The name of the function to be executed.
            function_args (dict[str, any]): The arguments to pass to the function being executed.

        Returns:
            A tuple containing:
            - function_response (str): The text response or result obtained after executing the function.
            - instant_response (str): An immediate response or action to be taken, if any (e.g., play audio).
            - used_skill (Skill): The skill that was used, if any.
            - tool_label (str): Label for benchmark timing (e.g., "MCP: resolve-library-id"), or None for meta-tools.
        """
        function_response = ""
        instant_response = ""
        used_skill = None
        tool_label = None

        # Handle unified capability meta-tools (activate_capability, list_active_capabilities)
        if self.capability_registry.is_meta_tool(function_name):
            function_response, tools_changed = (
                await self.capability_registry.execute_meta_tool(
                    function_name, function_args
                )
            )

            # If a skill was activated, perform lazy validation
            if tools_changed and function_name == "activate_capability":
                capability_name = function_args.get("capability_name", "")
                skill = self.skill_registry.get_skill_for_activation(capability_name)
                if skill and skill.needs_activation():
                    success, validation_msg = await skill.ensure_activated()
                    if not success:
                        # Validation failed - deactivate the skill
                        self.skill_registry.deactivate_skill(capability_name)
                        function_response = validation_msg
                        tools_changed = False
                        await printr.print_async(
                            f"Skill activation failed: {capability_name}",
                            color=LogType.ERROR,
                        )
                    else:
                        # Get display name for user-friendly message
                        display_name = self.skill_registry.get_skill_display_name(
                            capability_name
                        )
                        await printr.print_async(
                            f"Skill activated: {display_name}",
                            color=LogType.SKILL,
                        )

            return function_response, None, None, None  # Meta-tool, no timing label

        # Handle legacy meta-tools for progressive skill discovery/activation
        # These are kept for backward compatibility but shouldn't be called
        if self.skill_registry.is_meta_tool(function_name):
            function_response, tools_changed = (
                await self.skill_registry.execute_meta_tool(
                    function_name, function_args
                )
            )

            # If skill was activated, perform lazy validation
            if tools_changed and function_name == "activate_skill":
                skill_name = function_args.get("skill_name", "")
                skill = self.skill_registry.get_skill_for_activation(skill_name)
                if skill and skill.needs_activation():
                    success, validation_msg = await skill.ensure_activated()
                    if not success:
                        # Validation failed - deactivate the skill
                        self.skill_registry.deactivate_skill(skill_name)
                        function_response = validation_msg
                        tools_changed = False
                        await printr.print_async(
                            f"Skill activation failed: {skill_name}",
                            color=LogType.ERROR,
                        )
                    else:
                        # Get display name for user-friendly message
                        display_name = self.skill_registry.get_skill_display_name(
                            skill_name
                        )
                        await printr.print_async(
                            f"Skill activated: {display_name}",
                            color=LogType.SKILL,
                        )

            return function_response, None, None, None  # Meta-tool, no timing label

        # Handle MCP meta-tools for server discovery/activation
        if self.mcp_registry.is_meta_tool(function_name):
            function_response, tools_changed = (
                await self.mcp_registry.execute_meta_tool(function_name, function_args)
            )
            return function_response, None, None, None  # Meta-tool, no timing label

        # Handle MCP server tools (prefixed with mcp_)
        if self.mcp_registry.is_mcp_tool(function_name):
            connection = self.mcp_registry.get_connection_for_tool(function_name)
            if connection:
                display_name = connection.config.display_name
                original_name = self.mcp_registry.get_original_tool_name(function_name)
                tool_label = f"🌐 {display_name}: {original_name}"

                benchmark = Benchmark(
                    f"MCP '{connection.config.name}' - {original_name}"
                )

                # Always show simple 'called' message in UI so users know the wingman is working
                await printr.print_async(
                    f"{display_name}: called `{original_name}` with {function_args}",
                    color=LogType.MCP,
                )

                # Detailed 'calling' log only in terminal/log file
                await printr.print_async(
                    f"{display_name}: calling `{original_name}` with {function_args}...",
                    color=LogType.MCP,
                    server_only=True,
                )

                try:
                    function_response = await self.mcp_registry.call_tool(
                        function_name, function_args
                    )
                except Exception as e:
                    await printr.print_async(
                        f"{display_name}: `{original_name}` failed - {str(e)}",
                        color=LogType.ERROR,
                    )
                    printr.print(
                        traceback.format_exc(), color=LogType.ERROR, server_only=True
                    )
                    function_response = "ERROR DURING MCP TOOL EXECUTION"
                finally:
                    # Detailed 'completed' with timing only in terminal/log file (or UI if debug)
                    await printr.print_async(
                        f"{display_name}: `{original_name}` completed",
                        color=LogType.MCP,
                        benchmark_result=benchmark.finish(),
                        server_only=not self.settings.debug_mode,
                    )

                return function_response, None, None, tool_label

        # Handle command calls
        if function_name == "execute_command":
            # get the command based on the argument passed by the LLM
            command = self.get_command(function_args["command_name"])
            # execute the command
            instant_response, function_response = await self._execute_command(command)
            tool_label = f"Command: {function_args.get('command_name', function_name)}"
            # if the command has responses, we have to play one of them
            if instant_response:
                await self.play_to_user(instant_response)

        # Go through the skills and check if the function name matches any of the tools
        if function_name in self.tool_skills:
            skill = self.tool_skills[function_name]
            display_name = self.skill_registry.get_skill_display_name(skill.name)
            tool_label = f"⚡ {display_name}: {function_name}"

            benchmark = Benchmark(f"Skill '{skill.name}' - {function_name}")

            # Always show simple 'called' message in UI so users know the wingman is working
            await printr.print_async(
                f"{display_name}: called `{function_name}` with {function_args}",
                color=LogType.SKILL,
                skill_name=skill.name,
            )

            # Detailed 'calling' log only in terminal/log file
            await printr.print_async(
                f"{display_name}: calling `{function_name}` with {function_args}...",
                color=LogType.SKILL,
                skill_name=skill.name,
                server_only=True,
            )

            try:
                function_response, instant_response = await skill.execute_tool(
                    tool_name=function_name,
                    parameters=function_args,
                    benchmark=benchmark,
                )
                used_skill = skill
                if instant_response:
                    await self.play_to_user(instant_response)
            except Exception as e:
                await printr.print_async(
                    f"{display_name}: `{function_name}` failed - {str(e)}",
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
                    f"{display_name}: `{function_name}` completed",
                    color=LogType.SKILL,
                    benchmark_result=benchmark.finish(),
                    skill_name=skill.name,
                    server_only=not self.settings.debug_mode,
                )

        return function_response, instant_response, used_skill, tool_label

    async def play_to_user(
        self,
        text: str,
        no_interrupt: bool = False,
        sound_config: Optional[SoundConfig] = None,
    ):
        """Plays audio to the user using the configured TTS Provider (default: OpenAI TTS).
        Also adds sound effects if enabled in the configuration.

        Args:
            text (str): The text to play as audio.
        """
        if sound_config:
            printr.print(
                "Using custom sound config for playback", LogType.INFO, server_only=True
            )
        else:
            sound_config = self.config.sound

        # remove Markdown, links, emotes and code blocks
        text, contains_links, contains_code_blocks = cleanup_text(text)

        # wait for audio player to finish playing
        if no_interrupt and self.audio_player.is_playing:
            while self.audio_player.is_playing:
                await asyncio.sleep(0.1)

        # call skill hooks (only for prepared/activated skills)
        changed_text = text
        for skill in self.skills:
            if skill.is_prepared:
                changed_text = await skill.on_play_to_user(text, sound_config)
                if changed_text != text:
                    printr.print(
                        f"Skill '{skill.config.display_name}' modified the text to: '{changed_text}'",
                        LogType.INFO,
                    )
                    text = changed_text

        if sound_config.volume == 0.0:
            printr.print(
                "Volume modifier is set to 0. Skipping TTS processing.",
                LogType.WARNING,
                server_only=True,
            )
            return

        if "{SKIP-TTS}" in text:
            printr.print(
                "Skip TTS phrase found in input. Skipping TTS processing.",
                LogType.WARNING,
                server_only=True,
            )
            return

        try:
            if self.config.features.tts_provider == TtsProvider.EDGE_TTS:
                await self.edge_tts.play_audio(
                    text=text,
                    config=self.config.edge_tts,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif self.config.features.tts_provider == TtsProvider.ELEVENLABS:
                await self.elevenlabs.play_audio(
                    text=text,
                    config=self.config.elevenlabs,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                    stream=self.config.elevenlabs.output_streaming,
                )
            elif self.config.features.tts_provider == TtsProvider.HUME:
                try:
                    await self.hume.play_audio(
                        text=text,
                        config=self.config.hume,
                        sound_config=sound_config,
                        audio_player=self.audio_player,
                        wingman_name=self.name,
                    )
                except RuntimeError as e:
                    if "Event loop is closed" in str(e):
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        await self.hume.play_audio(
                            text=text,
                            config=self.config.hume,
                            sound_config=sound_config,
                            audio_player=self.audio_player,
                            wingman_name=self.name,
                        )
            elif self.config.features.tts_provider == TtsProvider.INWORLD:
                await self.inworld.play_audio(
                    text=text,
                    config=self.config.inworld,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif self.config.features.tts_provider == TtsProvider.AZURE:
                await self.openai_azure.play_audio(
                    text=text,
                    api_key=self.azure_api_keys["tts"],
                    config=self.config.azure.tts,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif self.config.features.tts_provider == TtsProvider.XVASYNTH:
                await self.xvasynth.play_audio(
                    text=text,
                    config=self.config.xvasynth,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif self.config.features.tts_provider == TtsProvider.OPENAI:
                await self.openai.play_audio(
                    text=text,
                    voice=self.config.openai.tts_voice,
                    model=self.config.openai.tts_model,
                    speed=self.config.openai.tts_speed,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                    stream=self.config.openai.output_streaming,
                )
            elif self.config.features.tts_provider == TtsProvider.OPENAI_COMPATIBLE:
                await self.openai_compatible_tts.play_audio(
                    text=text,
                    voice=self.config.openai_compatible_tts.voice,
                    model=self.config.openai_compatible_tts.model,
                    speed=(
                        self.config.openai_compatible_tts.speed
                        if self.config.openai_compatible_tts.speed  #!= 1.0
                        else NOT_GIVEN
                    ),
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                    stream=self.config.openai_compatible_tts.output_streaming,
                )
            elif self.config.features.tts_provider == TtsProvider.POCKET_TTS:
                await self.pocket_tts.play_audio(
                    text=text,
                    config=self.config.pocket_tts,
                    sound_config=sound_config,
                    audio_player=self.audio_player,
                    wingman_name=self.name,
                )
            elif self.config.features.tts_provider == TtsProvider.WINGMAN_PRO:
                if self.config.wingman_pro.tts_provider == WingmanProTtsProvider.OPENAI:
                    await self.wingman_pro.generate_openai_speech(
                        text=text,
                        voice=self.config.openai.tts_voice,
                        model=self.config.openai.tts_model,
                        speed=self.config.openai.tts_speed,
                        sound_config=sound_config,
                        audio_player=self.audio_player,
                        wingman_name=self.name,
                    )
                elif (
                    self.config.wingman_pro.tts_provider == WingmanProTtsProvider.AZURE
                ):
                    await self.wingman_pro.generate_azure_speech(
                        text=text,
                        config=self.config.azure.tts,
                        sound_config=sound_config,
                        audio_player=self.audio_player,
                        wingman_name=self.name,
                    )
                elif (
                    self.config.wingman_pro.tts_provider
                    == WingmanProTtsProvider.INWORLD
                ):
                    await self.wingman_pro.generate_inworld_speech(
                        text=text,
                        config=self.config.inworld,
                        sound_config=sound_config,
                        audio_player=self.audio_player,
                        wingman_name=self.name,
                    )
            else:
                printr.toast_error(
                    f"Unsupported TTS provider: {self.config.features.tts_provider}"
                )
        except Exception as e:
            await printr.print_async(
                f"Error during TTS playback: {str(e)}", color=LogType.ERROR
            )
            printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)

    async def _execute_command(self, command: CommandConfig, is_instant=False) -> tuple[str | None, str]:
        """Executes a command by delegating to the Wingman base implementation.

        Returns:
            tuple[str | None, str]: A 2-tuple of:
                - Instant response (str) to play immediately, or None if there is no instant response.
                - Function/tool response (str) to feed back to the LLM.
        """
        return await super()._execute_command(command, is_instant)

    def build_tools(self) -> list[dict]:
        """
        Builds tools for the LLM call.

        In progressive mode: Returns meta-tools (search_skills, activate_skill) plus
        tools from activated skills only.

        In legacy mode: Returns all skill tools.

        Returns:
            list[dict]: A list of tool descriptors in OpenAI format.
        """

        def _command_has_effective_actions(command: CommandConfig) -> bool:
            if command.is_system_command:
                return True

            if not command.actions:
                return False

            for action in command.actions:
                if not action:
                    continue
                if (
                    action.keyboard is not None
                    or action.mouse is not None
                    or action.joystick is not None
                    or action.audio is not None
                    or action.write is not None
                    or action.wait is not None
                ):
                    return True

            return False

        commands = [
            command.name
            for command in self.config.commands
            if (not command.force_instant_activation)
            and _command_has_effective_actions(command)
        ]
        tools: list[dict] = []
        if commands:
            tools.append(
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
                }
            )

        # Unified capability discovery: single activate_capability meta-tool
        # Combines skills and MCP servers - LLM doesn't need to know the difference
        for _, tool in self.capability_registry.get_meta_tools():
            tools.append(tool)

        # Add tools from activated capabilities (both skills and MCPs)
        for _, tool in self.skill_registry.get_active_tools():
            tools.append(tool)

        for _, tool in self.mcp_registry.get_active_tools():
            tools.append(tool)

        return tools

    def __get_message_role(self, message):
        """Helper method to get the role of the message regardless of its type."""
        if isinstance(message, Mapping):
            return message.get("role")
        elif hasattr(message, "role"):
            return message.role
        else:
            raise TypeError(
                f"Message is neither a mapping nor has a 'role' attribute: {message}"
            )
