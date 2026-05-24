from __future__ import annotations

import asyncio
import logging
from functools import partial

from llama_cpp import Llama

from pocket_agent.config.settings import InferenceConfig
from pocket_agent.inference.base import Message

log = logging.getLogger(__name__)


class LocalBackend:
    """llama.cpp inference via llama-cpp-python."""

    def __init__(self, config: InferenceConfig) -> None:
        if not config.model_path:
            raise ValueError("inference.model_path is required")
        log.info("Loading model: %s", config.model_path)
        self._llm = Llama(
            model_path=config.model_path,
            n_ctx=config.n_ctx,
            n_gpu_layers=config.n_gpu_layers,
            n_threads=config.n_threads,
            verbose=False,
        )
        log.info("Model loaded")

    async def complete(self, messages: list[Message], max_tokens: int = 512) -> str:
        formatted = [{"role": m.role, "content": m.content} for m in messages]
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            partial(
                self._llm.create_chat_completion,
                messages=formatted,
                max_tokens=max_tokens,
            ),
        )
        return result["choices"][0]["message"]["content"]

    async def close(self) -> None:
        del self._llm
