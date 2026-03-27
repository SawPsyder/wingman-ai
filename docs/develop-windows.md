# Developing on Windows

## Pre-requisites

You need **Python 3.11.7** and some dependencies to run Wingman AI Core. We recommend using a virtual environment to keep your system clean.

We do **NOT** recommend installing Python from the Microsoft Store because it runs in a sandbox environment and creates config files in directories we can't detect properly.

### The quick and easy way

Install Python 3.11.7 from [python.org](https://www.python.org/downloads/release/python-3117/) and add it to your `PATH`. Make sure to check the box **Add Python 3.11 to PATH** during the installation.

Then (re-)start your terminal and test with `python --version`.

### The clean and better way

Use [pyenv-win](https://github.com/pyenv-win/pyenv-win) to manage multiple Python versions on your system. Install it using their documentation, then:

```bash
pyenv install 3.11.7    # install Python with pyenv
pyenv global 3.11.7     # set your global Python version
```

Restart the terminal. Test with `python --version`.

## Install dependencies

Fork and clone the repository, then start a terminal in the root folder.

```bash
python -m venv venv                 # create a virtual environment
.\venv\scripts\activate             # activate the virtual environment
pip install -r requirements.txt     # install dependencies
```

### NVIDIA GPU acceleration (optional)

If you have an NVIDIA RTX GPU and want to use CUDA for FasterWhisper and PocketTTS:

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
```

This replaces the CPU-only torch that `requirements.txt` installs with a CUDA 12.4 build.

## Copy runtime dependencies from the Wingman AI client

The release version of Wingman AI bundles several model files and binaries that are too large for git. To get the full experience in your dev environment, install the latest [Wingman AI release](https://wingman-ai.com) and copy the following directories from the installation directory (default: `C:\Program Files\Wingman AI\`) into your repository root:

| Directory | Purpose | What happens if you skip it |
| --- | --- | --- |
| `faster-whisper-models/` | Pre-downloaded speech recognition models | Models auto-download from HuggingFace on first use — this can be slow and may crash on first launch. Restart and it will work. |
| `pocket-tts-models/` | PocketTTS text-to-speech model weights | Models auto-download on first use. |
| `pocket-tts-voices/` | Pre-packaged TTS voice samples | Voices auto-download on first use. |
| `whispercpp/` | WhisperCPP binary (optional, legacy) | Only needed if you use whispercpp instead of FasterWhisper. |
| `whispercpp-cuda/` | WhisperCPP CUDA variant (optional, legacy) | Same as above, for NVIDIA GPUs. |

Copying these directories is optional but recommended — it avoids slow first-launch downloads and potential timeouts.

## Setup Visual Studio Code

Open the root folder in Visual Studio Code. It should automatically detect the virtual environment and suggest the correct Python interpreter. If not, open the command palette (`Ctrl+Shift+P`), run `Python: Select Interpreter`, and select the `venv` you created.

The repo includes recommended extensions in `.vscode/extensions.json` — install them when prompted.

Press `F5` to launch `main.py` via the preconfigured debugger. The Wingman AI Core API server will start on `127.0.0.1:49111`. Connect the Wingman AI client to use it.

If it doesn't start, verify that:

- The virtual environment is selected as the Python interpreter
- All dependencies are installed (`pip install -r requirements.txt`)
- The integrated terminal is running from the repository root directory

## Developing Skills

See the full [Skills Developer Documentation](../skills/README.md) for everything you need to know about creating skills — discovery metadata, the `@tool` decorator, hooks, custom properties, bundling dependencies, and distribution.

If you're building a major skill or integration, please reach out on [Discord](https://www.shipbit.de/discord) first to make sure it aligns with the project's direction.
