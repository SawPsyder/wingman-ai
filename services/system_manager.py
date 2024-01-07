import platform
from fastapi import APIRouter
import requests
from packaging import version
from api.interface import SystemCore, SystemInfo
from services.printr import Printr

LOCAL_VERSION = "1.1.3b1"
VERSION_ENDPOINT = "https://shipbit.de/wingman.json"

printr = Printr()


class SystemManager:
    def __init__(self):
        self.router = APIRouter()
        self.router.add_api_route(
            methods=["GET"],
            path="/system-info",
            endpoint=self.get_system_info,
            response_model=SystemInfo,
            tags=["system"],
        )

        self.latest_version = version.parse("0.0.0")
        self.local_version = version.parse(LOCAL_VERSION)
        self.check_version()

    def check_version(self):
        try:
            app_version = self.local_version

            response = requests.get(VERSION_ENDPOINT, timeout=10)
            response.raise_for_status()

            remote_version_str = response.json().get("version", None)
            remote_version = version.parse(remote_version_str)

            self.latest_version = remote_version

            return app_version >= remote_version

        except requests.RequestException:
            msg = "Could not reach version endpoint."
            printr.toast_warning(f"Error fetching version information: \n{msg}")
            return False
        except ValueError as e:
            printr.toast_warning(f"Error with version information: {e}")
            return False

    def current_version_is_latest(self):
        return self.local_version >= self.latest_version

    def get_local_version(self, as_string=True) -> str | version.Version:
        return LOCAL_VERSION if as_string else self.local_version

    def get_latest_version(self, as_string=True) -> str | version.Version:
        return str(self.latest_version) if as_string else self.latest_version

    # GET /system-info
    def get_system_info(self):
        printr.print("Checking for updates...", server_only=True)
        is_latest = self.check_version()

        return SystemInfo(
            os=platform.system(),
            core=SystemCore(
                version=str(LOCAL_VERSION),
                latest=str(self.latest_version),
                isLatest=is_latest,
            ),
        )