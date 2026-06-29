"""Thin, pluggable model wrapper for the populator.

Reuses the Phase-1 provider layer in `eval/src/providers.py` (Groq + throttle/retry)
and adds the local qwen2.5-coder models served by Ollama.  Crucially this works in two
deployment modes with the same code:

  * on this Mac  -> use `--model groq` (cloud, fast, reliable) for development, OR a
    local Ollama if one is running;
  * on Colab     -> the populator script is shipped to the T4 and run there; `--model
    qwen7b` then hits the Ollama serving qwen2.5-coder on localhost:11434 (the chosen
    local default from Phase 1).  This is the headline "can the free local model author
    working data generators?" experiment.

Every call returns the provider dict: {text, prompt_tokens, completion_tokens, latency_s, error}.
"""
from __future__ import annotations

import sys
from pathlib import Path

_EVAL_SRC = Path(__file__).resolve().parents[1] / "eval" / "src"
sys.path.insert(0, str(_EVAL_SRC))

import providers  # noqa: E402  (from eval/src)

# friendly name -> (provider, model_id)
REGISTRY = {
    # default cloud authoring model. Switched 2026-06-29 off llama-3.3-70b-versatile (being
    # deprecated by Groq) to Qwen3.6-27B. The old Llama is kept under `groq-llama` for A/B.
    "groq":   ("groq", "qwen/qwen3.6-27b"),
    "groq-llama": ("groq", "llama-3.3-70b-versatile"),  # deprecated; kept for comparison only
    "qwen1.5b": ("ollama", "qwen2.5-coder:1.5b"),  # weak model — the Phase-6 simulated student
    "qwen7b":  ("ollama", "qwen2.5-coder:7b"),
    "qwen14b": ("ollama", "qwen2.5-coder:14b"),
    "qwen3coder": ("ollama", "qwen3-coder:30b"),  # Qwen3-Coder-30B-A3B (MoE, 3.3B active)
    "qwen32b": ("ollama", "qwen2.5-coder:32b"),   # dense 32B, fits L4
    "gptoss":  ("groq", "openai/gpt-oss-120b"),
}

_FN = {"groq": providers.gen_groq, "ollama": providers.gen_ollama,
       "gemini": providers.gen_gemini}

# Qwen2.5-Coder's official recommended sampling (generation_config.json): NOT greedy —
# do_sample=true, temp 0.7 / top_p 0.8 / top_k 20 / repeat_penalty 1.05. Greedy (temp 0)
# is discouraged for Qwen (repetition/degradation). seed pins it for reproducible runs.
_QWEN_OPTS = {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
              "repeat_penalty": 1.05, "seed": 0}


def call(model_name: str, prompt: str, max_retries: int = 5) -> dict:
    if model_name not in REGISTRY:
        raise ValueError(f"unknown model '{model_name}'; choices: {list(REGISTRY)}")
    provider, model_id = REGISTRY[model_name]
    fn = _FN[provider]
    use_qwen_opts = provider == "ollama" and "qwen" in model_id.lower()
    last = None
    for attempt in range(max_retries + 1):
        providers._throttle(provider)
        if use_qwen_opts:
            # vary the seed per attempt so a repair retry doesn't reproduce the same output
            last = fn(model_id, prompt, options={**_QWEN_OPTS, "seed": attempt})
        else:
            last = fn(model_id, prompt)
        if not providers._is_retryable(last["error"]):
            return last
        if attempt < max_retries:
            import random
            import time
            time.sleep(min(60.0, 5.0 * (2 ** attempt)) + random.uniform(0, 2))
    return last


def cost_usd(model_name: str, pt: int, ct: int) -> float:
    # only cloud models carry a price; locals are 0. ($/M input, $/M output)
    # NOTE: the "groq" price is for Qwen3.6-27B and is APPROXIMATE — verify against Groq's
    # current pricing page; "groq-llama" keeps the old llama-3.3-70b rate for comparison.
    table = {"groq": (0.29, 0.59), "groq-llama": (0.59, 0.79), "gptoss": (0.15, 0.75)}
    if model_name not in table:
        return 0.0
    pin, pout = table[model_name]
    return (pt * pin + ct * pout) / 1_000_000
