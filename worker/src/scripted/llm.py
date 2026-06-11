# worker/src/scripted/llm.py
"""LLM used by AI rescue paths (pending-transaction captcha solve).

The happy path is fully scripted and uses NO LLM. The only AI in pivot-a UP is
the background pending-transaction clear, which reads a captcha visually. Model
config via env so it can be tuned without code changes.
"""

from __future__ import annotations

import os


def build_llm():
    """Return a browser-use chat model. Defaults to Gemini (ChatGoogle).

    GOOGLE_API_KEY (or the provider's standard env var) must be set for the
    pending-clear AI path. If you never enable AUTO_CLEAR_PENDING, this is unused.
    """
    from browser_use import ChatGoogle

    model = os.environ.get("AI_MODEL", "gemini-2.0-flash")
    return ChatGoogle(model=model)
