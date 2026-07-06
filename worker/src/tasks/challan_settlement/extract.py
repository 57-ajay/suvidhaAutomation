"""Virtual Courts results extraction for challan-settlement quotations.

Ports the battle-tested DOM extractor from the legacy scripted flow
(scripted/challan_settlement/extract.py) and replaces the discount/pricing
rules with the new quotation contract:

    quotation amount = the record's "Proposed Fine" (the court settlement)

Per-record fields read off VC_RESULTS:
    challanId   <- "Challan No. :" in the yellow header bar (fallback: the 3rd
                   argument of the View button's onclick="view(...)")
    offenceText <- Offence column of the nested off_tbl (logging/summary only)
    fineNumber  <- rightmost Fine column (logging only; NOT used as a gate)
    proposed    <- number in the "Proposed Fine" row
    statusText  <- full record text, used for the skip rules

Skip rules (ignore = write nothing for that record):
    Paid / Transferred to Regular Court / Disposed / Warrant / "Proceedings of
    the Challan is yet to be completed" — plus any record whose Proposed Fine
    is missing or unreadable ("no settlement amount").

Two layers so the DOM-fragile part stays isolated and the rules stay
unit-testable without a browser:
    extract_raw_records(session)  -> raw dicts off the live page
    build_quotation_records(raws) -> (valid, dropped)
"""

from __future__ import annotations

import re

RESULTS_MARKER = "No. of Records"
# Popup / message shown when the searched number has nothing on this court.
NOT_FOUND_MARKERS = ("does not exist", "no record found", "no records found")
INVALID_CAPTCHA_MARKERS = ("invalid captcha", "wrong captcha", "captcha mismatch")


def _to_int(s) -> int | None:
    if s is None:
        return None
    digits = re.sub(r"[^\d]", "", str(s))
    return int(digits) if digits else None


# ── per-record skip rules ────────────────────────────────────────────────
# Matched against the record's FULL status text (header bar + detail rows) so a
# badge span anywhere in the record is caught. Checked BEFORE any amount is
# read — a closed challan never gets a quotation.
_SKIP_STATUS_PHRASES: list[tuple[str, str]] = [
    ("transferred to regular court", "skip_transferred_regular_court"),
    ("regular court", "skip_transferred_regular_court"),
    ("yet to be completed", "skip_proceedings_pending"),
    ("proceedings of the challan", "skip_proceedings_pending"),
    ("case disposed", "skip_disposed"),
    ("disposed", "skip_disposed"),
    ("warrant", "skip_warrant"),
]
_PAID_RE = re.compile(r"\bpaid\b", re.IGNORECASE)  # word boundary: not "unpaid"


def _status_skip_reason(status_text: str | None) -> str | None:
    if not status_text:
        return None
    s = status_text.lower()
    for needle, reason in _SKIP_STATUS_PHRASES:
        if needle in s:
            return reason
    if _PAID_RE.search(status_text):
        return "skip_paid"
    return None


# ── DOM extractor ────────────────────────────────────────────────────────
# The main results table has class `servicestbl`; its own tbody rows come in
# PAIRS: a header-bar row (Sr.No | "…Challan No. : N…" | View button) followed
# by a detail-wrapper row whose td[colspan=3] holds a nested table.off_tbl with
# the offence row(s) [Offence Code|Offence|Act/Section|Fine] and a final
# "Proposed Fine" row. `:scope > tbody > tr` restricts to the MAIN table's own
# rows so nested off_tbl rows aren't mistaken for records. Confirmed against
# the live markup (Delhi Notice, 8-record and paid-record samples).
_EXTRACT_JS = r"""
(function(){
  var out = [];
  var main = document.querySelector('table.servicestbl');
  if(!main) return out;
  var rows = main.querySelectorAll(':scope > tbody > tr');
  var current = null;
  for(var i=0;i<rows.length;i++){
    var row = rows[i];
    var viewBtn = row.querySelector('button[onclick^="view("]');
    if(viewBtn){
      var txt = row.innerText || '';
      var challan = '';
      var m = txt.match(/Challan\s*No\.?\s*:?\s*([A-Za-z0-9\/\-]+)/i);
      if(m){ challan = m[1]; }
      if(!challan){
        var oc = viewBtn.getAttribute('onclick') || '';
        var mm = oc.match(/view\('[^']*','[^']*','([^']*)'/);
        if(mm){ challan = mm[1]; }
      }
      current = {challanId: challan, offenceText:'', fineNumber:null,
                 proposedFineNum:null, statusText: txt};
      out.push(current);
    } else if(current){
      // Detail-wrapper row for the current record. Append its text so a status
      // badge anywhere in the record is captured for the skip check.
      current.statusText += ' ' + (row.innerText || '');
      var detailTbl = row.querySelector('table.off_tbl');
      if(detailTbl){
        var body = detailTbl.querySelector('tbody') || detailTbl;
        var drows = body.querySelectorAll(':scope > tr');
        for(var j=0;j<drows.length;j++){
          var cells = drows[j].querySelectorAll('td');
          if(cells.length === 0) continue;
          var first = (cells[0].innerText || '').trim();
          var last = (cells[cells.length-1].innerText || '').trim();
          if(/proposed\s*fine/i.test(first)){
            current.proposedFineNum = last;
          } else if(cells.length >= 4){
            if(!current.offenceText){ current.offenceText = (cells[1].innerText || '').trim(); }
            current.fineNumber = last;  // rightmost Fine column
          }
        }
      }
    }
  }
  return out;
})()
"""


async def extract_raw_records(session) -> list[dict]:
    """Read the current VC_RESULTS page into raw dicts. Values may be None
    when unreadable — build_quotation_records drops those with a reason."""
    from engine.steps import cdp_eval

    raw = await cdp_eval(session, _EXTRACT_JS)
    if not raw:
        return []
    return [
        {
            "challanId": (rec.get("challanId") or "").strip(),
            "offenceText": (rec.get("offenceText") or "").strip(),
            "fineNumber": _to_int(rec.get("fineNumber")),
            "proposedFineNum": _to_int(rec.get("proposedFineNum")),
            "statusText": (rec.get("statusText") or "").strip(),
        }
        for rec in raw
    ]


# ── rules + dedup (pure, testable) ───────────────────────────────────────


def _to_quotation(raw: dict) -> tuple[dict | None, str | None]:
    challan_id = (raw.get("challanId") or "").strip()
    if not challan_id:
        return None, "empty_challanId"

    skip = _status_skip_reason(raw.get("statusText"))
    if skip:
        return None, skip

    proposed = raw.get("proposedFineNum")
    if proposed is None:
        return None, "proposed_fine_unreadable"  # "no settlement amount"
    amount = int(proposed)
    if amount < 0:
        return None, "negative_amount"

    return (
        {
            "challanId": challan_id,
            "amount": amount,  # quotation.amount == Proposed Fine
            "fine": raw.get("fineNumber"),  # logging only
            "offence": (raw.get("offenceText") or "")[:120],  # logging only
        },
        None,
    )


def build_quotation_records(raws: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply the rules, dedup by NORMALIZED challan id.
    Returns (valid, dropped) where dropped items are {challanId, reason}."""
    from .params import normalize_id

    valid: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for raw in raws:
        rec, reason = _to_quotation(raw)
        if reason:
            dropped.append({"challanId": raw.get("challanId") or "", "reason": reason})
            continue
        assert rec is not None
        key = normalize_id(rec["challanId"])
        if key in seen:
            dropped.append({"challanId": rec["challanId"], "reason": "duplicate"})
            continue
        seen.add(key)
        valid.append(rec)
    return valid, dropped
