import json
import traceback
from typing import TYPE_CHECKING
from api.interface import SettingsConfig, SkillConfig
from api.enums import LogSource, LogType
from skills.skill_base import Skill, tool

if TYPE_CHECKING:
    from wingmen.open_ai_wingman import OpenAiWingman


AGENT_SYSTEM_PROMPT = """You are a focused sub-agent assistant. You have been spawned to complete a specific task.

Your task:
{task}

Instructions:
- Use the tools available to you to complete the task.
- Be thorough and complete the task fully before responding.
- Your final response should be a clear, concise summary of the results.
- Do NOT ask follow-up questions. Complete the task with the information provided.
- If you cannot complete the task, explain what went wrong.
"""


class SubAgent(Skill):

    def __init__(
        self,
        config: SkillConfig,
        settings: SettingsConfig,
        wingman: "OpenAiWingman",
    ) -> None:
        super().__init__(config=config, settings=settings, wingman=wingman)

    def _get_max_iterations(self) -> int:
        """Retrieve max iterations from config at runtime."""
        errors = []
        value = self.retrieve_custom_property_value("max_iterations", errors)
        if value is not None:
            return int(value)
        return 15

    @tool(
        description="Spawn a sub-agent to handle a complex, multi-step task. The agent has access to all your tools and capabilities but runs in its own isolated conversation. Returns the final result as text. Use this for tasks requiring multiple tool calls, research, or multi-step operations to keep your main conversation clean and save tokens.",
        summarize=True,
        wait_response=True,
    )
    async def create_agent(self, task: str) -> str:
        """Spawn a sub-agent to handle a task using the parent wingman's tools.

        Args:
            task: A detailed description of what the sub-agent should accomplish. Be specific about what information to gather or what actions to take.
        """
        await self.printr.print_async(
            f"Sub-Agent: spawning for task: {task[:100]}...",
            color=LogType.SKILL,
            source=LogSource.WINGMAN,
            source_name=self.wingman.name,
            skill_name=self.name,
        )

        max_iterations = self._get_max_iterations()

        # Build tools from parent wingman (inherits all skills + MCPs + commands)
        # Exclude the create_agent tool itself to prevent recursive agent spawning
        all_tools = self.wingman.build_tools()
        tools = [
            t
            for t in all_tools
            if t.get("function", {}).get("name") != "create_agent"
        ]

        # Build the agent's message history with its own system prompt
        messages = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT.format(task=task)},
            {"role": "user", "content": task},
        ]

        iteration = 0

        try:
            while iteration < max_iterations:
                iteration += 1

                await self.printr.print_async(
                    f"Sub-Agent: iteration {iteration}/{max_iterations}",
                    color=LogType.INFO,
                    server_only=True,
                )

                # Make LLM call with the agent's own message history
                completion = await self.wingman.actual_llm_call(
                    messages=messages,
                    tools=tools if tools else None,
                )

                if completion is None:
                    return "Sub-agent error: LLM call failed."

                response_message = completion.choices[0].message
                if response_message.content is None:
                    response_message.content = ""

                tool_calls = response_message.tool_calls

                # Add assistant response to agent's message history
                messages.append(response_message)

                # If no tool calls, we have our final answer
                if not tool_calls:
                    result = response_message.content or "Sub-agent completed but returned no content."
                    await self.printr.print_async(
                        f"Sub-Agent: completed after {iteration} iteration(s)",
                        color=LogType.SKILL,
                        source=LogSource.WINGMAN,
                        source_name=self.wingman.name,
                        skill_name=self.name,
                    )
                    return result

                # Process tool calls
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    try:
                        function_args = (
                            tool_call.function.arguments
                            if isinstance(tool_call.function.arguments, dict)
                            else json.loads(tool_call.function.arguments)
                        )
                    except (json.JSONDecodeError, TypeError):
                        function_args = {}

                    # Execute the tool via the parent wingman
                    try:
                        function_response, instant_response, used_skill, tool_label = (
                            await self.wingman.execute_command_by_function_call(
                                function_name, function_args
                            )
                        )
                    except Exception as e:
                        function_response = f"Error executing tool '{function_name}': {str(e)}"
                        await self.printr.print_async(
                            f"Sub-Agent: tool error - {function_response}",
                            color=LogType.ERROR,
                            server_only=True,
                        )

                    # Add tool response to agent's message history
                    tool_response_msg = {
                        "role": "tool",
                        "content": str(function_response) if function_response else "",
                    }
                    if tool_call.id is not None:
                        tool_response_msg["tool_call_id"] = tool_call.id
                    if function_name is not None:
                        tool_response_msg["name"] = function_name
                    messages.append(tool_response_msg)

            # Max iterations reached
            await self.printr.print_async(
                f"Sub-Agent: max iterations ({max_iterations}) reached",
                color=LogType.WARNING,
                source=LogSource.WINGMAN,
                source_name=self.wingman.name,
                skill_name=self.name,
            )

            # Make one final call without tools to get a summary
            completion = await self.wingman.actual_llm_call(
                messages=messages,
                tools=None,
            )
            if completion and completion.choices:
                return completion.choices[0].message.content or "Sub-agent reached max iterations without a final answer."
            return "Sub-agent reached max iterations without a final answer."

        except Exception as e:
            error_msg = f"Sub-agent error: {str(e)}"
            await self.printr.print_async(
                error_msg,
                color=LogType.ERROR,
                server_only=True,
            )
            self.printr.print(
                traceback.format_exc(), color=LogType.ERROR, server_only=True
            )
            return error_msg
