import argparse
import asyncio
import atexit
from enum import Enum
from os import path
import signal
import sys
import traceback
from typing import Any, Literal, get_args, get_origin
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.concurrency import asynccontextmanager
from fastapi.routing import APIRoute
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from api.commands import WebSocketCommandModel
from api.interface import BenchmarkResult
from api.enums import ENUM_TYPES, LogType, WingmanInitializationErrorType
import keyboard.keyboard as keyboard
from services.command_handler import CommandHandler
from services.config_manager import ConfigManager
from services.connection_manager import ConnectionManager
from services.esp32_handler import Esp32Handler
from services.secret_keeper import SecretKeeper
from services.printr import Printr
from services.system_manager import SystemManager
from wingman_core import WingmanCore

port = None
host = None

connection_manager = ConnectionManager()


printr = Printr()
Printr.set_connection_manager(connection_manager)

app_is_bundled = getattr(sys, "frozen", False)
app_root_path = sys._MEIPASS if app_is_bundled else path.dirname(path.abspath(__file__))

# creates all the configs from templates - do this first!
config_manager = ConfigManager(app_root_path)
printr.print(
    f"Config directory: {config_manager.config_dir}",
    server_only=True,
    color=LogType.HIGHLIGHT,
)

secret_keeper = SecretKeeper()
SecretKeeper.set_connection_manager(connection_manager)

system_manager = SystemManager()
printr.print(
    f"Wingman AI Core v{system_manager.local_version}",
    server_only=True,
    color=LogType.HIGHLIGHT,
)

is_latest_version = system_manager.check_version()
if not is_latest_version:
    printr.print(
        "A new Wingman AI version is available! Download at https://www.wingman-ai.com",
        server_only=True,
        color=LogType.WARNING,
    )

# uses the Singletons above, so don't move this up!
core = WingmanCore(
    config_manager=config_manager,
    app_root_path=app_root_path,
    app_is_bundled=app_is_bundled,
)
core.set_connection_manager(connection_manager)

keyboard.hook(core.on_key)


def custom_generate_unique_id(route: APIRoute):
    return f"{route.tags[0]}-{route.name}"


def modify_openapi():
    """Strip the tagname of the functions (for the client) in the OpenAPI spec"""
    openapi_schema = app.openapi()
    for path_data in openapi_schema["paths"].values():
        for operation in path_data.values():
            tags = operation.get("tags")
            if tags:
                tag = tags[0]
                operation_id = operation.get("operationId")
                if operation_id:
                    to_remove = f"{tag}-"
                    new_operation_id = operation_id[len(to_remove) :]
                    operation["operationId"] = new_operation_id
    app.openapi_schema = openapi_schema


async def shutdown():
    await connection_manager.shutdown()
    await core.shutdown()
    keyboard.unhook_all()


def exit_handler():
    printr.print(
        "atexit handler shutting down...", color=LogType.SUBTLE, server_only=True
    )
    asyncio.run(shutdown())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # executed before the application starts
    modify_openapi()

    yield

    # executed after the application has finished
    printr.print(
        "Lifespan end - shutting down...", color=LogType.SUBTLE, server_only=True
    )
    await shutdown()


app = FastAPI(lifespan=lifespan, generate_unique_id_function=custom_generate_unique_id)


def custom_openapi():
    global host

    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Wingman AI Core REST API",
        version=str(system_manager.local_version),
        description="Communicate with Wingman AI Core",
        routes=app.routes,
    )

    # Add custom server configuration
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"
    openapi_schema["servers"] = [{"url": f"{host}:{port}"}]

    # Ensure the components.schemas key exists
    openapi_schema.setdefault("components", {}).setdefault("schemas", {})

    # Add enums to schema
    for enum_name, enum_model in ENUM_TYPES.items():
        enum_field_name, enum_type = next(iter(enum_model.__annotations__.items()))
        if issubclass(enum_type, Enum):
            enum_values = [e.value for e in enum_type]
            enum_schema = {
                "type": "string",
                "enum": enum_values,
                "description": f"Possible values for {enum_name}",
            }
            openapi_schema["components"]["schemas"][enum_name] = enum_schema

    openapi_schema["components"]["schemas"]["CommandActionConfig"] = {
        "type": "object",
        "properties": {
            "keyboard": {"$ref": "#/components/schemas/CommandKeyboardConfig"},
            "wait": {"type": "number"},
            "mouse": {"$ref": "#/components/schemas/CommandMouseConfig"},
            "write": {"type": "string"},
            "audio": {"$ref": "#/components/schemas/AudioFileConfig"},
            "joystick": {"$ref": "#/components/schemas/CommandJoystickConfig"},
        },
    }

    # Add WebSocket command models to schema
    for cls in WebSocketCommandModel.__subclasses__():
        cls_schema_dict = cls.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )

        for field_name, field_type in cls.__annotations__.items():
            origin = get_origin(field_type)
            if origin is Literal:
                literal_args = get_args(field_type)
                if len(literal_args) == 1:
                    literal_value = literal_args[0]
                    cls_schema_dict["properties"][field_name] = {
                        "type": "string",
                        "enum": [literal_value],
                    }
                else:
                    cls_schema_dict["properties"][field_name] = {
                        "type": "string",
                        "enum": list(literal_args),
                    }

                cls_schema_dict.setdefault("required", []).append(field_name)
        openapi_schema["components"]["schemas"][cls.__name__] = cls_schema_dict

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# if a class adds GET/POST endpoints, add them here:
app.include_router(core.router)
app.include_router(core.config_service.router)
app.include_router(core.settings_service.router)
app.include_router(core.voice_service.router)

app.include_router(system_manager.router)
app.include_router(secret_keeper.router)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await connection_manager.connect(websocket)
    command_handler = CommandHandler(connection_manager, core)
    try:
        while True:
            message = await websocket.receive_text()
            await command_handler.dispatch(message, websocket)
    except WebSocketDisconnect:
        await connection_manager.disconnect(websocket)
        await printr.print_async("Client disconnected", server_only=True)


# Websocket for ESP32 clients to stream audio to and from
@app.websocket("/")
async def oi_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    esp32_handler = Esp32Handler(core)
    receive_task = asyncio.create_task(esp32_handler.receive_messages(websocket))
    send_task = asyncio.create_task(esp32_handler.send_messages(websocket))
    try:
        await asyncio.gather(receive_task, send_task)
    except Exception as e:
        print(traceback.format_exc())
        print(f"Connection lost. Error: {e}")


@app.websocket("/ws/audio")
async def websocket_global_audio_endpoint(websocket: WebSocket):
    await websocket.accept()
    printr.print(
        f"Audio client {websocket.client.host} connected",
        server_only=True,
        color=LogType.SUBTLE,
    )

    # Track connection state
    is_connected = True

    # Reference to the callback function for cleanup
    audio_callback = None

    try:
        # Wait for the audio player to be ready
        retry_count = 0
        max_retries = 5

        while retry_count < max_retries and is_connected:
            # Check if audio_player is ready
            if (
                core.audio_player
                and hasattr(core.audio_player, "stream_event")
                and core.audio_player.stream_event is not None
            ):
                try:
                    # Define handler for audio chunks
                    async def on_audio_chunk(data: bytes):
                        nonlocal is_connected

                        if not is_connected:
                            return

                        try:
                            # Forward the audio chunk to the browser client
                            await websocket.send_bytes(data)
                        except Exception as e:
                            printr.print(
                                f"Error sending audio: {str(e)}",
                                server_only=True,
                                color=LogType.ERROR,
                            )
                            is_connected = False

                    # Save reference to the callback for later cleanup
                    audio_callback = on_audio_chunk

                    # Subscribe without expecting a return value
                    core.audio_player.stream_event.subscribe("audio", audio_callback)
                    printr.print(
                        "Audio subscription successful",
                        server_only=True,
                        color=LogType.SUBTLE,
                    )
                    break

                except Exception as e:
                    printr.print(
                        f"Error subscribing to audio: {str(e)}",
                        server_only=True,
                        color=LogType.WARNING,
                    )

            # Not ready or subscription failed, wait and retry
            retry_count += 1
            if retry_count < max_retries:
                await asyncio.sleep(1)
            else:
                printr.print(
                    "Audio player not ready after multiple attempts",
                    server_only=True,
                    color=LogType.WARNING,
                )
                await websocket.close(code=1013)
                return

        # Keep connection open until client disconnects
        while is_connected:
            try:
                await websocket.receive_text()
            except:
                is_connected = False
                break

    except WebSocketDisconnect:
        printr.print(
            f"Audio client disconnected", server_only=True, color=LogType.SUBTLE
        )
    except Exception as e:
        printr.print(f"Audio error: {str(e)}", server_only=True, color=LogType.ERROR)
    finally:
        # Clean up subscription using the audio_callback reference
        if (
            audio_callback is not None
            and core.audio_player
            and hasattr(core.audio_player, "stream_event")
            and core.audio_player.stream_event is not None
        ):
            try:
                core.audio_player.stream_event.unsubscribe("audio", audio_callback)
                printr.print(
                    "Audio unsubscribed successfully",
                    server_only=True,
                    color=LogType.SUBTLE,
                )
            except Exception as e:
                printr.print(
                    f"Error unsubscribing from audio: {str(e)}",
                    server_only=True,
                    color=LogType.ERROR,
                )


@app.post("/start-secrets", tags=["main"])
async def start_secrets(secrets: dict[str, Any]):
    await secret_keeper.post_secrets(secrets)
    core.startup_errors = []
    await core.config_service.load_config()


@app.get("/ping", tags=["main"], response_model=str)
async def ping():
    return "Ok" if core.is_started else "Starting"


@app.get("/client/is-pro", tags=["main"], response_model=bool)
async def is_client_pro():
    return core.is_client_pro


@app.get("/client/account-name", tags=["main"], response_model=str)
async def get_client_account_name():
    return core.client_account_name


# required to generate API specs for class BenchmarkResult that is only used internally
@app.get("/dummy-benchmark", tags=["main"], response_model=BenchmarkResult)
async def get_dummy_benchmark():
    return BenchmarkResult(
        label="Sample Benchmark",
        execution_time_ms=150.0,
        formatted_execution_time=0.15,
        snapshots=[
            BenchmarkResult(
                label="Sub Benchmark",
                execution_time_ms=75.0,
                formatted_execution_time=0.075,
            )
        ],
    )


async def async_main(host: str, port: int, sidecar: bool):
    await core.config_service.migrate_configs(system_manager)
    await core.config_service.load_config()
    saved_secrets: list[str] = []
    for error in core.tower_errors:
        if (
            not sidecar  # running standalone
            and error.error_type == WingmanInitializationErrorType.MISSING_SECRET
            and not error.secret_name in saved_secrets
        ):
            secret = input(f"Please enter your '{error.secret_name}' API key/secret: ")
            if secret:
                secret_keeper.secrets[error.secret_name] = secret
                await secret_keeper.save()
                saved_secrets.append(error.secret_name)
            else:
                return
        else:
            core.startup_errors.append(error)

    try:
        await core.startup()
        event_loop = asyncio.get_running_loop()
        core.audio_player.set_event_loop(event_loop)
        asyncio.create_task(core.process_events())
        core.is_started = True
    except Exception as e:
        printr.print(f"Error starting Wingman AI Core: {str(e)}", color=LogType.ERROR)
        printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
        return

    try:
        config = uvicorn.Config(app=app, host=host, port=port, lifespan="on")
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        printr.print(f"Error starting uvicorn server: {str(e)}", color=LogType.ERROR)
        printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the FastAPI server.")
    parser.add_argument(
        "-H",
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host for the FastAPI server to listen on.",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=str,
        default="49111",
        help="Port for the FastAPI server to listen on.",
    )
    parser.add_argument(
        "--sidecar",
        action="store_true",
        help="Whether or not Wingman AI Core was launched from a client (as sidecar).",
    )
    args = parser.parse_args()

    host = args.host
    port = int(args.port)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # No running event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    atexit.register(exit_handler)

    def signal_handler(sig, frame):
        printr.print(
            "SIGINT/SIGTERM received! Initiating shutdown...",
            color=LogType.SUBTLE,
            server_only=True,
        )
        # Schedule the shutdown asynchronously
        asyncio.create_task(shutdown())
        loop.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        loop.run_until_complete(async_main(host=host, port=port, sidecar=args.sidecar))
    except Exception as e:
        printr.print(f"Error starting application: {str(e)}", color=LogType.ERROR)
        printr.print(traceback.format_exc(), color=LogType.ERROR, server_only=True)
