#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Bootstrap Palmtop on Android / Termux (Galaxy S21)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MODEL_DIR="models"
MODEL_NAME="phi-3.5-mini-instruct-q4_k_m.gguf"
MODEL_URL="https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf"

echo "=== Palmtop Termux bootstrap ==="

# ── System packages ──────────────────────────────────────────────────
echo "Installing system packages..."
pkg update -y
pkg install -y python git cmake ninja build-essential curl

# Attempt Vulkan SDK — may not be available in all Termux versions
if pkg install -y vulkan-loader-android vulkan-headers 2>/dev/null; then
    HAS_VULKAN=true
    echo "Vulkan packages installed."
else
    HAS_VULKAN=false
    echo "Vulkan packages not available — will fall back to CPU."
fi

# termux-api for SMS channel later
pkg install -y termux-api 2>/dev/null || echo "termux-api not available (install Termux:API app)"

# ── Install uv ───────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Tell uv to use Termux's system Python (uv can't download Python for android)
export UV_PYTHON_PREFERENCE=only-system

# ── Build llama.cpp Python bindings ──────────────────────────────────
echo "Installing Python dependencies..."
echo "Using system Python: $(python3 --version)"

# Create venv and install pure-Python deps first
uv sync --no-install-package llama-cpp-python

# Install llama-cpp-python build deps into the venv so we can skip
# build isolation (avoids building the cmake Python package from source —
# it fails on Termux because libarchive can't find libmd during bootstrap).
# System cmake from `pkg install cmake` is used instead.
uv pip install scikit-build-core numpy

if [ "$HAS_VULKAN" = true ]; then
    echo "Attempting build with Vulkan acceleration..."
    export CMAKE_ARGS="-DGGML_VULKAN=on"
    if ! uv pip install --no-build-isolation llama-cpp-python 2>&1; then
        echo "Vulkan build failed — falling back to CPU..."
        export CMAKE_ARGS="-DGGML_VULKAN=off"
        uv pip install --no-build-isolation llama-cpp-python
    fi
else
    echo "Building with CPU only..."
    export CMAKE_ARGS="-DGGML_VULKAN=off"
    uv pip install --no-build-isolation llama-cpp-python
fi

echo "Dependencies installed."

# Telegram channel (default on many setups; pure Python — safe on Termux)
echo "Installing Telegram channel..."
uv sync --extra telegram --no-install-package llama-cpp-python 2>/dev/null \
    || uv pip install "python-telegram-bot>=22.0"

# ── Download model if missing ────────────────────────────────────────
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/$MODEL_NAME" ]; then
    echo "Downloading $MODEL_NAME (~2.3 GB) — make sure you're on Wi-Fi..."
    curl -L --progress-bar -o "$MODEL_DIR/$MODEL_NAME" "$MODEL_URL"
    echo "Model downloaded."
else
    echo "Model already present: $MODEL_DIR/$MODEL_NAME"
fi

# ── Create config.toml if missing ────────────────────────────────────
if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    # Phone-specific defaults: fewer GPU layers, more threads for Snapdragon 888
    sed -i 's/n_gpu_layers = -1/n_gpu_layers = 0/' config.toml
    sed -i 's/n_threads = 4/n_threads = 8/' config.toml
    echo "Created config.toml with phone defaults."
fi

# Ensure channel matches how you talk to Julian (telegram vs SMS)
if ! grep -q '^channel' config.toml 2>/dev/null; then
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
        echo 'channel = "telegram"' >> config.toml
        echo "Set channel=telegram (TELEGRAM_BOT_TOKEN is set)."
    fi
fi

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "=== Done ==="
echo "Next steps:"
echo "  1. Run the agent (SMS channel is the default on phone):"
echo "     uv run python -m palmtop"
echo ""
echo "To keep it running in the background, use termux-services or:"
echo "  nohup uv run python -m palmtop > agent.log 2>&1 &"
