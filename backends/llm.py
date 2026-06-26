"""LLM backend integration.

Talks to a local llama.cpp server, an Ollama server, or any OpenAI-compatible
endpoint. Only depends on `requests` (python3-requests in apt).

The three modes:
  * llamacpp -> OpenAI-compatible /v1/chat/completions (llama-server exposes this)
  * openai   -> identical wire format, separate label for clarity / API keys
  * ollama   -> native /api/chat

A `mock` mode (driven by settings, handled by callers) lets the UI run with no
backend by returning canned structured content.
"""

import json
import logging
import random
import re

import requests

from database import all_settings

logger = logging.getLogger("musicworld.llm")


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings=None):
        s = settings or all_settings()
        self.backend = s.get("llm_backend", "llamacpp")
        self.base_url = s.get("llm_base_url", "http://localhost:8080").rstrip("/")
        self.model = s.get("llm_model", "default")
        self.api_key = s.get("llm_api_key", "")
        try:
            self.temperature = float(s.get("llm_temperature", 0.8))
        except (TypeError, ValueError):
            self.temperature = 0.8
        try:
            self.max_tokens = int(s.get("llm_max_tokens", 1500))
        except (TypeError, ValueError):
            self.max_tokens = 1500
        self.system_preamble = (s.get("llm_system_preamble", "") or "").strip()
        # Append a per-call entropy token to every prompt so identical, low-entropy
        # requests don't reconverge on the same output (see _vary). On by default;
        # set llm_vary_prompts to a falsey value to disable.
        self.vary_prompts = str(s.get("llm_vary_prompts", "1")).strip().lower() not in (
            "0", "false", "no", "off", "")
        self.last_seed = None  # the seed used by the most recent chat() call

    # -- low level -----------------------------------------------------------

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _vary(self, user):
        """Append a per-call entropy token to the user prompt.

        A fresh sampler seed alone does NOT guarantee different output: for an
        identical, low-entropy prompt ("invent an artist") the model reconverges
        on the same high-probability tokens every time (hence four artists all
        named "Elara Vance") regardless of the seed. Changing the prompt text
        itself forces a different decode path. The token is the same value as the
        sampler seed (so the two move together) and is placed at the END so the
        system prompt and the bulk of the user prompt remain a stable, cacheable
        prefix. The model is told to ignore its meaning — it exists only to break
        prompt-identity between otherwise-identical calls."""
        if not self.vary_prompts:
            return user
        return (f"{user}\n\n"
                f"[entropy: {self.last_seed:08x} — a random token to vary your "
                f"wording and choices on each call; ignore its literal meaning]")

    def chat(self, user, system=None, temperature=None, max_tokens=None, timeout=180,
             seed=None):
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens
        # Pick the sampler seed once, here, so it's the same value for whichever
        # backend handles the call and is inspectable afterwards as
        # `client.last_seed`. A fresh random seed per call is the default: without
        # one, llama-server reuses a fixed sampler seed and decodes similar prompts
        # to the same words every time. Callers that want variation between two
        # otherwise-identical calls (e.g. "regenerate") rely on this.
        self.last_seed = random.randint(0, 2**31 - 1) if seed is None else int(seed)
        logger.debug("LLM chat: seed=%s temperature=%s backend=%s",
                     self.last_seed, temperature, self.backend)
        # A configurable preamble (e.g. permissive framing for steerable models)
        # is prepended to the system prompt.
        if self.system_preamble:
            system = f"{self.system_preamble}\n\n{system}" if system else self.system_preamble
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": self._vary(user)})

        if self.backend == "ollama":
            return self._chat_ollama(messages, temperature, max_tokens, timeout)
        return self._chat_openai(messages, temperature, max_tokens, timeout)

    def _chat_openai(self, messages, temperature, max_tokens, timeout):
        url = f"{self.base_url}/v1/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # Seed chosen in chat(); see LLMClient.last_seed.
            "seed": self.last_seed,
            # Qwen3 and other reasoning models emit a <think>...</think> block
            # that eats the token budget before any JSON. llama.cpp passes this
            # through to the chat template; templates that don't use it ignore
            # it harmlessly.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            r = requests.post(url, headers=self._headers(), json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed ({url}): {exc}") from exc
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {exc}") from exc

    def _chat_ollama(self, messages, temperature, max_tokens, timeout):
        url = f"{self.base_url}/api/chat"
        body = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,  # Qwen3/DeepSeek-R1 etc.: skip the reasoning block
            "options": {"temperature": temperature, "num_predict": max_tokens,
                        "seed": self.last_seed},  # seed chosen in chat()
        }
        try:
            r = requests.post(url, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            return data["message"]["content"]
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed ({url}): {exc}") from exc
        except (KeyError, ValueError) as exc:
            raise LLMError(f"Unexpected Ollama response shape: {exc}") from exc

    # -- structured output ---------------------------------------------------

    def generate_json(self, user, system=None, **kwargs):
        """Ask the model for JSON and parse the first JSON object/array found."""
        guard = (
            "You respond with a single valid JSON value and nothing else. "
            "No markdown, no code fences, no commentary before or after."
        )
        system = f"{system}\n\n{guard}" if system else guard
        raw = self.chat(user, system=system, **kwargs)
        return _extract_json(raw)

    def ping(self, timeout=8):
        try:
            if self.backend == "ollama":
                r = requests.get(f"{self.base_url}/api/tags", timeout=timeout)
            else:
                r = requests.get(f"{self.base_url}/v1/models", timeout=timeout)
            r.raise_for_status()
            return True, f"Connected ({r.status_code})"
        except requests.RequestException as exc:
            return False, str(exc)


def _extract_json(text):
    text = text.strip()
    # Reasoning models (Qwen3, DeepSeek-R1, ...) wrap their scratch work in
    # <think>...</think>. Drop it before looking for JSON. Also drop a dangling
    # unterminated <think> with no closing tag (a truncated reasoning block).
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if "<think>" in text and "</think>" not in text:
        text = text.split("<think>", 1)[0].strip()
    # Strip code fences if the model added them despite instructions.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Fast path.
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Scan for a balanced { ... } or [ ... ] block, string-aware so braces
    # inside lyric/summary strings don't throw off the depth count. On a failed
    # parse, keep scanning for the next candidate rather than giving up.
    for opener, closer in (("{", "}"), ("[", "]")):
        i = 0
        while True:
            start = text.find(opener, i)
            if start == -1:
                break
            depth = 0
            in_str = False
            escape = False
            end = None
            for j in range(start, len(text)):
                ch = text[j]
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
            if end is not None:
                try:
                    return json.loads(text[start : end + 1])
                except ValueError:
                    pass
            i = start + 1
    raise LLMError("Could not parse JSON from model output:\n" + (text[:600] or "<empty>"))
