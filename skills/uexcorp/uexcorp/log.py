"""Warnings the UEX skill should show even when debug logging is off."""

def warn(message: str) -> None:
    from api.enums import LogType
    from services.printr import Printr

    Printr().print(message, color=LogType.WARNING, server_only=True, source_name="UEX")


def info(message: str) -> None:
    from api.enums import LogType
    from services.printr import Printr

    Printr().print(message, color=LogType.INFO, server_only=True, source_name="UEX")
