"""
Utility functions for OpenAI API interactions.
"""
import re
from services.printr import Printr

printr = Printr()


def get_minimal_reasoning_by_model(model_name: str) -> dict:
    """
    Returns the minimal reasoning effort setting based on the model name.
    This helps reduce latency by setting the lowest supported reasoning effort.
    See https://platform.openai.com/docs/api-reference/chat/create#chat_create-reasoning_effort

    Args:
        model_name: The name of the OpenAI model

    Returns:
        dict: Dictionary with reasoning_effort key if applicable, empty dict otherwise
    """
    # Models that don't support reasoning effort parameter
    if model_name in ["o1-mini", "gpt-5.2-chat-latest"]:
        return {}

    # o-series models (o1, o3, etc.) support "low" as minimal
    if model_name.startswith("o"):
        return {"reasoning_effort": "low"}

    # gpt-5.x models (5.1, 5.2, etc.) support "none" as minimal
    if model_name.startswith("gpt-5."):
        return {"reasoning_effort": "none"}

    # gpt-5 base models support "minimal" as lowest effort
    if model_name.startswith("gpt-5"):
        return {"reasoning_effort": "minimal"}

    # Other models don't support reasoning effort
    return {}


def handle_provider_key_error(provider_name: str):
    """
    Handle invalid API key errors for providers.
    
    Args:
        provider_name: The name of the provider (e.g., "OpenAI", "Gemini")
    """
    printr.toast_error(
        f"The {provider_name} API key you provided is invalid. Please check the GUI settings or your 'secrets.yaml'"
    )


def handle_provider_api_error(api_response):
    """
    Handle API errors from OpenAI-compatible providers.
    
    Args:
        api_response: The API error response object from OpenAI/compatible providers.
                     Expected to have attributes:
                     - status_code: HTTP status code (int)
                     - type: Error type (str)
                     - message: Error message (str)
    """
    printr.toast_error(
        f"The API sent the following error code {api_response.status_code} ({api_response.type})"
    )
    m = re.search(
        r"'message': (?P<quote>['\"])(?P<message>.+?)(?P=quote)",
        api_response.message,
    )
    if m is not None:
        message = m["message"].replace(". ", ".\n")
        printr.toast_error(message)
    elif api_response.message:
        printr.toast_error(api_response.message)
    else:
        printr.toast_error("The API did not provide further information.")
