"""Captcha / QR image capture.

The portal renders TWO canvases around the captcha: a hidden one with
id "captcha" (0x0 rect, holds its own drawing) and the VISIBLE one with no id
at all. Verified with a live DOM probe: only the visible canvas's pixels
match what the user sees, so selecting by id — or trusting the first match —
ships the wrong code. The probe therefore scans the WHOLE document, filters
to non-zero rects, and prefers the newest (last) visible canvas.

Equally important: right after a wizard section mounts, the visible canvas
exists but hasn't PAINTED yet. Treating that instant as "blank" and clicking
refresh regenerates the code while the user may already be looking at the old
image — the exact desync seen in production (app showed one code, portal
validated another). wait_and_capture_canvas_png() therefore POLLS until the
visible canvas has ink, and never clicks anything. Refresh decisions belong
to the caller, only after the paint-wait is exhausted.

Capture order: toDataURL (exact pixels) -> CDP clip screenshot of the element
rect (survives tainted canvases).
"""

from __future__ import annotations

import asyncio
import time

from .steps import cdp_eval

# Fewer than this many non-background pixels = the canvas hasn't painted.
_MIN_INK_PX = 5

_PROBE_JS = (
    """
(function(){
  var canvases = Array.prototype.slice.call(document.querySelectorAll('canvas'));
  if(!canvases.length) return {state:'no_canvas'};
  function rect(c){ var r=c.getBoundingClientRect();
                    return {x:r.x, y:r.y, w:r.width, h:r.height}; }
  var visible = canvases.filter(function(c){
    var r=c.getBoundingClientRect(); return r.width>0 && r.height>0; });
  if(!visible.length){
    return {state:'hidden_only', count:canvases.length};
  }
  var pick = visible[visible.length-1];
  try { pick.scrollIntoView({block:'center'}); } catch(e){}
  var out = {rect:rect(pick), count:canvases.length,
             visibleCount:visible.length, bw:pick.width, bh:pick.height};
  var ink = null;
  try {
    var ctx = pick.getContext('2d');
    if(ctx && pick.width>0 && pick.height>0){
      var d = ctx.getImageData(0, 0, pick.width, pick.height).data;
      ink = 0;
      for(var i=0;i<d.length;i+=4){
        if(d[i]<230 || d[i+1]<230 || d[i+2]<230) ink++;
      }
    }
  } catch(e){ out.pixelCheckError = String(e); }
  out.ink = ink;
  if(ink !== null && ink < %d){
    out.state = 'not_painted';
    return out;
  }
  out.state = 'painted';
  try { out.dataUrl = pick.toDataURL('image/png'); }
  catch(e){ out.dataUrlError = String(e); }
  return out;
})()
"""
    % _MIN_INK_PX
)


async def _clip_screenshot(session, rect: dict) -> str | None:
    try:
        cdp = await session.get_or_create_cdp_session()
        resp = await cdp.cdp_client.send.Page.captureScreenshot(
            params={
                "format": "png",
                "clip": {
                    "x": rect["x"],
                    "y": rect["y"],
                    "width": rect["w"],
                    "height": rect["h"],
                    "scale": 2,
                },
            },
            session_id=cdp.session_id,
        )
        data = (
            resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        )
        return data or None
    except Exception as e:
        print(f"[canvas] clip screenshot failed: {type(e).__name__}: {e}")
        return None


def _b64_from_probe(res: dict) -> str | None:
    data_url = res.get("dataUrl") or ""
    if "," in data_url:
        b64 = data_url.split(",", 1)[1]
        if len(b64) > 100:
            return b64
    return None


async def wait_and_capture_canvas_png(
    session,
    *,
    timeout: float = 6.0,
    tick: float = 0.6,
) -> tuple[str | None, str]:
    """Poll until the VISIBLE canvas has painted, then capture it.

    Returns (base64_png | None, detail). On None, detail is the last probe
    state: 'not_painted' (paint never landed — caller may refresh ONCE and
    call this again), 'hidden_only', 'no_canvas', or a capture error.
    NEVER clicks refresh or anything else.
    """
    deadline = time.monotonic() + timeout
    last_state = "no_probe"
    while True:
        res = await cdp_eval(session, _PROBE_JS) or {}
        state = res.get("state", "probe_failed")
        last_state = state
        if state == "painted":
            b64 = _b64_from_probe(res)
            if b64:
                return b64, f"toDataURL ink={res.get('ink')}"
            rect = res.get("rect") or {}
            if rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
                b64 = await _clip_screenshot(session, rect)
                if b64:
                    return b64, f"cdp_clip ink={res.get('ink')}"
            return None, f"capture_failed ({res.get('dataUrlError', 'no pixels')})"
        if time.monotonic() > deadline:
            return None, last_state
        await asyncio.sleep(tick)


async def capture_canvas_png(
    session,
    *,
    container_selector: str | None = None,
) -> tuple[str | None, str]:
    """One-shot capture (no paint-wait). Kept for callers that manage their
    own polling; prefer wait_and_capture_canvas_png for captcha flows.
    container_selector is accepted for compatibility and ignored — the probe
    always scans the whole document (the hidden #captcha canvas trap)."""
    res = await cdp_eval(session, _PROBE_JS) or {}
    state = res.get("state", "probe_failed")
    if state != "painted":
        return None, "blank" if state == "not_painted" else state
    b64 = _b64_from_probe(res)
    if b64:
        return b64, f"toDataURL ink={res.get('ink')}"
    rect = res.get("rect") or {}
    if rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
        b64 = await _clip_screenshot(session, rect)
        if b64:
            return b64, f"cdp_clip ink={res.get('ink')}"
    return None, f"capture_failed ({res.get('dataUrlError', 'no pixels')})"


async def capture_element_png(session, selector: str) -> str | None:
    """Clip-screenshot the LAST VISIBLE element matching the selector
    (used for the SBIePay QR <img>)."""
    import json as _json

    expr = (
        "(function(s){"
        "var els = Array.prototype.slice.call(document.querySelectorAll(s))"
        "  .filter(function(e){var r=e.getBoundingClientRect();"
        "          return r.width>0 && r.height>0;});"
        "if(!els.length) return null;"
        "var e = els[els.length-1];"
        "try{ e.scrollIntoView({block:'center'}); }catch(err){}"
        "var r=e.getBoundingClientRect();"
        "return {x:r.x, y:r.y, w:r.width, h:r.height};"
        "})(" + _json.dumps(selector) + ")"
    )
    rect = await cdp_eval(session, expr)
    if not rect or rect.get("w", 0) <= 0 or rect.get("h", 0) <= 0:
        return None
    return await _clip_screenshot(session, rect)
