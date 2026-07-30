// api/src/dashboard.ts
// Internal ops console at /api/dashboard. Two views:
//   • Jobs   — live job list (polls /api/jobs), filter by task + status, 50 per
//              page with prev/next, answer captchas/OTPs, view QR, open the live
//              browser, cancel a job.
//   • Run    — fire a PUC or border-tax job from a form instead of curl.
//
// Plain HTML/CSS/JS in one string so the API stays a single Bun process. The
// inner script deliberately avoids template literals and ${...} so it survives
// being embedded in this outer TS template literal; event delegation is used
// instead of inline on* handlers to keep quoting sane.
//
// NOTE: pagination + filtering rely on handleList accepting limit/offset/taskId/
// status (see routes/jobs.ts). If PUBLIC_API_KEY guards mutating routes, enter
// the key in the field on the Run tab — it is sent as X-Internal-Key on
// run/intervene/cancel.

export const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Console — CabsWale</title>
<style>
  :root {
    --bg:#0b0e11; --panel:#12161b; --panel2:#0f1318; --line:#1f262e; --text:#d7dde4;
    --dim:#76808a; --accent:#e8a33d; --ok:#4ade80; --bad:#f87171; --wait:#60a5fa;
    --puc:#a78bfa; --bt:#38bdf8; --cs:#34d399; --cp:#fb923c;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
  a { color:var(--accent); }
  header { display:flex; justify-content:space-between; align-items:center;
           padding:14px 24px; border-bottom:1px solid var(--line); gap:16px; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0; letter-spacing:.08em; text-transform:uppercase; }
  .tabs { display:flex; gap:6px; }
  .tab { background:transparent; color:var(--dim); border:1px solid var(--line);
         border-radius:6px; padding:7px 14px; font:inherit; cursor:pointer; }
  .tab.active { color:#111; background:var(--accent); border-color:var(--accent); font-weight:700; }

  .toolbar { display:flex; gap:14px; align-items:center; flex-wrap:wrap;
             padding:14px 24px; border-bottom:1px solid var(--line); }

  .stats { display:flex; gap:12px; align-items:stretch; padding:18px 24px 4px; flex-wrap:wrap; }
  .stat { background:var(--panel); border:1px solid var(--line); border-radius:8px;
          padding:10px 16px; min-width:88px; display:flex; flex-direction:column; gap:3px; }
  .stat b { font-size:22px; font-weight:700; line-height:1; }
  .stat span { font-size:10px; color:var(--dim); text-transform:uppercase; letter-spacing:.08em; }
  .stat.s-ok b { color:var(--ok); }
  .stat.s-bad b { color:var(--bad); }
  .stat.s-accent b { color:var(--accent); }
  .stat.s-wait b { color:var(--wait); }
  .stat.s-dim b { color:var(--dim); }
  .stat.s-total b { color:var(--text); }
  .vsep { width:1px; background:var(--line); margin:6px 2px; }
  .toolbar label { color:var(--dim); font-size:12px; display:flex; gap:6px; align-items:center; }
  select, input[type=text], input[type=date] {
    background:var(--panel2); border:1px solid var(--line); color:var(--text);
    border-radius:6px; padding:7px 9px; font:inherit; }
  .btn { background:var(--accent); color:#111; border:none; border-radius:6px;
         padding:7px 14px; font:inherit; font-weight:700; cursor:pointer; }
  .btn:disabled { opacity:.4; cursor:default; }
  .ghost { background:transparent; color:var(--dim); border:1px solid var(--line);
           border-radius:6px; padding:7px 12px; font:inherit; cursor:pointer; }
  .ghost:disabled { opacity:.4; cursor:default; }
  .count { color:var(--dim); font-size:12px; }
  .pager { margin-left:auto; display:flex; gap:8px; align-items:center; }
  .pager #pageinfo { color:var(--dim); font-size:12px; min-width:96px; text-align:center; }
  .updated { color:var(--dim); font-size:11px; }

  #grid { padding:20px 24px; display:grid; gap:14px;
          grid-template-columns:repeat(auto-fill, minmax(420px, 1fr)); }
  .job { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px; }
  .row { display:flex; justify-content:space-between; gap:10px; align-items:baseline; }
  .id { font-weight:700; word-break:break-all; }
  .meta { color:var(--dim); font-size:12px; margin-top:2px; }
  .tags { display:flex; gap:6px; align-items:center; flex-shrink:0; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--line); white-space:nowrap; }
  .b-running { color:var(--accent); border-color:var(--accent); }
  .b-waiting_for_human { color:var(--wait); border-color:var(--wait); }
  .b-done { color:var(--ok); border-color:var(--ok); }
  .b-queued, .b-parked { color:var(--dim); }
  .b-failed, .b-cancelled, .b-partial { color:var(--bad); border-color:var(--bad); }
  .task { font-size:10px; padding:2px 7px; border-radius:10px; text-transform:uppercase; letter-spacing:.05em; }
  .task-puc { color:var(--puc); border:1px solid var(--puc); }
  .task-bt { color:var(--bt); border:1px solid var(--bt); }
  .task-cs { color:var(--cs); border:1px solid var(--cs); }
  .task-cp { color:var(--cp); border:1px solid var(--cp); }
  .agent { margin-top:8px; font-size:12px; color:var(--dim); }
  .agent b { color:var(--text); font-weight:600; }
  .wait { margin-top:10px; padding:10px 12px; border:1px solid var(--wait); border-radius:6px; font-size:13px; color:var(--wait); }
  .imgs { display:flex; gap:12px; margin-top:10px; flex-wrap:wrap; }
  .imgs figure { margin:0; }
  .imgs img { background:#fff; border-radius:6px; display:block; }
  .imgs .cap { width:200px; image-rendering:pixelated; }
  .imgs .qr { width:160px; height:160px; }
  .imgs figcaption { font-size:11px; color:var(--dim); margin-top:4px; }
  .controls { display:flex; gap:8px; margin-top:10px; }
  .controls input { flex:1; }
  .links { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; }
  .links a, .links button { padding:7px 12px; border-radius:6px; font:inherit; text-decoration:none; }
  .links a { background:var(--accent); color:#111; font-weight:700; }
  .note { font-size:12px; margin-top:8px; }
  .note.ok { color:var(--ok); } .note.bad { color:var(--bad); }
  .result { margin-top:8px; font-size:12px; color:var(--dim); max-height:60px; overflow:hidden; word-break:break-word; }
  .empty { grid-column:1/-1; color:var(--dim); padding:60px 0; text-align:center; }

  /* run view */
  #view-run { padding:22px 24px; }
  .runtabs { display:flex; gap:6px; margin-bottom:18px; }
  .rt { background:transparent; color:var(--dim); border:1px solid var(--line); border-radius:6px;
        padding:7px 14px; font:inherit; cursor:pointer; }
  .rt.active { color:#111; font-weight:700; }
  .rt[data-rt=puc].active { background:var(--puc); border-color:var(--puc); }
  .rt[data-rt=border-tax].active { background:var(--bt); border-color:var(--bt); }
  .rt[data-rt=challan-settlement].active { background:var(--cs); border-color:var(--cs); }
  .rt[data-rt=challan-payment].active { background:var(--cp); border-color:var(--cp); }
  .runform { max-width:560px; background:var(--panel); border:1px solid var(--line);
             border-radius:8px; padding:18px 20px; }
  .field { margin-bottom:14px; }
  .field label { display:block; font-size:12px; color:var(--dim); margin-bottom:5px; }
  .field .with-btn { display:flex; gap:8px; }
  .field input, .field select { width:100%; }
  .hint { color:var(--dim); font-size:11px; margin-top:4px; }
  .formrow { display:flex; gap:14px; }
  .formrow .field { flex:1; }
  .apikey { max-width:560px; margin-bottom:14px; }
  #runresult { max-width:560px; margin-top:16px; }
  .resbox { border:1px solid var(--line); border-radius:8px; padding:14px 16px; background:var(--panel2); }
  .resbox.ok { border-color:var(--ok); }
  .resbox.bad { border-color:var(--bad); }
  .resbox h3 { margin:0 0 8px; font-size:13px; }
  .resbox pre { margin:0; white-space:pre-wrap; word-break:break-word; font-size:12px; color:var(--dim); }
  .hidden { display:none !important; }
</style>
</head>
<body>
<header>
  <h1>Agent Console</h1>
  <nav class="tabs">
    <button class="tab active" data-tab="jobs">Jobs</button>
    <button class="tab" data-tab="run">Run a task</button>
  </nav>
  <span class="updated" id="updated"></span>
</header>

<!-- ================= JOBS ================= -->
<section id="view-jobs">
  <div class="stats" id="stats">
    <div class="stat s-ok"><b id="s-done">0</b><span>done</span></div>
    <div class="stat s-bad"><b id="s-cancelled">0</b><span>cancelled</span></div>
    <div class="stat s-bad"><b id="s-failed">0</b><span>failed</span></div>
    <div class="vsep"></div>
    <div class="stat s-accent"><b id="s-running">0</b><span>running</span></div>
    <div class="stat s-wait"><b id="s-waiting">0</b><span>waiting</span></div>
    <div class="stat s-dim"><b id="s-queued">0</b><span>queued</span></div>
    <div class="stat s-total"><b id="s-total">0</b><span>total</span></div>
  </div>
  <div class="toolbar">
    <label>Task
      <select id="f-task">
        <option value="">All tasks</option>
        <option value="border-tax">border-tax</option>
        <option value="puc-certificate">puc-certificate</option>
        <option value="challan-settlement">challan-settlement</option>
        <option value="challan-payment">challan-payment</option>
      </select>
    </label>
    <label>Status
      <select id="f-status">
        <option value="">All</option>
        <option value="queued">queued</option>
        <option value="running">running</option>
        <option value="waiting_for_human">waiting_for_human</option>
        <option value="done">done</option>
        <option value="failed">failed</option>
        <option value="cancelled">cancelled</option>
        <option value="partial">partial</option>
        <option value="parked">parked</option>
      </select>
    </label>
    <label>Source
      <select id="f-source">
        <option value="">All</option>
        <option value="app">app</option>
        <option value="web">web</option>
      </select>
    </label>
    <label>Path
      <select id="f-path">
        <option value="">All</option>
        <option value="fullyAutomated">fullyAutomated</option>
        <option value="scripted">scripted</option>
      </select>
    </label>
    <button class="ghost" id="btn-refresh">Refresh</button>
    <label><input type="checkbox" id="auto" checked> auto</label>
    <span class="count" id="count"></span>
    <span class="pager">
      <button class="ghost" id="prev">‹ Prev</button>
      <span id="pageinfo"></span>
      <button class="ghost" id="next">Next ›</button>
    </span>
  </div>
  <div id="grid"><div class="empty">Loading…</div></div>
</section>

<!-- ================= RUN ================= -->
<section id="view-run" class="hidden">
  <div class="apikey field">
    <label>X-Internal-Key (only if this instance guards mutating routes; remembered locally)</label>
    <input type="text" id="apikey" placeholder="leave blank on a private deployment">
  </div>

  <div class="runtabs">
    <button class="rt active" data-rt="puc">PUC certificate</button>
    <button class="rt" data-rt="border-tax">Border tax</button>
    <button class="rt" data-rt="challan-settlement">Challan settlement</button>
    <button class="rt" data-rt="challan-payment">Challan payment</button>
  </div>

  <!-- PUC -->
  <form class="runform" id="form-puc">
    <div class="field">
      <label>requestId</label>
      <div class="with-btn">
        <input type="text" id="puc-requestId" placeholder="auto-generated">
        <button type="button" class="ghost" data-gen="puc-requestId" data-prefix="puc">Generate</button>
      </div>
      <div class="hint">use a fresh id per run — reusing a terminal request leaves its doc in a closed state</div>
    </div>
    <div class="field">
      <label>driverId</label>
      <input type="text" id="puc-driverId" value="test_driver_001">
    </div>
    <div class="formrow">
      <div class="field">
        <label>Registration number</label>
        <input type="text" id="puc-registrationNumber" placeholder="UK17TA0921">
      </div>
      <div class="field">
        <label>Chassis number (full or last 5)</label>
        <input type="text" id="puc-chassisNumber" placeholder="…187562">
      </div>
    </div>
    <div class="links" style="margin-top:0">
      <button type="submit" class="btn">Start PUC run</button>
      <button type="button" class="ghost" id="puc-sample">Fill sample</button>
    </div>
  </form>

  <!-- BORDER TAX -->
  <form class="runform hidden" id="form-border">
    <div class="formrow">
      <div class="field">
        <label>Source</label>
        <select id="bt-source">
          <option value="app">app (fullyAutomated)</option>
          <option value="web">web</option>
        </select>
      </div>
      <div class="field">
        <label>Path (web only)</label>
        <select id="bt-path" disabled>
          <option value="fullyAutomated">fullyAutomated</option>
          <option value="scripted">scripted</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>requestId</label>
      <div class="with-btn">
        <input type="text" id="bt-requestId" placeholder="auto-generated">
        <button type="button" class="ghost" data-gen="bt-requestId" data-prefix="bt">Generate</button>
      </div>
    </div>
    <div class="formrow">
      <div class="field">
        <label>driverId</label>
        <input type="text" id="bt-driverId" value="test_driver_001">
      </div>
      <div class="field">
        <label>State</label>
        <select id="bt-state"></select>
      </div>
    </div>
    <div class="formrow">
      <div class="field">
        <label>Vehicle number</label>
        <input type="text" id="bt-vehicleNumber" placeholder="UP25AB1234">
      </div>
      <div class="field">
        <label>Mobile number</label>
        <input type="text" id="bt-mobileNumber" placeholder="9xxxxxxxxx">
      </div>
    </div>
    <div class="formrow">
      <div class="field">
        <label>Tax mode</label>
        <select id="bt-taxMode">
          <option value="DAYS">DAYS</option>
          <option value="MONTHLY">MONTHLY</option>
          <option value="QUARTERLY">QUARTERLY</option>
          <option value="YEARLY">YEARLY</option>
        </select>
      </div>
      <div class="field">
        <label>Tax from</label>
        <input type="date" id="bt-taxFrom">
      </div>
      <div class="field" id="bt-taxUpto-wrap">
        <label>Tax upto (DAYS only)</label>
        <input type="date" id="bt-taxUpto">
      </div>
    </div>
    <div class="formrow">
      <div class="field">
        <label>Entry district</label>
        <input type="text" id="bt-entryDistrict" placeholder="as the portal lists it">
      </div>
      <div class="field">
        <label>Entry checkpoint (optional)</label>
        <input type="text" id="bt-entryCheckpoint">
      </div>
    </div>
    <div class="hint" style="margin-bottom:12px">The API validates per state and returns exactly what is missing or invalid, so partial fills are fine to probe.</div>
    <button type="submit" class="btn">Start border-tax run</button>
  </form>

  <!-- CHALLAN SETTLEMENT -->
  <form class="runform hidden" id="form-challan-settlement">
    <div class="formrow">
      <div class="field">
        <label>Source</label>
        <select id="cs-source">
          <option value="app">app</option>
          <option value="web">web</option>
        </select>
      </div>
      <div class="field">
        <label>driverId</label>
        <input type="text" id="cs-driverId" value="test_driver_001">
      </div>
    </div>
    <div class="field">
      <label>Vehicle number</label>
      <input type="text" id="cs-vehicleNumber" placeholder="HR38AE8912">
      <div class="hint">requestId and the Firestore doc are both the vehicle number — there is no separate id to generate</div>
    </div>
    <div class="field">
      <label>Challan numbers</label>
      <input type="text" id="cs-challanNumbers" placeholder="UP123455, 57693177, DL19016240430095546">
      <div class="hint">comma or space separated. One phase per Virtual Courts department is built from the prefixes; unmapped ones come back in the response</div>
    </div>
    <div class="links" style="margin-top:0">
      <button type="submit" class="btn">Start settlement scan</button>
      <button type="button" class="ghost" id="cs-sample">Fill sample</button>
    </div>
  </form>

  <!-- CHALLAN PAYMENT -->
  <form class="runform hidden" id="form-challan-payment">
    <div class="formrow">
      <div class="field">
        <label>Source</label>
        <select id="cp-source">
          <option value="web">web</option>
          <option value="app">app</option>
        </select>
      </div>
      <div class="field">
        <label>driverId</label>
        <input type="text" id="cp-driverId" value="test_driver_001">
      </div>
    </div>
    <div class="formrow">
      <div class="field">
        <label>Vehicle number</label>
        <input type="text" id="cp-vehicleNumber" placeholder="HR38AE8912">
      </div>
      <div class="field">
        <label>Challan number</label>
        <input type="text" id="cp-challanNo" placeholder="UP123455">
      </div>
    </div>
    <div class="formrow">
      <div class="field">
        <label>Mobile number (receives the OTP)</label>
        <input type="text" id="cp-phoneNo" placeholder="9xxxxxxxxx">
      </div>
      <div class="field">
        <label>Chassis number (full or last 4)</label>
        <input type="text" id="cp-chassisNo" placeholder="…1234">
      </div>
    </div>
    <div class="field">
      <label>Department (optional override)</label>
      <input type="text" id="cp-department" placeholder="derived from the challan prefix">
      <div class="hint">exact dropdown text, e.g. <b>Uttar Pradesh(Traffic Department)</b> — only needed for a prefix the table doesn't cover</div>
    </div>
    <div class="hint" style="margin-bottom:12px">
      One challan per run. requestId is the challan number; the job id is <b>cp-&lt;challan&gt;</b>.
      The run parks at <b>waiting_for_human</b> — open the live view and do Get OTP → OTP → pay.
      A challan that already has a receipt is refused with <b>409 already_paid</b>.
    </div>
    <div class="links" style="margin-top:0">
      <button type="submit" class="btn">Start challan payment</button>
      <button type="button" class="ghost" id="cp-sample">Fill sample</button>
    </div>
  </form>

  <div id="runresult"></div>
</section>

<script>
var KEYKEY = 'agentConsoleApiKey';
var apiKey = '';
try { apiKey = localStorage.getItem(KEYKEY) || ''; } catch (e) {}

var state = { offset:0, limit:50, taskId:'', status:'', source:'', path:'', total:0 };
var sent = new Set();
var timer = null;

function esc(s){ return (s == null ? '' : s.toString())
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function ago(iso){
  if (!iso) return '';
  var m = Math.floor((Date.now() - new Date(iso).getTime())/60000);
  if (isNaN(m)) return '';
  if (m < 1) return 'just now';
  if (m < 60) return m + 'm ago';
  return Math.floor(m/60) + 'h ' + (m%60) + 'm ago';
}

function gen(prefix){
  return prefix + '_' + Date.now().toString(36) + Math.random().toString(36).slice(2,6);
}

function authHeaders(isJson){
  var h = {};
  if (isJson) h['Content-Type'] = 'application/json';
  if (apiKey) h['X-Internal-Key'] = apiKey;
  return h;
}

function taskBadge(t){
  if (t === 'puc-certificate') return '<span class="task task-puc">PUC</span>';
  if (t === 'border-tax' || !t) return '<span class="task task-bt">Border tax</span>';
  if (t === 'challan-settlement') return '<span class="task task-cs">Challan settle</span>';
  if (t === 'challan-payment') return '<span class="task task-cp">Challan pay</span>';
  return '<span class="task">' + esc(t) + '</span>';
}

// ---------- view switching ----------
function showTab(name){
  document.getElementById('view-jobs').classList.toggle('hidden', name !== 'jobs');
  document.getElementById('view-run').classList.toggle('hidden', name !== 'run');
  var tabs = document.querySelectorAll('.tab');
  for (var i=0;i<tabs.length;i++) tabs[i].classList.toggle('active', tabs[i].getAttribute('data-tab') === name);
  if (name === 'jobs') fetchJobs();
}

var RUN_FORMS = {
  'puc': 'form-puc',
  'border-tax': 'form-border',
  'challan-settlement': 'form-challan-settlement',
  'challan-payment': 'form-challan-payment',
};

function showRun(which){
  for (var k in RUN_FORMS)
    document.getElementById(RUN_FORMS[k]).classList.toggle('hidden', which !== k);
  var rts = document.querySelectorAll('.rt');
  for (var i=0;i<rts.length;i++) rts[i].classList.toggle('active', rts[i].getAttribute('data-rt') === which);
}

// ---------- jobs ----------
async function fetchJobs(){
  var qs = 'limit=' + state.limit + '&offset=' + state.offset;
  if (state.taskId) qs += '&taskId=' + encodeURIComponent(state.taskId);
  if (state.status) qs += '&status=' + encodeURIComponent(state.status);
  if (state.source) qs += '&source=' + encodeURIComponent(state.source);
  if (state.path)   qs += '&path='   + encodeURIComponent(state.path);
  var data;
  try { data = await (await fetch('/api/jobs?' + qs)).json(); }
  catch (e) { return; }
  state.total = data.total || 0;
  renderStats(data.counts);
  var jobs = data.jobs || [];
  var grid = document.getElementById('grid');

  // Preserve a half-typed captcha/OTP answer so the 3s refresh can't wipe it.
  var act = document.activeElement;
  var keep = null;
  if (act && act.getAttribute && act.getAttribute('data-action') === 'send-input')
    keep = { id: act.getAttribute('data-id'), v: act.value, s: act.selectionStart };

  grid.innerHTML = jobs.length
    ? jobs.map(render).join('')
    : '<div class="empty">No jobs match. Try clearing filters, or start one from the Run tab.</div>';

  if (keep){
    var back = document.querySelector('input[data-action="send-input"][data-id="' + cssEsc(keep.id) + '"]');
    if (back){ back.value = keep.v; back.focus(); try { back.setSelectionRange(keep.s, keep.s); } catch (e) {} }
  }

  renderPager();
  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
}

function setStat(id, v){ var el = document.getElementById(id); if (el) el.textContent = (v == null ? 0 : v); }
function renderStats(c){
  c = c || {};
  setStat('s-done', c.done);
  setStat('s-cancelled', c.cancelled);
  setStat('s-failed', c.failed);
  setStat('s-running', c.running);
  setStat('s-waiting', c.waiting_for_human);
  setStat('s-queued', c.queued);
  setStat('s-total', c.total);
}

function renderPager(){
  var pages = Math.max(1, Math.ceil(state.total / state.limit));
  var cur = Math.floor(state.offset / state.limit) + 1;
  if (cur > pages) cur = pages;
  document.getElementById('count').textContent = state.total + ' job' + (state.total === 1 ? '' : 's');
  document.getElementById('pageinfo').textContent = 'Page ' + cur + ' / ' + pages;
  document.getElementById('prev').disabled = state.offset <= 0;
  document.getElementById('next').disabled = cur >= pages;
}

function render(j){
  var waiting = j.status === 'waiting_for_human';
  var h = '<div class="job">';
  h += '<div class="row"><span class="id">' + esc(j.id) + '</span>'
     + '<span class="tags">' + taskBadge(j.taskId)
     + '<span class="badge b-' + esc(j.status) + '">' + esc(j.status) + '</span></span></div>';
  h += '<div class="meta">' + esc(j.state || j.vehicleNumber || '') + ' · ' + esc(j.driverId || '')
     + ' · ' + esc(j.source || 'app') + ' · ' + esc(j.path || 'fullyAutomated')
     + ' · ' + ago(j.createdAt) + '</div>';
  if (j.agentStatus)
    h += '<div class="agent">lifecycle <b>' + esc(j.agentStatus) + '</b>'
       + (j.portalAmount ? ' · portal amount ₹<b>' + esc(j.portalAmount) + '</b>' : '') + '</div>';
  if (waiting && j.waitReason)
    h += '<div class="wait">' + esc(j.waitReason) + '</div>';

  var showCap = j.captchaUrl && (j.agentStatus === 'captchaSolving' || j.agentStatus === 'pendingTransactionCaptcha');
  var showQr  = j.qrCodeUrl && j.agentStatus === 'qrPaymentNeeded';
  if (showCap || showQr){
    h += '<div class="imgs">';
    if (showCap)
      h += '<figure><img class="cap" src="' + esc(j.captchaUrl) + '">'
         + '<figcaption>captcha · attempt ' + esc(j.captchaAttempt || '1') + '</figcaption></figure>';
    if (showQr)
      h += '<figure><img class="qr" src="' + esc(j.qrCodeUrl) + '">'
         + '<figcaption>UPI QR — scan to pay</figcaption></figure>';
    h += '</div>';
  }

  if (waiting && j.agentStatus !== 'humanHandover'){
    h += '<div class="controls">'
       + '<input type="text" data-action="send-input" data-id="' + esc(j.id) + '" placeholder="captcha text / OTP / paid">'
       + '<button class="btn" data-action="send" data-id="' + esc(j.id) + '">Send answer</button>'
       + '</div><div id="msg-' + esc(j.id) + '"></div>';
  }

  h += '<div class="links">';
  if (j.liveUrl) h += '<a href="' + esc(j.liveUrl) + '" target="_blank">Open live view</a>';
  if (['done','failed','cancelled','partial'].indexOf(j.status) === -1)
    h += '<button class="ghost" data-action="cancel" data-id="' + esc(j.id) + '">Cancel job</button>';
  if (j.receiptDocumentUrl)
    h += '<a class="ghost" href="' + esc(j.receiptDocumentUrl) + '" target="_blank">Receipt</a>';
  h += '</div>';

  if (j.result) h += '<div class="result">' + esc(j.result) + '</div>';
  if (j.error)  h += '<div class="note bad">' + esc(j.error) + '</div>';
  h += '</div>';
  return h;
}

async function send(id){
  var input = document.querySelector('input[data-action="send-input"][data-id="' + cssEsc(id) + '"]');
  var msg = document.getElementById('msg-' + id);
  var value = input ? input.value.trim() : '';
  if (!value){ if (input) input.focus(); return; }
  try {
    var res = await fetch('/api/jobs/' + encodeURIComponent(id) + '/intervene', {
      method:'POST', headers: authHeaders(true), body: JSON.stringify({ input: value }),
    });
    var data = await res.json();
    if (res.ok){
      sent.add(id);
      if (msg) msg.innerHTML = '<div class="note ok">Sent — agent resumes shortly.</div>';
      if (input) input.value = '';
      setTimeout(fetchJobs, 1500);
    } else if (msg) {
      msg.innerHTML = '<div class="note bad">' + esc(data.error || 'Failed') + '</div>';
    }
  } catch (e) {
    if (msg) msg.innerHTML = '<div class="note bad">Network error — try again.</div>';
  }
}

async function cancelJob(id){
  if (!confirm('Cancel job ' + id + '?')) return;
  try { await fetch('/api/jobs/' + encodeURIComponent(id) + '/cancel', { method:'POST', headers: authHeaders(false) }); }
  catch (e) {}
  fetchJobs();
}

function cssEsc(s){ return (s || '').replace(/["\\\\]/g, '\\\\$&'); }

// ---------- run forms ----------
async function submitRun(taskId, params, btn, source, path){
  var box = document.getElementById('runresult');
  if (btn) btn.disabled = true;
  box.innerHTML = '<div class="resbox">Submitting…</div>';
  try {
    var body = { taskId: taskId, source: source || 'app', params: params };
    if (path) body.path = path;
    var res = await fetch('/api/run', {
      method:'POST', headers: authHeaders(true),
      body: JSON.stringify(body),
    });
    var data = await res.json();
    if (res.ok && data.ok && data.jobId && data.status !== 'cancelled'){
      box.innerHTML = '<div class="resbox ok"><h3>Queued — ' + esc(data.jobId)
        + (data.deduped ? ' (already active)' : '') + '</h3>'
        + '<pre>' + esc(JSON.stringify(data, null, 2)) + '</pre>'
        + '<div class="links"><button class="btn" id="goto-jobs">View in Jobs</button></div></div>';
      var go = document.getElementById('goto-jobs');
      if (go) go.onclick = function(){
        document.getElementById('f-task').value = taskId;
        state.taskId = taskId; state.status = ''; state.offset = 0;
        document.getElementById('f-status').value = '';
        showTab('jobs');
      };
    } else if (data.status === 'cancelled'){
      box.innerHTML = '<div class="resbox bad"><h3>Cancelled up front</h3>'
        + '<pre>' + esc(JSON.stringify(data, null, 2)) + '</pre></div>';
    } else {
      box.innerHTML = '<div class="resbox bad"><h3>' + esc(data.message || data.error || ('HTTP ' + res.status)) + '</h3>'
        + '<pre>' + esc(JSON.stringify(data, null, 2)) + '</pre></div>';
    }
  } catch (e) {
    box.innerHTML = '<div class="resbox bad"><h3>Network error</h3><pre>' + esc(String(e)) + '</pre></div>';
  }
  if (btn) btn.disabled = false;
}

function val(id){ var el = document.getElementById(id); return el ? el.value.trim() : ''; }

function buildChallanSettlement(){
  // The API takes an array; accept commas, spaces or newlines so a list
  // pasted straight out of a settlement scan works unedited.
  var raw = val('cs-challanNumbers');
  var list = raw.split(/[\s,]+/).filter(function(x){ return x.length; });
  return {
    vehicleNumber: val('cs-vehicleNumber').toUpperCase(),
    challanNumbers: list,
    driverId: val('cs-driverId'),
  };
}

function buildChallanPayment(){
  var p = {
    vehicleNumber: val('cp-vehicleNumber').toUpperCase(),
    challanNo: val('cp-challanNo'),
    phoneNo: val('cp-phoneNo'),
    chassisNo: val('cp-chassisNo').toUpperCase(),
    driverId: val('cp-driverId'),
  };
  // requestId is derived server-side as the challan number; sending a
  // mismatched one is a 400, so we deliberately never send it.
  var dept = val('cp-department');
  if (dept) p.department = dept;
  return p;
}

function buildPuc(){
  var p = {
    requestId: val('puc-requestId') || gen('puc'),
    driverId: val('puc-driverId'),
    registrationNumber: val('puc-registrationNumber'),
    chassisNumber: val('puc-chassisNumber'),
  };
  document.getElementById('puc-requestId').value = p.requestId;
  return p;
}

function buildBorder(){
  var p = {
    requestId: val('bt-requestId') || gen('bt'),
    driverId: val('bt-driverId'),
    state: val('bt-state'),
    vehicleNumber: val('bt-vehicleNumber'),
    mobileNumber: val('bt-mobileNumber'),
    taxMode: val('bt-taxMode'),
    taxFrom: val('bt-taxFrom'),
    entryDistrict: val('bt-entryDistrict'),
  };
  if (val('bt-taxMode') === 'DAYS' && val('bt-taxUpto')) p.taxUpto = val('bt-taxUpto');
  if (val('bt-entryCheckpoint')) p.entryCheckpoint = val('bt-entryCheckpoint');
  document.getElementById('bt-requestId').value = p.requestId;
  // strip empties so the API applies its own defaults / reports missing
  Object.keys(p).forEach(function(k){ if (p[k] === '') delete p[k]; });
  return p;
}

// ---------- metadata ----------
async function loadMeta(){
  try {
    var t = await (await fetch('/api/tasks')).json();
    var sel = document.getElementById('f-task');
    var have = {};
    Array.prototype.forEach.call(sel.options, function(o){ have[o.value] = true; });
    (t.tasks || []).forEach(function(task){
      if (have[task.id]) return;
      var o = document.createElement('option');
      o.value = task.id; o.textContent = task.id;
      sel.appendChild(o);
    });
  } catch (e) {}
  try {
    var hres = await (await fetch('/api/health')).json();
    var s2 = document.getElementById('bt-state');
    (hres.states || []).forEach(function(st){
      var o = document.createElement('option');
      var tag = st.auto === false ? ' (scripted only)'
              : (st.scriptedEnabled ? ' (auto + scripted)' : '');
      o.value = st.code; o.textContent = st.code + ' — ' + st.name + tag;
      s2.appendChild(o);
    });
  } catch (e) {}
}

document.getElementById('bt-source').addEventListener('change', function(e){
  var p = document.getElementById('bt-path');
  p.disabled = e.target.value !== 'web';
});

// ---------- wiring ----------
document.querySelectorAll('.tab').forEach(function(b){
  b.addEventListener('click', function(){ showTab(b.getAttribute('data-tab')); });
});
document.querySelectorAll('.rt').forEach(function(b){
  b.addEventListener('click', function(){ showRun(b.getAttribute('data-rt')); });
});

document.getElementById('f-task').addEventListener('change', function(e){
  state.taskId = e.target.value; state.offset = 0; fetchJobs();
});
document.getElementById('f-status').addEventListener('change', function(e){
  state.status = e.target.value; state.offset = 0; fetchJobs();
});
document.getElementById('f-source').addEventListener('change', function(e){
  state.source = e.target.value; state.offset = 0; fetchJobs();
});
document.getElementById('f-path').addEventListener('change', function(e){
  state.path = e.target.value; state.offset = 0; fetchJobs();
});
document.getElementById('btn-refresh').addEventListener('click', fetchJobs);
document.getElementById('prev').addEventListener('click', function(){
  state.offset = Math.max(0, state.offset - state.limit); fetchJobs();
});
document.getElementById('next').addEventListener('click', function(){
  state.offset = state.offset + state.limit; fetchJobs();
});

// delegated job-card actions
var grid = document.getElementById('grid');
grid.addEventListener('click', function(e){
  var el = e.target.closest('[data-action]');
  if (!el) return;
  var id = el.getAttribute('data-id');
  var action = el.getAttribute('data-action');
  if (action === 'cancel') cancelJob(id);
  else if (action === 'send') send(id);
});
grid.addEventListener('keydown', function(e){
  if (e.key !== 'Enter') return;
  var inp = e.target.closest('input[data-action="send-input"]');
  if (inp){ e.preventDefault(); send(inp.getAttribute('data-id')); }
});

// generate-id buttons
document.querySelectorAll('[data-gen]').forEach(function(b){
  b.addEventListener('click', function(){
    document.getElementById(b.getAttribute('data-gen')).value = gen(b.getAttribute('data-prefix'));
  });
});

// api key persistence
var keyInput = document.getElementById('apikey');
keyInput.value = apiKey;
keyInput.addEventListener('change', function(){
  apiKey = keyInput.value.trim();
  try { localStorage.setItem(KEYKEY, apiKey); } catch (e) {}
});

// taxMode -> taxUpto visibility
document.getElementById('bt-taxMode').addEventListener('change', function(e){
  document.getElementById('bt-taxUpto-wrap').classList.toggle('hidden', e.target.value !== 'DAYS');
});

// sample fill
document.getElementById('puc-sample').addEventListener('click', function(){
  document.getElementById('puc-registrationNumber').value = 'UK17TA0921';
  document.getElementById('puc-chassisNumber').value = 'MA3ERLF1S00187562';
  document.getElementById('puc-requestId').value = gen('puc');
});

document.getElementById('cs-sample').addEventListener('click', function(){
  document.getElementById('cs-vehicleNumber').value = 'HR38AE8912';
  document.getElementById('cs-challanNumbers').value = 'UP123455, 57693177';
});
document.getElementById('cp-sample').addEventListener('click', function(){
  document.getElementById('cp-vehicleNumber').value = 'HR38AE8912';
  document.getElementById('cp-challanNo').value = 'UP123455';
  document.getElementById('cp-phoneNo').value = '9999999999';
  document.getElementById('cp-chassisNo').value = 'MA3ERLF1S00187562';
});

// form submits
document.getElementById('form-puc').addEventListener('submit', function(e){
  e.preventDefault();
  submitRun('puc-certificate', buildPuc(), e.submitter);
});
document.getElementById('form-border').addEventListener('submit', function(e){
  e.preventDefault();
  var src2 = val('bt-source') || 'app';
  submitRun('border-tax', buildBorder(), e.submitter,
    src2, src2 === 'web' ? (val('bt-path') || 'fullyAutomated') : undefined);
});
// Both challan tasks are scripted-only, so neither sends a path.
document.getElementById('form-challan-settlement').addEventListener('submit', function(e){
  e.preventDefault();
  submitRun('challan-settlement', buildChallanSettlement(), e.submitter,
    val('cs-source') || 'app');
});
document.getElementById('form-challan-payment').addEventListener('submit', function(e){
  e.preventDefault();
  submitRun('challan-payment', buildChallanPayment(), e.submitter,
    val('cp-source') || 'web');
});

// auto refresh (jobs view only)
function tick(){
  if (document.getElementById('auto').checked && !document.getElementById('view-jobs').classList.contains('hidden'))
    fetchJobs();
}
function startTimer(){ if (timer) clearInterval(timer); timer = setInterval(tick, 3000); }

loadMeta();
fetchJobs();
startTimer();
</script>
</body>
</html>`;
