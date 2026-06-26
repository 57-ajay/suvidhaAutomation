"""Selectors and DOM probes for the PUC portal.

The PUC form's *named* ids are stable (regnNumber, chessiNo [sic], txtCaptchaId,
showDetails). The buttons on the results / Form-59 pages carry JSF auto-ids
(j_idt161, j_idt9, ...) which shift if the page changes — those we click by
visible text instead, exactly like the border-tax runners do for "Next" / "Go".
"""

from __future__ import annotations

PORTAL_URL = "https://puc.parivahan.gov.in/puc/views/PucCertificate.xhtml"

# ── Step-1 form (PucCertificate.xhtml) ──────────────────────────────────────
SEL_REGN = "#regnNumber"  # Registration Number (maxlength 10, makeCaps)
SEL_CHASSIS = "#chessiNo"  # portal's id is misspelled; last 5, maxlength 5
SEL_CAPTCHA_INPUT = "#txtCaptchaId"
SEL_CAPTCHA_IMG = "#imgCaptchaId"  # <img>, not a canvas -> capture_element_png

# Refresh control: the <a> whose onclick re-points #imgCaptchaId.src. Targeting
# it by the onclick attribute is id-independent (the wrapped <img id=j_idt95> is
# a shifting JSF id). Clicking it triggers the portal's own refresh+postback.
SEL_CAPTCHA_REFRESH = 'a[onclick*="imgCaptchaId"]'

SEL_SUBMIT = "#showDetails"  # "PUC Details" — stable named button

# Fallback click for #showDetails (named id). flow.py normally clicks via the
# shared `click` step (same primitive the border-tax runners use to post their
# forms); this is only used if that element click can't be resolved.
CLICK_SUBMIT_JS = (
    "(function(){var e=document.querySelector('#showDetails');"
    "if(e){e.click();return true;}return false;})()"
)

# True once reg + chassis both hold non-empty values. submit_action checks this
# so it only (re)fills them after a postback actually wiped them — refilling
# when they're already present fires a change event that retriggers the portal's
# field postback, which refreshes the just-answered captcha.
FIELDS_FILLED_JS = (
    "(function(){var r=document.querySelector('#regnNumber');"
    "var c=document.querySelector('#chessiNo');"
    "return !!(r&&(r.value||'').trim()&&c&&(c.value||'').trim());})()"
)

# ── Post-submit state probe ─────────────────────────────────────────────────
# Run after clicking PUC Details. The portal reloads (JSF postback) to one of:
#   results       -> the details + a Print button rendered (captcha was correct)
#   not_found     -> a "no record / not available" message (captcha was correct)
#   captcha_error -> an invalid-captcha message, form (incl. #txtCaptchaId) back
#   pending       -> nothing decisive yet (still loading) -> caller keeps polling
#
# results/not_found both mean the captcha was accepted; only captcha_error means
# "retry the captcha". A 'results' check therefore wins over a stale message.
SUBMIT_STATE_JS = r"""
(function () {
  function txt(el){ return (el && (el.innerText||el.textContent) || ''); }
  var body = (document.body && (document.body.innerText||'')).toLowerCase();

  // Print button (results page) or the certificate's own marker.
  var btns = Array.prototype.slice.call(
      document.querySelectorAll('button, input[type=submit], input[type=button]'));
  var hasPrint = btns.some(function (b) {
      return /print/i.test(b.textContent || b.value || '');
  });
  if (hasPrint || body.indexOf('pollution under control') !== -1) {
      return { state: 'results' };
  }

  var msgEl = document.querySelector('#messages') ||
              document.querySelector('.ui-messages');
  var msg = txt(msgEl).trim();
  var ml = (msg || body).toLowerCase();

  if (ml.indexOf('captcha') !== -1 ||
      ml.indexOf('invalid security') !== -1 ||
      ml.indexOf('enter valid') !== -1 ||
      ml.indexOf('not match') !== -1) {
      return { state: 'captcha_error', msg: msg.substring(0, 200) };
  }

  if (ml.indexOf('not found') !== -1 ||
      ml.indexOf('no record') !== -1 ||
      ml.indexOf('not available') !== -1 ||
      ml.indexOf('no puc') !== -1 ||
      ml.indexOf('does not exist') !== -1 ||
      ml.indexOf('no data') !== -1) {
      return { state: 'not_found', msg: msg.substring(0, 200) };
  }

  return { state: 'pending', msg: msg.substring(0, 200) };
})()
"""

# Form-59 (pucPublicNew.xhtml) readiness: the certificate heading + the SL-No
# label are present once the printable certificate has rendered.
FORM59_READY_JS = r"""
(function () {
  var t = (document.body && (document.body.innerText||'')).toLowerCase();
  return t.indexOf('pollution under control certificate') !== -1 &&
         t.indexOf('certificate sl') !== -1;
})()
"""

# Best-effort field scrape off Form-59 for aiAgentData.receipt.fields. Walks the
# label rows and reads the value to the right of each known label. Never throws
# into the run — the caller wraps it and the PDF is the real artifact.
EXTRACT_FIELDS_JS = r"""
(function () {
  var out = {};
  var labels = Array.prototype.slice.call(document.querySelectorAll('label, span, div'));
  function valAfter(re) {
    for (var i = 0; i < labels.length; i++) {
      var s = (labels[i].innerText || labels[i].textContent || '').trim();
      if (re.test(s) && s.length < 40) {
        // The value sits in a sibling cell; scan forward for the next non-empty,
        // non-colon text node within the same row-ish neighbourhood.
        var row = labels[i].closest('.row, tr') || labels[i].parentElement;
        if (row) {
          var cells = Array.prototype.slice.call(row.querySelectorAll('label, span, div, td'));
          for (var j = 0; j < cells.length; j++) {
            var v = (cells[j].innerText || cells[j].textContent || '').trim();
            if (v && v !== ':' && !re.test(v) && v.length < 60) return v;
          }
        }
      }
    }
    return '';
  }
  // certificateSlNo and validityUpto are the certificate-specific values worth
  // capturing. The registration number is already known authoritatively from
  // the job params, so we don't scrape it (the label-adjacency walk grabbed the
  // wrong cell for it on Form-59 anyway).
  out.certificateSlNo = valAfter(/certificate\s*sl/i);
  out.validityUpto    = valAfter(/validity\s*upto/i);
  return out;
})()
"""
