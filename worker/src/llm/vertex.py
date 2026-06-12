"""Vertex AI — the only LLM path in this worker.

Auth is Application Default Credentials from the GCE metadata server: the VM's
service account needs roles/aiplatform.user on the project. No key file is
mounted into this container (the API holds the only key, for Firestore/GCS).

ocr_image() is a direct google-genai call (no agent loop): one image + one
instruction at temperature 0. Used by the pending-transaction auto-clear,
where the captcha is background recovery work the user never sees.
"""

from __future__ import annotations

import base64

from config import LLM_MODEL, VERTEX_LOCATION, VERTEX_PROJECT

# Conservative $/1M tokens for cost telemetry (agentCost in Firestore).
_IN_PER_M = 0.10
_OUT_PER_M = 0.40

OCR_INSTRUCTION = (
    "Read the captcha characters in this image. Respond with ONLY the "
    "characters — no spaces, no punctuation, no explanation. Preserve "
    "upper/lower case exactly as shown."
)


def build_llm():
    """browser-use chat model against Vertex, for any future AI-driven step."""
    from browser_use import ChatGoogle

    return ChatGoogle(model=LLM_MODEL, vertexai=True, project=VERTEX_PROJECT)


async def ocr_image(
    image_b64: str, instruction: str = OCR_INSTRUCTION,
) -> tuple[str, float]:
    """Returns (text, cost_usd). ("UNREADABLE", cost) when the model can't
    produce an answer — callers refresh the captcha and retry."""
    try:
        from google import genai
        from google.genai import types as gt

        client = genai.Client(
            vertexai=True, project=VERTEX_PROJECT, location=VERTEX_LOCATION,
        )
        resp = await client.aio.models.generate_content(
            model=LLM_MODEL,
            contents=[
                gt.Part.from_bytes(
                    data=base64.b64decode(image_b64), mime_type="image/png",
                ),
                instruction,
            ],
            config=gt.GenerateContentConfig(temperature=0, max_output_tokens=50),
        )

        cost = 0.0
        usage = getattr(resp, "usage_metadata", None)
        if usage:
            cost = (
                (usage.prompt_token_count or 0) * _IN_PER_M
                + (usage.candidates_token_count or 0) * _OUT_PER_M
            ) / 1_000_000

        text = (resp.text or "").strip().replace(" ", "")
        return (text, cost) if text else ("UNREADABLE", cost)
    except Exception as e:
        print(f"[vertex] ocr_image failed: {type(e).__name__}: {e}")
        return ("UNREADABLE", 0.0)
