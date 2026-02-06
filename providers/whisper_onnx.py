from os import path
import platform
import gc
from typing import Optional
from api.enums import LogType
from api.interface import (
    WhisperOnnxSettings,
    WhisperOnnxTranscript,
    WhisperOnnxSttConfig,
    WingmanInitializationError,
)
from services.printr import Printr

MODELS_DIR = "whisper-onnx-models"


class WhisperOnnx:
    def __init__(
        self,
        settings: WhisperOnnxSettings,
        app_root_path: str,
        app_is_bundled: bool,
    ):
        self.printr = Printr()
        self.settings = settings
        self.model = None
        self.processor = None
        self.pipeline = None

        self.is_windows = platform.system() == "Windows"
        app_dir = path.dirname(app_root_path) if app_is_bundled else app_root_path
        self.models_dir = path.join(app_dir, MODELS_DIR)

        self.__update_model()

    def _get_execution_provider(self) -> str:
        """Determine the best ONNX Runtime execution provider based on settings."""
        device = self.settings.device
        if device == "auto":
            # Try DirectML first (Windows, works with AMD/Intel/NVIDIA)
            if self.is_windows:
                try:
                    import onnxruntime as ort

                    if "DmlExecutionProvider" in ort.get_available_providers():
                        return "DmlExecutionProvider"
                except ImportError:
                    pass
            # Try CUDA next (NVIDIA)
            try:
                import onnxruntime as ort

                if "CUDAExecutionProvider" in ort.get_available_providers():
                    return "CUDAExecutionProvider"
            except ImportError:
                pass
            # Fall back to CPU
            return "CPUExecutionProvider"
        elif device == "directml":
            return "DmlExecutionProvider"
        elif device == "cuda":
            return "CUDAExecutionProvider"
        else:
            return "CPUExecutionProvider"

    def __unload_model(self):
        """Unload the current model to free memory."""
        if self.pipeline is not None or self.model is not None:
            self.printr.print(
                "WhisperOnnx: Unloading current model to free memory...",
                server_only=True,
            )
            del self.pipeline
            del self.model
            del self.processor
            self.pipeline = None
            self.model = None
            self.processor = None
            gc.collect()

    def __update_model(self):
        self.__unload_model()

        model_id = self.settings.model_size

        try:
            from optimum.onnxruntime import ORTModelForSpeechSeq2Seq
            from transformers import AutoProcessor, pipeline

            provider = self._get_execution_provider()

            # Check for local model directory
            local_model_path = None
            local_path = path.join(self.models_dir, model_id.replace("/", "--"))
            if path.exists(local_path):
                local_model_path = local_path

            model_source = local_model_path or model_id

            self.model = ORTModelForSpeechSeq2Seq.from_pretrained(
                model_source,
                export=local_model_path is None,
                provider=provider,
            )

            # Save locally for future use if downloaded from hub
            if local_model_path is None:
                save_path = path.join(
                    self.models_dir, model_id.replace("/", "--")
                )
                self.model.save_pretrained(save_path)

            self.processor = AutoProcessor.from_pretrained(model_source)

            # Save processor locally too
            if local_model_path is None:
                self.processor.save_pretrained(save_path)

            self.pipeline = pipeline(
                "automatic-speech-recognition",
                model=self.model,
                tokenizer=self.processor.tokenizer,
                feature_extractor=self.processor.feature_extractor,
            )

            self.printr.print(
                f"WhisperOnnx initialized with model '{model_id}' (provider: '{provider}').",
                server_only=True,
                color=LogType.POSITIVE,
            )
        except ImportError as e:
            self.printr.toast_error(
                f"WhisperOnnx requires 'optimum[onnxruntime]' and 'transformers'. Install with: pip install optimum[onnxruntime] transformers. Error: {e}"
            )
        except Exception as e:
            self.printr.toast_error(
                f"Failed to initialize WhisperOnnx with model {model_id}. Error: {e}"
            )

    def transcribe(
        self,
        config: WhisperOnnxSttConfig,
        filename: str,
        language: Optional[str] = None,
    ) -> Optional[WhisperOnnxTranscript]:
        if self.pipeline is None:
            self.printr.toast_error(
                "WhisperOnnx model is not loaded. Cannot transcribe."
            )
            return None

        try:
            generate_kwargs = {}
            lang = language or config.language
            if lang:
                generate_kwargs["language"] = lang
                generate_kwargs["task"] = "transcribe"

            result = self.pipeline(
                filename,
                generate_kwargs=generate_kwargs,
                return_timestamps=False,
            )

            text = result.get("text", "").strip()

            return WhisperOnnxTranscript(
                text=text,
                language=lang or "en",
            )

        except FileNotFoundError:
            self.printr.toast_error(
                f"WhisperOnnx file to transcribe '{filename}' not found."
            )
        except Exception as e:
            self.printr.toast_error(f"WhisperOnnx failed to transcribe. Error: {e}")

        return None

    def update_settings(self, settings: WhisperOnnxSettings):
        if self.settings == settings:
            self.printr.print("WhisperOnnx settings unchanged.", server_only=True)
            return
        self.printr.print(
            "WhisperOnnx settings updated, reloading model..", server_only=True
        )
        self.settings = settings
        self.__update_model()

    def validate(self, errors: list[WingmanInitializationError]):
        pass
