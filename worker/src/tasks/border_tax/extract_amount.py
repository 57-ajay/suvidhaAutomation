"""Extract the calculated border-tax amount from the Tax Information table
and persist it to the Redis job hash as `borderTaxAmount`.

Ported from main. The CheckPost portal renders the same Tax/Fee table layout
on every state's Tax Information step: we locate the table by its HEADER TEXT
("Tax/Fee Particulars" + "Amount") — robust against ngcontent attribute churn
or class changes — and sum the LAST <td> of every tbody row.

Best-effort by design: any failure (table missing, parse error, Redis hiccup)
is logged as RETRIED and "0" is saved. The runner continues regardless — we
never want amount extraction to abort a run the user can still complete; the
real Grand Total is re-read from the receipt page after payment.
"""

from __future__ import annotations

import time

from engine.steps import cdp_eval
from engine.types import RunContext, StepLog, StepStatus
from redis_client import job_key

_EXTRACT_JS = r"""
(function() {
  var tables = document.querySelectorAll('table');
  var target = null;
  for (var i = 0; i < tables.length; i++) {
    var thead = tables[i].querySelector('thead');
    var headerText = ((thead && thead.innerText) || '').toLowerCase();
    if (headerText.indexOf('tax/fee particulars') >= 0 &&
        headerText.indexOf('amount') >= 0) {
      target = tables[i];
      break;
    }
  }
  if (!target) {
    return {ok: false, reason: 'tax_table_not_found', total: 0, rows: []};
  }

  var trs = target.querySelectorAll('tbody tr');
  if (trs.length === 0) {
    return {ok: false, reason: 'no_rows', total: 0, rows: []};
  }

  var total = 0;
  var rows = [];
  for (var r = 0; r < trs.length; r++) {
    var tds = trs[r].querySelectorAll('td');
    if (tds.length === 0) continue;
    var last = tds[tds.length - 1];
    var raw = (last.innerText || last.textContent || '').trim();
    var cleaned = raw.replace(/[^0-9.]/g, '');
    var n = parseFloat(cleaned);
    var label = tds.length >= 2
      ? (tds[1].innerText || tds[1].textContent || '').trim()
      : '';
    if (!isNaN(n)) {
      total += n;
      rows.push({label: label, amount: n, raw: raw});
    } else {
      rows.push({label: label, amount: null, raw: raw});
    }
  }

  return {ok: true, total: total, rows: rows, rowCount: trs.length};
})()
"""


async def extract_and_save_border_tax_amount(
    ctx: RunContext, *, name: str = "p5.extract_border_tax_amount",
) -> float:
    started = time.monotonic()
    amount = 0.0
    err: str | None = None
    detail = ""

    try:
        result = await cdp_eval(ctx.session, _EXTRACT_JS)
        if isinstance(result, dict) and result.get("ok"):
            try:
                amount = float(result.get("total") or 0)
            except (TypeError, ValueError):
                amount = 0.0
            rows = result.get("rows") or []
            row_summary = ", ".join(
                f"{(r.get('label') or '?')}={r.get('amount')!r}"
                for r in rows[:10]
            )
            detail = f"total={amount} rows={result.get('rowCount')} ({row_summary})"
        elif isinstance(result, dict):
            err = result.get("reason") or "unknown_extract_failure"
            detail = f"reason={err}"
        else:
            err = "cdp_eval_returned_non_dict"
            detail = f"raw={result!r}"
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        detail = f"exception: {err}"

    # Always persist SOMETHING so the client poll has a key to read;
    # "0" is the sentinel for "tried but couldn't read it".
    try:
        ctx.r.hset(job_key(ctx.job_id), "borderTaxAmount", str(amount))
    except Exception as e:
        err = err or f"redis: {type(e).__name__}: {e}"

    if amount > 0:
        ctx.scratch["portalAmount"] = amount
        try:
            ctx.r.hset(job_key(ctx.job_id), "portalAmount", str(amount))
        except Exception:
            pass

    ctx.log.record(StepLog(
        index=ctx.log.next_index(), name=name,
        status=StepStatus.OK if err is None else StepStatus.RETRIED,
        duration_ms=int((time.monotonic() - started) * 1000),
        value=detail, error=err,
    ))
    return amount
