from google import genai
from google.genai import types
from openai import APIStatusError, OpenAI
from services.openai_utils import handle_provider_key_error, handle_provider_api_error
from services.printr import Printr

printr = Printr()


class GoogleGenAI:
    def __init__(self, api_key: str):
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(api_version="v1alpha"),
        )
        self.openai_client = OpenAI(
            api_key=api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

    def _handle_key_error(self):
        handle_provider_key_error("Gemini")

    def _handle_api_error(self, api_response):
        handle_provider_api_error(api_response)

    def get_minimal_reasoning_by_model(self, model_name: str) -> dict:
        """Return minimal allowed OpenAI `reasoning_effort` for Gemini models.

        Gemini models support thinking controls that map to OpenAI's `reasoning_effort`.
        We always send the lowest allowed value to minimize latency.

                Rules (practical constraints from Gemini's OpenAI-compatible endpoint):
                - Gemini 2.5 Flash-family: supports `reasoning_effort="none"` (fastest).
                - Gemini 2.5 Pro-family: do not disable thinking; use the lowest supported value.
                - Gemini 3-family: does NOT accept `"minimal"` (e.g. gemini-3-flash-preview).
                    The lowest supported value is `"low"`.

                For non-Gemini models and unknown/legacy Gemini aliases, we omit the parameter
                to avoid sending unsupported values.
        """

        if not model_name:
            return {}

        normalized = model_name.lower()

        # Only apply to Gemini models; don't risk sending unknown params to other providers.
        if "gemini" not in normalized:
            return {}

        # Gemini 2.5 models
        if "2.5" in normalized:
            # Reasoning cannot be turned off for 2.5 Pro.
            if "pro" in normalized:
                return {"reasoning_effort": "low"}
            # Flash and other non-Pro 2.5 variants allow disabling thinking.
            return {"reasoning_effort": "none"}

        # Gemini 3 models: minimal is not a valid value; use the lowest supported value.
        if re.search(r"(^|[^0-9])3([^0-9]|$)", normalized):
            return {"reasoning_effort": "low"}

        # Other Gemini aliases (e.g. gemini-flash-latest / gemini-pro-latest) may vary.
        # Don't send reasoning_effort unless we know it's supported.
        return {}

    def ask(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = False,
        tools: list[dict[str, any]] = None,
    ):
        try:
            reasoning_params = self.get_minimal_reasoning_by_model(model)
            if not tools:
                completion = self.openai_client.chat.completions.create(
                    stream=stream,
                    messages=messages,
                    model=model,
                    **reasoning_params,
                )
            else:
                completion = self.openai_client.chat.completions.create(
                    stream=stream,
                    messages=messages,
                    model=model,
                    tools=tools,
                    tool_choice="auto",
                    **reasoning_params,
                )
            return completion
        except APIStatusError as e:
            self._handle_api_error(e)
            return None
        except UnicodeEncodeError:
            self._handle_key_error()
            return None

    def get_available_models(self):
        models: list[types.Model] = []
        for model in self.client.models.list():
            for action in model.supported_actions:
                if action == "generateContent":
                    models.append(model)
        return models
