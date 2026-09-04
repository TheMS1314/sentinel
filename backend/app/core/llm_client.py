"""
Sentinel LLM Client
====================
A thin, provider-agnostic wrapper so the rest of the codebase never needs
to know whether we're calling Anthropic directly or going through
OpenRouter. Controlled entirely by env vars:

    LLM_PROVIDER=anthropic | openrouter
    ANTHROPIC_API_KEY=...          (if provider=anthropic)
    OPENROUTER_API_KEY=...         (if provider=openrouter)
    LLM_MODEL=...                  (e.g. "claude-sonnet-4-6" for anthropic,
                                     "anthropic/claude-sonnet-4.5" for openrouter)

Falls back to a deterministic MOCK MODE if no key is configured for the
selected provider, so the rest of the system keeps running end-to-end
during development. Mock responses are clearly labeled and NEVER silently
mistaken for real model output.
"""

from __future__ import annotations
import os
import json
import logging

logger = logging.getLogger("sentinel.llm_client")


class LLMClient:
    def __init__(self):
        self.provider = os.environ.get("LLM_PROVIDER", "openrouter").lower()
        self.model = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4.5")
        self.mock_mode = False
        self._client = None

        if self.provider == "anthropic":
            key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not key:
                self.mock_mode = True
            else:
                import anthropic
                self._client = anthropic.Anthropic(api_key=key)

        elif self.provider == "openrouter":
            key = os.environ.get("OPENROUTER_API_KEY", "")
            if not key:
                self.mock_mode = True
            else:
                # OpenRouter is OpenAI-API-compatible
                from openai import OpenAI
                self._client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=key,
                )
        else:
            raise ValueError(f"Unknown LLM_PROVIDER: {self.provider}")

        if self.mock_mode:
            logger.warning(
                f"LLMClient running in MOCK MODE (provider={self.provider}, "
                f"no API key configured). Responses are canned, not real model output."
            )

    def complete_json(self, system_prompt: str, user_prompt: str, mock_response: dict) -> dict:
        """
        Calls the LLM asking for a JSON object back.

        Live provider failures and unusable responses are retried once.
        If all attempts fail, returns a safe error so callers can fail closed
        to HOLD rather than crashing or approving without a valid decision.
        """
        if self.mock_mode:
            logger.info("[MOCK LLM] Returning canned response instead of a real API call.")
            return mock_response

        max_attempts = 2

        for attempt in range(1, max_attempts + 1):
            try:
                if self.provider == "anthropic":
                    resp = self._client.messages.create(
                        model=self.model,
                        max_tokens=1024,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                    )

                    if not resp or not getattr(resp, "content", None):
                        raise RuntimeError("LLM response contained no content")

                    text = "".join(
                        block.text
                        for block in resp.content
                        if hasattr(block, "text")
                    )

                else:  # openrouter
                    resp = self._client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": "sentinel_decision",
                                "strict": True,
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "verdict": {
                                            "type": "string",
                                            "enum": ["APPROVE", "HOLD", "BLOCK"],
                                        },
                                        "reasoning": {
                                            "type": "string",
                                        },
                                        "confidence": {
                                            "type": "number",
                                        },
                                    },
                                    "required": [
                                        "verdict",
                                        "reasoning",
                                        "confidence",
                                    ],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        max_tokens=1024,
                    )

                    choices = getattr(resp, "choices", None)
                    if not choices:
                        raise RuntimeError("LLM response contained no choices")

                    message = getattr(choices[0], "message", None)
                    if message is None:
                        raise RuntimeError("LLM response contained no message")

                    text = getattr(message, "content", None)

                    if not text:
                        raise RuntimeError("LLM response contained no usable content")

                    finish_reason = getattr(choices[0], "finish_reason", None)
                    if finish_reason == "length":
                        raise RuntimeError("LLM response was truncated (finish_reason=length)")

                result = self._safe_parse_json(text)

                if "error" not in result and "verdict" in result:
                    return result

                raise RuntimeError("LLM response contained invalid JSON")

            except Exception as e:
                if attempt < max_attempts:
                    logger.warning(
                        f"Live LLM call failed ({self.provider}), "
                        f"attempt {attempt}/{max_attempts}: {e}. Retrying."
                    )
                    continue

                logger.error(
                    f"Live LLM call failed ({self.provider}) after "
                    f"{max_attempts} attempts: {e}"
                )
                return {
                    "error": "llm_call_failed",
                    "detail": str(e)[:300],
                }

        return {"error": "llm_call_failed", "detail": "unknown LLM failure"}

    @staticmethod
    def _safe_parse_json(text: str | None) -> dict:
        if not text or not isinstance(text, str):
            logger.error("LLM response contained no usable text")
            return {"error": "invalid_json", "raw": ""}

        text = text.strip()

        # First try the response exactly as returned.
        try:
            result = json.loads(text)
            if isinstance(result, dict) and "verdict" in result:
                return result
        except json.JSONDecodeError:
            pass

        # Handle Markdown code fences such as ```json ... ```
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

            try:
                result = json.loads(text)
                if isinstance(result, dict) and "verdict" in result:
                    return result
            except json.JSONDecodeError:
                pass

        # Some models prepend reasoning/explanation before the JSON object.
        # Extract the outermost JSON object and try parsing only that.
        start = text.find("{")
        end = text.rfind("}")

        if start != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                result = json.loads(candidate)
                if isinstance(result, dict) and "verdict" in result:
                    return result
            except json.JSONDecodeError:
                pass

        logger.error(f"LLM response was not valid JSON: {text[:300]}")
        return {"error": "invalid_json", "raw": text[:500]}