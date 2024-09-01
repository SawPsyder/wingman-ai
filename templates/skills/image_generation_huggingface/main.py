from os import path
import datetime
import base64
import requests
from api.enums import LogSource, LogType
from api.interface import WingmanInitializationError
from skills.skill_base import Skill
from services.file import get_writable_dir
from services.system_manager import LOCAL_VERSION

class ImageGenerationHF(Skill):

    def __init__(
        self,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.api_key = ""
        self.api_url = ""
        self.api_timeout = 120
        self.api_retries = 1
        self.image_path = get_writable_dir(
            path.join("skills", "image_generation_huggingface", "generated_images")
        )

    async def validate(self) -> list[WingmanInitializationError]:
        errors = await super().validate()
        self.api_url = self.retrieve_custom_property_value(
            "huggingface_model_url", errors
        )
        self.api_key = await self.retrieve_secret(
            "huggingface_inference",
            errors,
        )

        return errors

    async def execute_tool(
        self, tool_name: str, parameters: dict[str, any]
    ) -> tuple[str, str]:
        instant_response = ""
        function_response = "I can't generate an image, sorry."

        if tool_name == "generate_image_huggingface":
            prompt = parameters["prompt"]
            if self.settings.debug_mode:
                await self.printr.print_async(
                    f"Generate huggingface image with prompt: {prompt}.", color=LogType.INFO
                )

            image_path = await self.generate_image_with_huggingface(prompt)

            if LOCAL_VERSION != "1.5.0": # TODO remove this when the new version is released
                await self.printr.print_async(
                    "",
                    color=LogType.INFO,
                    source=LogSource.WINGMAN,
                    source_name=self.wingman.name,
                    skill_name=self.name,
                    additional_data={"image_url": image_path},
                )
            else:
                image_bytes = open(image_path, "rb").read()
                image_base64 = base64.b64encode(image_bytes).decode("utf-8")
                await self.printr.print_async(
                    f"![{prompt}](data:image/jpeg;base64,{image_base64})",
                    color=LogType.INFO,
                    source=LogSource.WINGMAN,
                    source_name=self.wingman.name,
                    skill_name=self.name,
                )
            function_response = "Image generated (Already displayed to user)."
        return function_response, instant_response

    async def generate_image_with_huggingface(self, prompt: str) -> str|None:
        headers = {"Authorization": f"Bearer {self.api_key}"}

        image_bytes = ""
        for _ in range(self.api_retries):
            response = requests.post(
                self.api_url,
                headers=headers,
                json={"inputs": prompt},
                timeout=self.api_timeout,
                verify=False,
            ).content
            if response and not response.startswith(b"{"):
                image_bytes = response
                break

        if not image_bytes:
            return None

        image_path = path.join(
            self.image_path,
            f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{prompt[:40]}.jpg"
        )
        with open(image_path, "wb") as file:
            file.write(image_bytes)
        return image_path

    async def is_waiting_response_needed(self, tool_name: str) -> bool:
        return True

    def _get_configured_hf_model(self):
        last_part = self.api_url.split("/")[-1]
        return last_part

    def get_tools(self) -> list[tuple[str, dict]]:
        tools = [
            (
                "generate_image_huggingface",
                {
                    "type": "function",
                    "function": {
                        "name": "generate_image_huggingface",
                        "description": f"Generate an image based on the users prompt using {self._get_configured_hf_model()}. Generated images are located here (Dont tell the user without him asking): {self.image_path}",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string"},
                            },
                            "required": ["prompt"],
                        },
                    },
                },
            ),
        ]

        return tools
