"""vcourts.gov.in selectors for the challan-PAYMENT flow.

The search half is shared with challan-settlement (same index.php, same
main.php police pane, same securimage widget) and is imported from there so
the two tasks can never drift on the parts they provably share. Only the
selectors AFTER the results table — the ones settlement never touches because
it is read-only — are new here.

Every selector below marked (legacy-confirmed) was captured from the live DOM
in the previous implementation (scripted/challan/vcourts.py). engine.steps
resolves each to the LAST VISIBLE match, so pane-scoped ids stay correct even
though hidden sibling panes carry their own copies.
"""

from __future__ import annotations

from tasks.challan_settlement.selectors import (  # noqa: F401  (re-export)
    SEL_CAPTCHA_IMAGE,
    SEL_CAPTCHA_INPUT,
    SEL_CAPTCHA_REFRESH,
    SEL_DEPT_DROPDOWN,
    SEL_PANE,
    SEL_PROCEED,
    SEL_RESULTS_SPAN,
    SEL_SUBMIT,
    SEL_TAB_POLICE,
    VC_HOME,
)

# ── main.php, police pane ────────────────────────────────────────────────
# Settlement searches by VEHICLE number; payment searches by CHALLAN number,
# which pins the result to exactly one record and makes the "View" click
# unambiguous. Both inputs live in the same pane.
SEL_CHALLAN_INPUT = "input#challan_no"  # (legacy-confirmed)

# ── record detail (payment-only, legacy-confirmed) ───────────────────────
# The lone green "View" on a single-record result.
TXT_VIEW_BUTTON = "View"
# Radio that reveals the verification inputs (mobile + chassis).
SEL_REVEAL_FIELDS = "#incorrectsubmit"
SEL_OTP_MOBILE = "#otp_mobile_ce"
SEL_CHASSIS_LAST4 = "#fcha_no_add"

# ── receipt page ─────────────────────────────────────────────────────────
# The receipt renders with <button onclick="window.print();">Print</button>.
# Matching the onclick attribute is robust against class/whitespace churn and
# is the ONLY reliable "we are on a real receipt" signal on this portal.
RECEIPT_READY_JS = """
(function() {
  var btns = document.querySelectorAll('button[onclick]');
  for (var i = 0; i < btns.length; i++) {
    if ((btns[i].getAttribute('onclick') || '').indexOf('window.print') !== -1) {
      return { found: true };
    }
  }
  return { found: false };
})();
"""

# Portal-side "your money did not go through" surfaces. Same semantics as the
# scripted border-tax handover's negative patterns: seeing one ends the wait
# immediately as failed rather than burning the whole window.
NEGATIVE_PATTERNS = [
    r"transaction\s*status\s*[:\-]?\s*pending",
    r"transaction\s*status\s*[:\-]?\s*failed",
    r"transaction\s*failed",
    r"payment\s*failed",
    r"payment\s*was\s*not\s*successful",
]
