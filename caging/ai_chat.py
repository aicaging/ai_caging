"""AI Chat module for Caging — pure AI chat API handling.

Separated from webfacade.py so that ``/chat`` endpoint only deals with
regular AI API calls.  Built-in commands (``?help``, ``>approve``, etc.)
are handled purely client-side in ``chat.js``.
"""

import logging

logger = logging.getLogger(__name__)


class AIChat:
    """Handles AI chat conversations using the configured AI provider.

    Thin wrapper over ``AIScreener.chat_conversation()`` — builds the
    system prompt and message list, delegates to the provider, and
    returns the response.
    """

    def __init__(self, ai_screener):
        """*ai_screener* is an ``AIScreener`` instance (from ``ai_screener.py``)."""
        self._ai = ai_screener

    # ── Public API ─────────────────────────────────────────────────

    def chat(
        self,
        message: str,
        chat_history: list,
        context: str = "",
    ) -> str:
        """Send a plain-text chat message to the AI and return the response.

        Args:
            message: The user's message (no ``?`` / ``>`` command prefix).
            chat_history: Recent messages as ``[{"role":"...", "content":"..."}]``.
            context: Optional string with active-request context (JSON or prose).

        Returns:
            AI response string.
        """
        messages = self._build_messages(message, chat_history, context)
        try:
            return self._ai.chat_conversation(messages)
        except Exception as e:
            logger.error("AI chat error: %s", e)
            return f"AI chat error: {e}"

    # ── Internal ───────────────────────────────────────────────────

    def _build_messages(
        self, message: str, chat_history: list, context: str
    ) -> list:
        """Assemble the full message list to send to the AI provider."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Caging Assistant, an AI security operations helper. "
                    "You help users review and manage operation requests in the Caging "
                    "authorization system. Be concise, helpful, and security-focused.\n\n"
                    "When a user asks about a specific request, explain the risk level, "
                    "the command being executed, and recommend whether to approve or deny. "
                    "Be cautious — when in doubt, recommend human review."
                ),
            }
        ]

        # Inject active-request context if provided
        if context:
            messages.append({
                "role": "system",
                "content": f"Current active request context:\n{context}",
            })

        # Append recent chat history (last 20 entries)
        for m in chat_history[-20:]:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role in ("user", "assistant"):
                messages.append({"role": role, "content": content})

        # Append the current user message
        messages.append({"role": "user", "content": message})

        return messages
