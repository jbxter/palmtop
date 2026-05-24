#!/usr/bin/env bash
set -euo pipefail

# Bootstrap Palmtop dev environment on macOS (Apple Silicon)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MODEL_DIR="models"
MODEL_NAME="phi-3.5-mini-instruct-q4_k_m.gguf"
MODEL_URL="https://huggingface.co/bartowski/Phi-3.5-mini-instruct-GGUF/resolve/main/Phi-3.5-mini-instruct-Q4_K_M.gguf"

echo "=== Palmtop macOS bootstrap ==="

# ── Check prerequisites ──────────────────────────────────────────────
command -v uv >/dev/null 2>&1 || {
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
}

# ── Create venv and install deps ─────────────────────────────────────
echo "Creating virtual environment and installing dependencies..."

# llama-cpp-python needs CMAKE_ARGS for Metal acceleration on Apple Silicon
export CMAKE_ARGS="-DGGML_METAL=on"

uv sync --extra all --extra dev
echo "Dependencies installed."

# ── Download model if missing ────────────────────────────────────────
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_DIR/$MODEL_NAME" ]; then
    echo "Downloading $MODEL_NAME (~2.3 GB)..."
    curl -L --progress-bar -o "$MODEL_DIR/$MODEL_NAME" "$MODEL_URL"
    echo "Model downloaded."
else
    echo "Model already present: $MODEL_DIR/$MODEL_NAME"
fi

# ── Create config.toml if missing ────────────────────────────────────
if [ ! -f config.toml ]; then
    cp config.example.toml config.toml
    echo "Created config.toml from example. Edit it if needed."
fi

# ── Summary ──────────────────────────────────────────────────────────
echo ""
echo "=== Done ==="
echo "Next steps:"
echo "  1. Set your Telegram bot token:"
echo "     export TELEGRAM_BOT_TOKEN='your-token-here'"
echo "  2. Run the agent:"
echo "     uv run python -m palmtop"
echo ""
echo "To get a bot token, talk to @BotFather on Telegram."
