// Internal ops console at /api/dashboard. Reads /api/jobs every 3s; lets an
// operator answer captchas/OTPs, view the QR, open the live browser, and
// cancel a job. Plain HTML in a string so the API stays a single Bun process.

export const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Border Tax — Agent Console</title>
<style>
  :root {
    --bg:#0b0e11; --panel:#12161b; --line:#1f262e; --text:#d7dde4;
    --dim:#76808a; --accent:#e8a33d; --ok:#4ade80; --bad:#f87171; --wait:#60a5fa;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
  header { display:flex; justify-content:space-between; align-items:baseline;
           padding:18px 24px; border-bottom:1px solid var(--line); }
  header h1 { font-size:15px; margin:0; letter-spacing:.08em; text-transform:uppercase; }
  header .sub { color:var(--dim); font-size:12px; }
  #grid { padding:20px 24px; display:grid; gap:14px;
          grid-template-columns:repeat(auto-fill, minmax(420px, 1fr)); }
  .job { background:var(--panel); border:1px solid var(--line); border-radius:8px;
         padding:14px 16px; }
  .row { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .id { font-weight:700; word-break:break-all; }
  .meta { color:var(--dim); font-size:12px; margin-top:2px; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--line);
           white-space:nowrap; }
  .b-running { color:var(--accent); border-color:var(--accent); }
  .b-waiting_for_human { color:var(--wait); border-color:var(--wait); }
  .b-done { color:var(--ok); border-color:var(--ok); }
  .b-queued { color:var(--dim); }
  .b-failed, .b-cancelled, .b-partial { color:var(--bad); border-color:var(--bad); }
  .agent { margin-top:8px; font-size:12px; color:var(--dim); }
  .agent b { color:var(--text); font-weight:600; }
  .wait { margin-top:10px; padding:10px 12px; border:1px solid var(--wait);
          border-radius:6px; font-size:13px; color:var(--wait); }
  .imgs { display:flex; gap:12px; margin-top:10px; flex-wrap:wrap; }
  .imgs figure { margin:0; }
  .imgs img { background:#fff; border-radius:6px; display:block; }
  .imgs .cap { width:200px; image-rendering:pixelated; }
  .imgs .qr { width:160px; height:160px; }
  .imgs figcaption { font-size:11px; color:var(--dim); margin-top:4px; }
  .controls { display:flex; gap:8px; margin-top:10px; }
  .controls input { flex:1; background:#0b0e11; border:1px solid var(--line);
                    color:var(--text); border-radius:6px; padding:8px 10px; font:inherit; }
  .controls button, .links a {
    background:var(--accent); color:#111; border:none; border-radius:6px;
    padding:8px 14px; font:inherit; font-weight:700; cursor:pointer; text-decoration:none; }
  .controls button:disabled { opacity:.45; cursor:default; }
  .links { display:flex; gap:8px; margin-top:10px; }
  .links a.ghost, .links button.ghost {
    background:transparent; color:var(--dim); border:1px solid var(--line);
    font-weight:400; cursor:pointer; padding:8px 14px; border-radius:6px; font:inherit; }
  .note { font-size:12px; margin-top:8px; }
  .note.ok { color:var(--ok); } .note.bad { color:var(--bad); }
  .result { margin-top:8px; font-size:12px; color:var(--dim);
            max-height:60px; overflow:hidden; }
  .empty { grid-column:1/-1; color:var(--dim); padding:60px 0; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>Border Tax — Agent Console</h1>
  <span class="sub">refreshes every 3s</span>
</header>
<div id="grid"><div class="empty">Loading…</div></div>

<script>
const sent = new Set();

function esc(s){ return (s ?? '').toString()
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;'); }

function ago(iso){
  if(!iso) return '';
  const m = Math.floor((Date.now() - new Date(iso).getTime())/60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + 'm ago';
  return Math.floor(m/60) + 'h ' + (m%60) + 'm ago';
}

async function refresh(){
  let data;
  try { data = await (await fetch('/api/jobs?limit=30')).json(); }
  catch { return; }
  const jobs = data.jobs || [];
  const grid = document.getElementById('grid');
  if (!jobs.length){
    grid.innerHTML = '<div class="empty">No jobs yet. POST /api/run to start one.</div>';
    return;
  }
  grid.innerHTML = jobs.map(render).join('');
}

function render(j){
  const waiting = j.status === 'waiting_for_human';
  let h = '<div class="job">';
  h += '<div class="row"><span class="id">' + esc(j.id) + '</span>'
     + '<span class="badge b-' + esc(j.status) + '">' + esc(j.status) + '</span></div>';
  h += '<div class="meta">' + esc(j.state || '') + ' · ' + esc(j.vehicleNumber || '')
     + ' · ' + ago(j.createdAt) + '</div>';
  if (j.agentStatus)
    h += '<div class="agent">lifecycle <b>' + esc(j.agentStatus) + '</b>'
       + (j.portalAmount ? ' · portal amount ₹<b>' + esc(j.portalAmount) + '</b>' : '')
       + '</div>';
  if (waiting && j.waitReason)
    h += '<div class="wait">' + esc(j.waitReason) + '</div>';

  if ((waiting && j.captchaUrl && (j.agentStatus === 'captchaSolving')) || (j.qrCodeUrl && j.agentStatus === 'qrPaymentNeeded')) {
    h += '<div class="imgs">';
    if (j.captchaUrl && j.agentStatus === 'captchaSolving')
      h += '<figure><img class="cap" src="' + esc(j.captchaUrl) + '">'
         + '<figcaption>captcha · attempt ' + esc(j.captchaAttempt || '1') + '</figcaption></figure>';
    if (j.qrCodeUrl && j.agentStatus === 'qrPaymentNeeded')
      h += '<figure><img class="qr" src="' + esc(j.qrCodeUrl) + '">'
         + '<figcaption>UPI QR — scan to pay</figcaption></figure>';
    h += '</div>';
  }

  if (waiting){
    h += '<div class="controls">'
       + '<input id="in-' + esc(j.id) + '" placeholder="captcha text / OTP / paid"'
       + ' onkeydown="if(event.key===\\'Enter\\')send(\\'' + esc(j.id) + '\\')">'
       + '<button id="btn-' + esc(j.id) + '" onclick="send(\\'' + esc(j.id) + '\\')">Send answer</button>'
       + '</div><div id="msg-' + esc(j.id) + '"></div>';
  }

  h += '<div class="links">';
  if (j.liveUrl) h += '<a class="ghost" href="' + esc(j.liveUrl) + '" target="_blank">Open live view</a>';
  if (!['done','failed','cancelled','partial'].includes(j.status))
    h += '<button class="ghost" onclick="cancelJob(\\'' + esc(j.id) + '\\')">Cancel job</button>';
  h += '</div>';

  if (j.result) h += '<div class="result">' + esc(j.result) + '</div>';
  if (j.error)  h += '<div class="note bad">' + esc(j.error) + '</div>';
  h += '</div>';
  return h;
}

async function send(id){
  const input = document.getElementById('in-' + id);
  const btn = document.getElementById('btn-' + id);
  const msg = document.getElementById('msg-' + id);
  const value = input ? input.value.trim() : '';
  if (!value){ if (input) input.focus(); return; }
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/jobs/' + id + '/intervene', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ input: value }),
    });
    const data = await res.json();
    if (res.ok){
      sent.add(id);
      if (msg) msg.innerHTML = '<div class="note ok">Sent — agent resumes shortly.</div>';
      if (input) input.value = '';
      setTimeout(refresh, 1500);
    } else {
      if (msg) msg.innerHTML = '<div class="note bad">' + esc(data.error || 'Failed') + '</div>';
      if (btn) btn.disabled = false;
    }
  } catch {
    if (msg) msg.innerHTML = '<div class="note bad">Network error — try again.</div>';
    if (btn) btn.disabled = false;
  }
}

async function cancelJob(id){
  if (!confirm('Cancel job ' + id + '?')) return;
  await fetch('/api/jobs/' + id + '/cancel', { method:'POST' });
  refresh();
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>`;
