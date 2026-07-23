"""AI Screening module for Caging - pluggable interface for OpenAI/Gemini."""
import os
import json
from typing import Optional


class AIScreener:
    """Pluggable AI screening module. Supports OpenAI and Gemini providers."""

    def __init__(self, provider: str = "openai", api_key: str = "",
                 model: str = "gpt-4", fallback: str = "manual", base_url: str = ""):
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.fallback = fallback
        self.base_url = base_url

    def screen(self, request_payload: dict) -> dict:
        """Screen a request and return decision.
        Returns {"decision": "allow"|"deny"|"manual", "explanation": "...", "risk_score": int}
        """
        if not self.api_key:
            return self._fallback_result("AI API key not configured")

        try:
            if self.provider == "openai":
                return self._screen_openai(request_payload)
            elif self.provider == "gemini":
                return self._screen_gemini(request_payload)
            else:
                return self._fallback_result(f"Unknown provider: {self.provider}")
        except Exception as e:
            return self._fallback_result(f"AI screening error: {e}")

    def _fallback_result(self, reason: str) -> dict:
        return {"decision": "manual", "explanation": reason, "risk_score": 50}

    def _screen_openai(self, payload: dict) -> dict:
        """Screen via OpenAI API."""
        import openai
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = openai.OpenAI(**client_kwargs)

        prompt = self._build_prompt(payload)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a security screening AI. Analyze the following operation request and respond with a JSON object containing 'decision' (allow/deny/manual), 'explanation' (string), and 'risk_score' (0-100)."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        result = json.loads(response.choices[0].message.content)
        return {
            "decision": result.get("decision", "manual"),
            "explanation": result.get("explanation", ""),
            "risk_score": result.get("risk_score", 50),
        }

    def _screen_gemini(self, payload: dict) -> dict:
        """Screen via Google Gemini API."""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(self.model)
            prompt = self._build_prompt(payload)
            response = model.generate_content(
                f"You are a security screening AI. Analyze the following operation request and respond with a JSON object containing 'decision' (allow/deny/manual), 'explanation' (string), and 'risk_score' (0-100).\n\n{prompt}"
            )
            result = json.loads(response.text.strip().removeprefix("```json").removesuffix("```").strip())
            return {
                "decision": result.get("decision", "manual"),
                "explanation": result.get("explanation", ""),
                "risk_score": result.get("risk_score", 50),
            }
        except ImportError:
            return self._fallback_result("Gemini SDK not installed")

    def chat(self, message: str, chat_history: list, context: str = "") -> str:
        """General-purpose AI chat with conversation history and optional context.
        Returns the AI's text response."""
        if not self.api_key:
            return "AI not configured — please set an API key."

        try:
            messages = [{
                "role": "system",
                "content": (
                    "You are an AI assistant for Caging, an isolated environment manager. "
                    "Help the user understand and manage requests, policies, and system operations. "
                    "Be concise and helpful. When reviewing requests, focus on security implications."
                ),
            }]
            if context:
                messages.append({
                    "role": "system",
                    "content": f"Current request context:\n{context}",
                })
            for entry in chat_history[-20:]:  # last 20 messages max
                role = entry.get("role", "user")
                content = entry.get("content", "")
                if role in ("user", "assistant"):
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": message})

            if self.provider == "openai":
                return self._chat_openai(messages)
            elif self.provider == "gemini":
                return self._chat_gemini(messages)
            else:
                return f"Chat not supported for provider: {self.provider}"
        except Exception as e:
            return f"AI chat error: {e}"

    def _chat_openai(self, messages: list) -> str:
        """Chat via OpenAI-compatible API."""
        import openai
        client_kwargs = {"api_key": self.api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = openai.OpenAI(**client_kwargs)

        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.7,
        )
        return response.choices[0].message.content

    def _chat_gemini(self, messages: list) -> str:
        """Chat via Google Gemini API."""
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(self.model)
            # Convert messages to Gemini format
            history = []
            for m in messages[:-1]:
                role = "user" if m["role"] == "user" else "model"
                history.append({"role": role, "parts": [m["content"]]})
            chat = model.start_chat(history=history)
            resp = chat.send_message(messages[-1]["content"])
            return resp.text
        except ImportError:
            return "Gemini SDK not installed"

    def chat_conversation(self, messages: list) -> str:
        """Send a pre-built messages list to the AI and return the response.

        Unlike ``chat()``, this does NOT add a system prompt or append the
        last user message — *messages* is used as-is.  This is the preferred
        method for ai_chat.py, where the caller already assembled the full
        conversation including system prompt and history.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.

        Returns:
            AI response string.
        """
        if not self.api_key:
            return "AI not configured — please set an API key."
        try:
            if self.provider == "openai":
                return self._chat_openai(messages)
            elif self.provider == "gemini":
                return self._chat_gemini(messages)
            else:
                return f"Chat not supported for provider: {self.provider}"
        except Exception as e:
            return f"AI chat error: {e}"

    def _build_prompt(self, payload: dict) -> str:
        """Build a prompt from the request payload."""
        parts = []
        if "command" in payload and payload["command"]:
            parts.append(f"Command: {payload['command']}")
        if "script_source" in payload and payload["script_source"]:
            parts.append(f"Script source: {payload['script_source'][:500]}")
        if "path" in payload and payload["path"]:
            parts.append(f"Path: {payload['path']}")
        if "catalog" in payload and payload["catalog"]:
            parts.append(f"Category: {payload['catalog']}")
        if "reason" in payload and payload["reason"]:
            parts.append(f"Reason: {payload['reason']}")
        if "policy_context" in payload and payload["policy_context"]:
            parts.append(f"Context: {json.dumps(payload['policy_context'])}")
        return "\n".join(parts)
