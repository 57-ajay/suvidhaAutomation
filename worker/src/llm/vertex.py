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

from config import LLM_MODEL,  VERTEX_PROJECT
# import config
import asyncio
# Conservative $/1M tokens for cost telemetry (agentCost in Firestore).
_IN_PER_M = 0.10
_OUT_PER_M = 0.40

OCR_INSTRUCTION = (
    "Transcribe the exact sequence of characters that appears in this image, "
    "reading left to right. Examine each character carefully and individually. "
    "Respond with only those characters as a single string — no spaces, no "
    "quotation marks, no explanation. The string may mix uppercase letters, "
    "lowercase letters, digits, and symbols; reproduce the exact case of every "
    "letter."
)

OCR_SYSTEM = (
    "You are an OCR engine that transcribes the characters visible in an "
    "image, returning them verbatim and nothing else."
)

def build_llm():
    """browser-use chat model against Vertex, for any future AI-driven step."""
    from browser_use import ChatGoogle

    return ChatGoogle(model=LLM_MODEL, vertexai=True, project=VERTEX_PROJECT)

async def ocr_image(
    image_b64: str, instruction: str = OCR_INSTRUCTION,
) -> tuple[str, float]:
    try:
        from google import genai
        from google.genai import types as gt

        img_bytes = base64.b64decode(image_b64)

        client = genai.Client(
            vertexai=True, project=VERTEX_PROJECT,
        )
        cfg = gt.GenerateContentConfig(
            temperature=0,
            max_output_tokens=4096,
            response_mime_type="text/plain",
            system_instruction=OCR_SYSTEM,
            safety_settings=[
                gt.SafetySetting(category=c, threshold=gt.HarmBlockThreshold.BLOCK_NONE)
                for c in (
                    gt.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    gt.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    gt.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    gt.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                )
            ],
        )
        try:
            cfg.thinking_config = gt.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass

        resp = await client.aio.models.generate_content(
            model=LLM_MODEL,
            contents=[
                gt.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                instruction,
            ],
            config=cfg,
        )

        usage = getattr(resp, "usage_metadata", None)
        cand = (getattr(resp, "candidates", None) or [None])[0]
        fr = getattr(cand, "finish_reason", None)

        raw = ""
        try:
            raw = resp.text or ""
        except Exception as e:
            print(f"[vertex] resp.text raised: {type(e).__name__}: {e}")

        cost = 0.0
        if usage:
            cost = (
                (usage.prompt_token_count or 0) * _IN_PER_M
                + (usage.candidates_token_count or 0) * _OUT_PER_M
            ) / 1_000_000

        text = raw.strip().strip("`'\"").replace(" ", "")

        # THE line you've been missing — shows up plainly in `docker compose logs`.
        print(
            f"[vertex] ocr model={LLM_MODEL} img={len(img_bytes)}B \n"
            f"raw={raw!r} -> text={text!r} finish={fr} \n"
            f"tok(in/out)="
            f"{getattr(usage, 'prompt_token_count', None)}/"
            f"{getattr(usage, 'candidates_token_count', None)}"
            f"\nraw response -> {raw}\n"
        )

        return (text or "UNREADABLE", cost)
    except Exception as e:
        print(f"[vertex] ocr_image failed: {type(e).__name__}: {e}")
        return ("UNREADABLE", 0.0)
#
# async def ocr_image(
#     image_b64: str, instruction: str = OCR_INSTRUCTION,
# ) -> tuple[str, float]:
#     """Returns (text, cost_usd). ("UNREADABLE", cost) when the model produces
#     no usable answer — callers refresh the captcha and retry."""
#     try:
#         from google import genai
#         from google.genai import types as gt
#
#         client = genai.Client(
#             vertexai=True,
#             project=VERTEX_PROJECT,
#         )
#
#         cfg = gt.GenerateContentConfig(
#             temperature=0,
#             max_output_tokens=256,  
#             response_mime_type="text/plain",
#             safety_settings=[
#                 gt.SafetySetting(category=c, threshold=gt.HarmBlockThreshold.BLOCK_NONE)
#                 for c in (
#                     gt.HarmCategory.HARM_CATEGORY_HARASSMENT,
#                     gt.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
#                     gt.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
#                     gt.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
#                 )
#             ],
#         )
#         # Turn OFF "thinking" so tokens go to the answer, not hidden reasoning.
#         # (No-op on models that don't support it.)
#         try:
#             cfg.thinking_config = gt.ThinkingConfig(thinking_budget=0)
#         except Exception:
#             pass
#
#         resp = await client.aio.models.generate_content(
#             model=LLM_MODEL,
#             contents=[
#                 gt.Part.from_bytes(
#                     data=base64.b64decode(image_b64), mime_type="image/png",
#                 ),
#                 instruction,
#             ],
#             config=cfg,
#         )
#
#         cost = 0.0
#         usage = getattr(resp, "usage_metadata", None)
#         if usage:
#             cost = (
#                 (usage.prompt_token_count or 0) * _IN_PER_M
#                 + (usage.candidates_token_count or 0) * _OUT_PER_M
#             ) / 1_000_000
#
#         text = ""
#         try:
#             text = (resp.text or "").strip()
#         except Exception:
#             text = ""
#
#         if not text:
#             # Tell us WHY it was empty: MAX_TOKENS (thinking) vs SAFETY vs other.
#             cand = (getattr(resp, "candidates", None) or [None])[0]
#             fr = getattr(cand, "finish_reason", None)
#             print(f"[vertex] ocr empty (finish_reason={fr}, usage={usage})")
#             return ("UNREADABLE", cost)
#
#         # Strip anything the model wraps around the code.
#         text = text.strip().strip("`'\"").replace(" ", "")
#         return (text or "UNREADABLE", cost)
#     except Exception as e:
#         print(f"[vertex] ocr_image failed: {type(e).__name__}: {e}")
#         return ("UNREADABLE", 0.0)

