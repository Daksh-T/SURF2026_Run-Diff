"""Unified LLM calling across Groq, Gemini (AI Studio), and local Ollama.

Each generate() returns a dict: {text, prompt_tokens, completion_tokens, latency_s, error}.
Cost is computed separately from a price table so providers stay simple.
"""
from __future__ import annotations

import os
import random
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

GROQ_KEY = os.environ.get("groq_api_key", "")
GEMINI_KEY = os.environ.get("aistudio_api_key", "")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Honor OLLAMA_HOST so a non-default Ollama address works everywhere — the webapp's
# setup_ollama.py already reads this env var, and hint generation must hit the same host.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_URL = f"{OLLAMA_HOST}/api/chat"

# (provider, model_id) registry. Display name -> spec.
MODELS = {
    "gemma4-local":   ("ollama", "gemma4"),
    "phi4-local":     ("ollama", "phi4"),
    "groq-qwen3.6-27b":  ("groq", "qwen/qwen3.6-27b"),   # default authoring model (2026-06-29)
    "groq-llama3.3-70b": ("groq", "llama-3.3-70b-versatile"),  # deprecated; kept for A/B
    "groq-gpt-oss-120b": ("groq", "openai/gpt-oss-120b"),
    "gemini-3.5-flash":  ("gemini", "gemini-3.5-flash"),
    "gemini-3.1-pro":    ("gemini", "gemini-3.1-pro-preview"),
}

# Approx USD per 1M tokens (input, output). Local = 0. Update as needed.
PRICES = {
    "groq-qwen3.6-27b":  (0.29, 0.59),   # APPROXIMATE — verify against Groq pricing
    "groq-llama3.3-70b": (0.59, 0.79),
    "groq-gpt-oss-120b": (0.15, 0.75),
    "gemini-3.5-flash":  (0.30, 2.50),
    "gemini-3.1-pro":    (1.25, 10.0),
}


def _empty(latency, error=None):
    return {"text": "", "prompt_tokens": 0, "completion_tokens": 0,
            "latency_s": latency, "error": error}


# Groq reasoning models (Qwen3, gpt-oss) emit <think>…</think> chain-of-thought inline in the
# response content by default, which pollutes the SQL/hint text every caller here expects. Ask
# Groq to strip it (reasoning_format="hidden") for those models; non-reasoning models (llama)
# reject the param, so it is sent only when the model id is a known reasoning family.
#
# CRITICAL (regression found 2026-06-29): the reasoning tokens count against the completion
# budget. With Groq's small default max, the model spends the whole budget THINKING and returns
# EMPTY content (the authoring loop then fails @compile with code_len 0). So we must (a) raise
# max_completion_tokens to leave room for the actual answer, and (b) for Qwen3 — whose authoring
# quality does not need long CoT and whose token appetite blows the daily limit — disable
# thinking outright with reasoning_effort="none" (only Qwen3 accepts none; gpt-oss does not, so
# it just gets the larger budget). This restores llama-like behavior and cost.
_GROQ_REASONING = ("qwen3", "gpt-oss")
_GROQ_REASONING_MAX_TOKENS = 8192


def gen_groq(model_id, prompt, timeout=120):
    t0 = time.time()
    mid = model_id.lower()
    body = {"model": model_id, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}]}
    if any(tag in mid for tag in _GROQ_REASONING):
        body["reasoning_format"] = "hidden"            # strip <think> CoT, return only the answer
        body["max_completion_tokens"] = _GROQ_REASONING_MAX_TOKENS  # leave room past reasoning
        if "qwen3" in mid:
            body["reasoning_effort"] = "none"          # no CoT for authoring (cost + budget)
    try:
        r = requests.post(GROQ_URL, timeout=timeout,
            headers={"Authorization": f"Bearer {GROQ_KEY}"}, json=body)
        dt = time.time() - t0
        if r.status_code != 200:
            return _empty(dt, f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        u = d.get("usage", {})
        return {"text": d["choices"][0]["message"]["content"],
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "latency_s": dt, "error": None}
    except Exception as e:
        return _empty(time.time() - t0, repr(e))


def gen_gemini(model_id, prompt, timeout=120):
    t0 = time.time()
    url = f"{GEMINI_BASE}/models/{model_id}:generateContent?key={GEMINI_KEY}"
    try:
        r = requests.post(url, timeout=timeout,
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0}})
        dt = time.time() - t0
        if r.status_code != 200:
            return _empty(dt, f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        cand = d.get("candidates", [])
        if not cand:
            return _empty(dt, f"no candidates: {str(d)[:200]}")
        parts = cand[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        u = d.get("usageMetadata", {})
        return {"text": text,
                "prompt_tokens": u.get("promptTokenCount", 0),
                "completion_tokens": u.get("candidatesTokenCount", 0),
                "latency_s": dt, "error": None}
    except Exception as e:
        return _empty(time.time() - t0, repr(e))


def gen_ollama(model_id, prompt, timeout=600, options=None):
    # default: greedy (temp 0) for reproducible Phase-1 benchmarking. Callers that want a
    # model's recommended sampling (e.g. the populator) pass `options` to override.
    opts = {"temperature": 0} if options is None else options
    t0 = time.time()
    try:
        r = requests.post(OLLAMA_URL, timeout=timeout,
            json={"model": model_id, "stream": False,
                  "options": opts,
                  "messages": [{"role": "user", "content": prompt}]})
        dt = time.time() - t0
        if r.status_code != 200:
            return _empty(dt, f"HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        return {"text": d.get("message", {}).get("content", ""),
                "prompt_tokens": d.get("prompt_eval_count", 0),
                "completion_tokens": d.get("eval_count", 0),
                "latency_s": dt, "error": None}
    except Exception as e:
        return _empty(time.time() - t0, repr(e))


# --- per-provider throttle: keep request spacing under free-tier RPM/TPM limits ---
_MIN_INTERVAL = {"groq": 6.0, "gemini": 5.0, "ollama": 0.0}
_last_call: dict = {}
_lock = threading.Lock()


def _throttle(provider):
    gap = _MIN_INTERVAL.get(provider, 0.0)
    if gap <= 0:
        return
    with _lock:
        prev = _last_call.get(provider, 0.0)
        wait = gap - (time.time() - prev)
        if wait > 0:
            time.sleep(wait)
        _last_call[provider] = time.time()


_RETRYABLE = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
              "timeout", "Timeout", "ConnectionError", "ConnectTimeout")


def _is_retryable(err: str | None) -> bool:
    return bool(err) and any(tok in err for tok in _RETRYABLE)


def generate(display_name, prompt, max_retries=6):
    provider, model_id = MODELS[display_name]
    fn = {"groq": gen_groq, "gemini": gen_gemini, "ollama": gen_ollama}[provider]
    last = None
    for attempt in range(max_retries + 1):
        _throttle(provider)
        last = fn(model_id, prompt)
        if not _is_retryable(last["error"]):
            return last
        if attempt < max_retries:
            backoff = min(60.0, 5.0 * (2 ** attempt)) + random.uniform(0, 2)
            time.sleep(backoff)
    return last  # exhausted retries


def cost_usd(display_name, prompt_tokens, completion_tokens):
    if display_name not in PRICES:
        return 0.0
    pin, pout = PRICES[display_name]
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


def list_gemini_models():
    r = requests.get(f"{GEMINI_BASE}/models?key={GEMINI_KEY}", timeout=60)
    if r.status_code != 200:
        return f"HTTP {r.status_code}: {r.text[:300]}"
    return [m["name"].replace("models/", "")
            for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])]
