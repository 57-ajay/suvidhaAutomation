// /tmp/apiharness/src/harness.ts — executes handleRun against every branch of
// the source/path matrix with recording stubs. Bundled with esbuild, run in
// node. HARNESS_MODE=disabled runs only the empty-allow-list case (config
// reads the env at import time, so that case needs its own process).
import { handleRun } from "./routes/run";

type Cap = { existing?: string; multi?: any; statusWrites?: any[] };
const g = globalThis as any;

let passed = 0;
let failed = 0;

function reset(existing?: string): Cap {
  g.__cap = { existing } as Cap;
  return g.__cap;
}

async function call(body: any): Promise<{ status: number; data: any }> {
  const res = await handleRun({ __body: body } as any);
  return { status: res.status, data: await res.json() };
}

function check(name: string, cond: boolean, detail?: any) {
  if (cond) {
    passed++;
    console.log(`  ok  ${name}`);
  } else {
    failed++;
    console.log(`FAIL  ${name}`, detail !== undefined ? JSON.stringify(detail) : "");
  }
}

const VALID_UP = {
  requestId: "req_up_1",
  driverId: "drv1",
  vehicleNumber: "UP32AB1234",
  mobileNumber: "9999999999",
  state: "UP",
  taxMode: "DAYS",
  taxFrom: "2026-07-25",
  taxUpto: "2026-07-26",
  paymentMethod: "UPI",
  entryDistrict: "SAHARANPUR",
};

async function main() {
  const mode = process.env.HARNESS_MODE ?? "enabled";

  if (mode === "disabled") {
    // Case 8: allow-list EMPTY -> scripted disabled everywhere.
    reset();
    const r = await call({
      taskId: "border-tax",
      source: "web",
      path: "scripted",
      params: { ...VALID_UP },
    });
    check(
      "8. web+scripted with empty allow-list -> 400 + env hint",
      r.status === 400 &&
        r.data.error === "unsupported_path" &&
        /SCRIPTED_BORDER_TAX_STATES/.test(r.data.message),
      r.data,
    );
    console.log(`\n(disabled mode) passed=${passed} failed=${failed}`);
    if (failed) process.exit(1);
    return;
  }

  // ── enabled mode: SCRIPTED_BORDER_TAX_STATES=UP,UK,TN ──────────────────

  // 1. missing source
  reset();
  let r = await call({ taskId: "border-tax", params: { ...VALID_UP } });
  check(
    "1. missing source -> 400 missing:[source]",
    r.status === 400 && r.data.missing?.includes("source"),
    r.data,
  );

  // 2. bad source
  reset();
  r = await call({ taskId: "border-tax", source: "phone", params: { ...VALID_UP } });
  check(
    "2. source=phone -> 400 unsupported source",
    r.status === 400 && /unsupported source/.test(r.data.error ?? ""),
    r.data,
  );

  // 3. app + explicit scripted path
  reset();
  r = await call({
    taskId: "border-tax",
    source: "app",
    path: "scripted",
    params: { ...VALID_UP },
  });
  check(
    "3. app+path=scripted -> 400 unsupported_path",
    r.status === 400 && r.data.error === "unsupported_path",
    r.data,
  );

  // 4. app happy path -> fullyAutomated persisted everywhere
  let cap = reset();
  r = await call({ taskId: "border-tax", source: "app", params: { ...VALID_UP } });
  const hset4 = cap.multi?.hsets?.[0] ?? {};
  const seed4 = cap.statusWrites?.[0] ?? {};
  check(
    "4a. app UP -> 200 queued path=fullyAutomated",
    r.status === 200 && r.data.status === "queued" && r.data.path === "fullyAutomated",
    r.data,
  );
  check(
    "4b. job hash carries path=fullyAutomated",
    hset4.path === "fullyAutomated" && hset4.source === "app",
    hset4,
  );
  check(
    "4c. queued seed writes aiAgentData.path",
    seed4.to === "queued" && seed4.extra?.path === "fullyAutomated" && seed4.force === true,
    seed4,
  );

  // 5. web without path
  reset();
  r = await call({ taskId: "border-tax", source: "web", params: { ...VALID_UP } });
  check(
    "5. web without path -> 400 missing:[path]",
    r.status === 400 && r.data.missing?.includes("path"),
    r.data,
  );

  // 6. web with junk path
  reset();
  r = await call({
    taskId: "border-tax",
    source: "web",
    path: "banana",
    params: { ...VALID_UP },
  });
  check(
    "6. web+path=banana -> 400 unsupported_path",
    r.status === 400 && r.data.error === "unsupported_path",
    r.data,
  );

  // 7. web + fullyAutomated + UK (scripted-only state)
  reset();
  r = await call({
    taskId: "border-tax",
    source: "web",
    path: "fullyAutomated",
    params: { ...VALID_UP, state: "UK", vehicleNumber: "UK07AB1234" },
  });
  check(
    "7. web+fullyAutomated+UK -> 400 'not fully automated'",
    r.status === 400 && /not fully automated/.test(r.data.message ?? ""),
    r.data,
  );

  // 9. web + scripted + UP with a non-UPI paymentMethod: the shim must let it
  //    through UP's UPI pin AND the stored params must carry the caller's
  //    method back.
  cap = reset();
  r = await call({
    taskId: "border-tax",
    source: "web",
    path: "scripted",
    params: { ...VALID_UP, requestId: "req_up_scripted", paymentMethod: "net_banking" },
  });
  const hset9 = cap.multi?.hsets?.[0] ?? {};
  const params9 = JSON.parse(hset9.params ?? "{}");
  check(
    "9a. web+scripted+UP(net_banking) -> 200 queued path=scripted",
    r.status === 200 && r.data.status === "queued" && r.data.path === "scripted",
    r.data,
  );
  check(
    "9b. shim restored caller paymentMethod NET_BANKING in stored params",
    params9.paymentMethod === "NET_BANKING",
    params9.paymentMethod,
  );
  check(
    "9c. hash + seed carry path=scripted",
    hset9.path === "scripted" && cap.statusWrites?.[0]?.extra?.path === "scripted",
    { hash: hset9.path, seed: cap.statusWrites?.[0]?.extra },
  );

  // 9d. shim must NOT leak: an app UP run with net_banking still 400s.
  reset();
  r = await call({
    taskId: "border-tax",
    source: "app",
    params: { ...VALID_UP, paymentMethod: "net_banking" },
  });
  check(
    "9d. app UP net_banking still rejected (UPI pin intact)",
    r.status === 400 && r.data.error === "validation_failed",
    r.data,
  );

  // 10. web + scripted + UK: scripted-only validator defaults hold
  //     (NETBANKING default, DAYS same-day taxUpto from duration).
  cap = reset();
  r = await call({
    taskId: "border-tax",
    source: "web",
    path: "scripted",
    params: {
      requestId: "req_uk_1",
      driverId: "drv1",
      vehicleNumber: "UK07AB1234",
      state: "UTTARAKHAND", // alias must resolve
      taxMode: "DAYS",
      taxFrom: "2026-07-25",
      duration: 1,
    },
  });
  const paramsUK = JSON.parse(cap.multi?.hsets?.[0]?.params ?? "{}");
  check(
    "10a. web+scripted+UTTARAKHAND (alias) -> 200 queued",
    r.status === 200 && r.data.status === "queued",
    r.data,
  );
  check(
    "10b. UK defaults: NETBANKING + DEHRADUN + same-day taxUpto",
    paramsUK.paymentMethod === "NETBANKING" &&
      paramsUK.entryDistrict === "DEHRADUN" &&
      paramsUK.taxUpto === "2026-07-25" &&
      paramsUK.stateCode === "UK",
    paramsUK,
  );

  // 11. TN: DAYS is invalid; WEEKLY passes with taxUpto stripped.
  reset();
  r = await call({
    taskId: "border-tax",
    source: "web",
    path: "scripted",
    params: {
      requestId: "req_tn_1",
      driverId: "drv1",
      vehicleNumber: "TN01AB1234",
      state: "TN",
      taxMode: "DAYS",
      taxFrom: "2026-07-25",
    },
  });
  check(
    "11a. TN taxMode=DAYS -> 400 validation_failed",
    r.status === 400 && r.data.error === "validation_failed",
    r.data,
  );
  cap = reset();
  r = await call({
    taskId: "border-tax",
    source: "web",
    path: "scripted",
    params: {
      requestId: "req_tn_2",
      driverId: "drv1",
      vehicleNumber: "TN01AB1234",
      state: "TN",
      taxMode: "WEEKLY",
      taxFrom: "2026-07-25",
      taxUpto: "2026-09-01", // must be STRIPPED (portal derives)
    },
  });
  const paramsTN = JSON.parse(cap.multi?.hsets?.[0]?.params ?? "{}");
  check(
    "11b. TN WEEKLY -> 200; client taxUpto stripped to ''",
    r.status === 200 && paramsTN.taxUpto === "" && paramsTN.taxMode === "WEEKLY",
    paramsTN,
  );

  // 12. PUC pinned to app.
  reset();
  r = await call({
    taskId: "puc-certificate",
    source: "web",
    params: {
      requestId: "puc1",
      driverId: "drv1",
      registrationNumber: "UP32AB1234",
      chassisNumber: "ABCDE12345",
    },
  });
  check(
    "12. puc + web -> 400 app only",
    r.status === 400 && /app only/.test(r.data.error ?? ""),
    r.data,
  );

  // 13. validation failure still propagates missing fields.
  reset();
  r = await call({
    taskId: "border-tax",
    source: "app",
    params: { ...VALID_UP, vehicleNumber: undefined },
  });
  check(
    "13. missing vehicleNumber -> 400 validation_failed",
    r.status === 400 &&
      r.data.error === "validation_failed" &&
      r.data.missing?.includes("vehicleNumber"),
    r.data,
  );

  // 14. dedupe path untouched: existing active job returns deduped.
  reset("running");
  r = await call({ taskId: "border-tax", source: "app", params: { ...VALID_UP } });
  check(
    "14. active job -> deduped response",
    r.status === 200 && r.data.deduped === true && r.data.status === "running",
    r.data,
  );

  console.log(`\n(enabled mode) passed=${passed} failed=${failed}`);
  if (failed) process.exit(1);
}

main().catch((e) => {
  console.error("harness crashed:", e);
  process.exit(1);
});
