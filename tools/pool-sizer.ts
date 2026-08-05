#!/usr/bin/env bun
// tools/pool-sizer.ts — v2. Blue/green migration for the CORE node pool.
//
//   bun tools/pool-sizer.ts                    # watchtower: booked vs busy vs allocatable
//   bun tools/pool-sizer.ts room               # what can I still fit on core?
//   bun tools/pool-sizer.ts plan --cpu 4       # print the sequence, change nothing
//   bun tools/pool-sizer.ts create --cpu 4     # build green, cordoned. Blue untouched.
//   bun tools/pool-sizer.ts cutover            # THE OUTAGE. ~60s. redis PVC moves.
//   bun tools/pool-sizer.ts rollback           # undo, whatever state we're in
//   bun tools/pool-sizer.ts finish             # delete blue. The only irreversible verb.
//   bun tools/pool-sizer.ts check              # phase + live shapes
//
// WHY BLUE/GREEN. Four things are physics and no strategy avoids them: the new VM
// must exist; pods must stop-then-start (there is no live migration); the redis
// RWO volume must detach-then-attach (strictly serial — a RWO disk cannot be on
// two nodes at once); the old VM must go away. Only the TIMING of that last one
// is a design choice. Blue/green = "defer it". That buys three real things:
//   1. you read green's REAL allocatable before committing (no model error)
//   2. rollback is `kubectl uncordon`, ~60s, not another 10-min gcloud op
//   3. the PD has hours to cleanly detach before blue is deleted — GCE disks can
//      hang half-attached if the holder is deleted too soon after a drain
//
// WHAT THIS TOOL WILL NOT DO:
//   - shrink anything (grow-only ratchet, every axis, no override flag)
//   - touch worker-pool (KEDA owns it; tripwire #4: 12 slots <-> 12Gi <-> e2-standard-4)
//   - run while jobs are in flight (queue must be empty, worker pods must be zero)
//
// Measured 2026-07-30, and the reason the shrink idea died: the core node is
// 228m/1930m CPU (11% USED, 98% BOOKED) and 2746Mi memory USED. A 4GB node
// allocates ~2890Mi — the live working set would have sat at 95% before anything
// happened. Requests looked padded; usage says they are not. Size on busy,
// schedule on booked, and never confuse the two again.

// knobs
const PROJECT = "cabswale-ai";
const ZONE = "asia-south1-a";       // MUST match the redis PVC's zone. Zonal PD.
const CLUSTER = "suvidha-cluster";
const NS = "suvidha";
const WORKER_POOL = "worker-pool";  // read-only. never touched by this file.
const CORE_LABEL = "role=core";

const DISK_GB = 50;
const DISK_TYPE = "pd-balanced";
const DRAIN_TIMEOUT = "300s";
const STATE_FILE = "tools/.pool-state/migration.json";

// Fraction of allocatable we accept as REQUESTED. Booking above this means one
// new kube-system daemonset from a control-plane upgrade = Pending pods.
const HEADROOM = 0.85;
// ─────────────────────────────────────────────────────────────────────────

const gb = (b: number) => b / 1e9;
const fmtGb = (b: number) => `${gb(b).toFixed(2)} GB`;
const pct = (a: number, b: number) => (b <= 0 ? "n/a" : `${((a / b) * 100).toFixed(0)}%`);

function sh(cmd: string[], quiet = false): string | null {
    const p = Bun.spawnSync(cmd, { stdout: "pipe", stderr: "pipe", stdin: "ignore" });
    if (!p.success) {
        if (!quiet) {
            const err = p.stderr.toString().trim().split("\n").slice(-3).join("\n     ");
            console.error(`  ! ${cmd.slice(0, 3).join(" ")} failed:\n     ${err}`);
        }
        return null;
    }
    return p.stdout.toString();
}

function shLive(cmd: string[]): boolean {
    console.log(`  $ ${cmd.join(" ")}`);
    return Bun.spawnSync(cmd, { stdout: "inherit", stderr: "inherit", stdin: "ignore" }).success;
}

function j<T>(cmd: string[], quiet = false): T | null {
    const out = sh(cmd, quiet);
    if (out === null) return null;
    try { return JSON.parse(out) as T; } catch { return null; }
}

const gcloudBase = ["gcloud", "container", "node-pools"];
const gcloudCtx = ["--cluster", CLUSTER, "--zone", ZONE, "--project", PROJECT];

// quantities 
function parseCpu(s?: string): number {
    if (!s) return 0;
    if (s.endsWith("m")) return parseFloat(s) / 1e3;
    if (s.endsWith("u")) return parseFloat(s) / 1e6;
    if (s.endsWith("n")) return parseFloat(s) / 1e9;
    return parseFloat(s) || 0;
}
const MEM_SUFFIX: Record<string, number> = {
    Ki: 1024, Mi: 1024 ** 2, Gi: 1024 ** 3, Ti: 1024 ** 4,
    k: 1e3, K: 1e3, M: 1e6, G: 1e9, T: 1e12,
};
function parseMem(s?: string): number {
    if (!s) return 0;
    const m = /^([0-9.]+)([A-Za-z]*)$/.exec(String(s).trim());
    if (!m) return 0;
    return parseFloat(m[1]!) * (m[2] ? (MEM_SUFFIX[m[2]] ?? 1) : 1);
}

// machine shapes 
interface Shape { vcpu: number; memMib: number; name: string }

function shapeOf(machineType: string): Shape | null {
    const t = machineType.trim().toLowerCase();
    const custom = /^[a-z0-9]+-custom-(\d+)-(\d+)$/.exec(t);
    if (custom) return { vcpu: +custom[1]!, memMib: +custom[2]!, name: t };
    const pre = /^[a-z0-9]+-(standard|highmem|highcpu)-(\d+)$/.exec(t);
    if (pre) {
        const perVcpu: Record<string, number> = { standard: 4, highmem: 8, highcpu: 1 };
        const n = +pre[2]!;
        return { vcpu: n, memMib: n * perVcpu[pre[1]!]! * 1024, name: t };
    }
    return null;  // shared-core and anything exotic: refuse rather than guess
}

// E2 custom rules: even vCPU 2-32, memory a multiple of 256 MB, 0.5-8 GB per vCPU.
function e2Custom(vcpu: number, memMib: number): string | { error: string } {
    if (vcpu % 2 !== 0 || vcpu < 2 || vcpu > 32)
        return { error: `E2 custom needs an even vCPU count 2-32 (got ${vcpu})` };
    if (memMib % 256 !== 0)
        return { error: `E2 custom memory must be a multiple of 256 MB (got ${memMib})` };
    const per = memMib / 1024 / vcpu;
    if (per < 0.5 || per > 8)
        return { error: `E2 custom needs 0.5-8 GB per vCPU (got ${per.toFixed(2)})` };
    return `e2-custom-${vcpu}-${memMib}`;
}

// Name carries the shape, so `kubectl get nodes` tells you what you're paying for.
const poolName = (s: Shape) => `core-${s.vcpu}c${Math.round(s.memMib / 1024)}g`;

// ── live cluster ─────────────────────────────────────────────────────────
interface NodeInfo {
    name: string; pool: string; ready: boolean; schedulable: boolean;
    allocCpu: number; allocMem: number; conditions: string[];
}
interface PodDraw {
    ns: string; name: string; node: string; cpu: number; mem: number;
    ready: boolean; restarts: number;
}

function nodes(): NodeInfo[] {
    const d = j<any>(["kubectl", "get", "nodes", "-o", "json"]);
    return (d?.items ?? []).map((n: any): NodeInfo => ({
        name: n.metadata?.name ?? "?",
        pool: n.metadata?.labels?.["cloud.google.com/gke-nodepool"] ?? "(none)",
        ready: (n.status?.conditions ?? []).some((c: any) => c.type === "Ready" && c.status === "True"),
        schedulable: !n.spec?.unschedulable,
        allocCpu: parseCpu(n.status?.allocatable?.cpu),
        allocMem: parseMem(n.status?.allocatable?.memory),
        conditions: (n.status?.conditions ?? [])
            .filter((c: any) => c.status === "True" && c.type !== "Ready")
            .map((c: any) => c.type),
    }));
}

function pods(): PodDraw[] {
    const d = j<any>(["kubectl", "get", "pods", "-A", "-o", "json"]);
    const out: PodDraw[] = [];
    for (const p of d?.items ?? []) {
        if (p.status?.phase === "Succeeded" || p.status?.phase === "Failed") continue;
        if (!p.spec?.nodeName) continue;
        let cpu = 0, mem = 0;
        for (const c of p.spec.containers ?? []) {
            cpu += parseCpu(c.resources?.requests?.cpu);
            mem += parseMem(c.resources?.requests?.memory);
        }
        for (const c of p.spec.initContainers ?? []) {
            cpu = Math.max(cpu, parseCpu(c.resources?.requests?.cpu));
            mem = Math.max(mem, parseMem(c.resources?.requests?.memory));
        }
        const cs = p.status?.containerStatuses ?? [];
        out.push({
            ns: p.metadata?.namespace ?? "?", name: p.metadata?.name ?? "?",
            node: p.spec.nodeName, cpu, mem,
            ready: cs.length > 0 && cs.every((c: any) => c.ready),
            restarts: cs.reduce((a: number, c: any) => a + (c.restartCount ?? 0), 0),
        });
    }
    return out;
}

// `kubectl top` — the number that matters. Booked != busy.
function usage(): Record<string, { cpu: number; mem: number }> {
    const out = sh(["kubectl", "top", "nodes", "--no-headers"], true);
    const map: Record<string, { cpu: number; mem: number }> = {};
    for (const line of (out ?? "").trim().split("\n").filter(Boolean)) {
        const f = line.trim().split(/\s+/);
        if (f.length >= 5) map[f[0]!] = { cpu: parseCpu(f[1]), mem: parseMem(f[3]) };
    }
    return map;
}

function listPools(): string[] {
    const d = j<any[]>([...gcloudBase, "list", ...gcloudCtx, "--format", "json"]);
    return (d ?? []).map((p) => p.name);
}
const describe = (pool: string) =>
    j<any>([...gcloudBase, "describe", pool, ...gcloudCtx, "--format", "json"], true);

// ── migration state, DERIVED from the cluster, not trusted from a file ───
type Phase = "IDLE" | "CREATED" | "CUTOVER";
interface State { phase: Phase; blue?: string; green?: string }

function detect(): State {
    const cores = listPools().filter((p) => p !== WORKER_POOL);
    if (cores.length === 0) return { phase: "IDLE" };
    if (cores.length === 1) return { phase: "IDLE", blue: cores[0] };
    if (cores.length > 2) {
        console.error(`  ! ${cores.length} core pools exist: ${cores.join(", ")}`);
        console.error(`    Expected at most 2. Resolve by hand before using this tool.`);
        process.exit(1);
    }
    const ps = pods().filter((p) => p.ns === NS);
    const ns = nodes();
    const homeOf = (pool: string) =>
        ps.filter((p) => ns.find((n) => n.name === p.node)?.pool === pool).length;
    const [a, b] = cores as [string, string];
    const live = homeOf(a) >= homeOf(b) ? a : b;
    const other = live === a ? b : a;
    const otherEmpty = homeOf(other) === 0;
    const otherCordoned = ns.filter((n) => n.pool === other).every((n) => !n.schedulable);
    return otherEmpty && otherCordoned && homeOf(live) > 0
        ? { phase: "CUTOVER", blue: other, green: live }
        : { phase: "CREATED", blue: live, green: other };
}

function saveState(extra: Record<string, unknown>): void {
    try {
        Bun.spawnSync(["mkdir", "-p", STATE_FILE.replace(/\/[^/]+$/, "")]);
        Bun.write(STATE_FILE, JSON.stringify({ at: new Date().toISOString(), ...extra }, null, 2));
    } catch { /* audit trail only; never block on it */ }
}

// ── gates ────────────────────────────────────────────────────────────────
function quiescent(): boolean {
    console.log(`── quiescence gate ──`);
    let ok = true;

    const q = sh(["kubectl", "-n", NS, "exec", "deploy/redis", "--", "redis-cli", "LLEN", "job:queue"]);
    const qlen = q === null ? NaN : Number(q.trim());
    if (Number.isNaN(qlen)) { console.log(`   ?  could not read job:queue — treating as NOT quiet`); ok = false; }
    else if (qlen > 0) { console.log(`   x  job:queue has ${qlen} entries`); ok = false; }
    else console.log(`   ok job:queue empty`);

    const wp = pods().filter((p) => p.ns === NS && p.name.startsWith("worker-"));
    if (wp.length > 0) { console.log(`   x  ${wp.length} worker pod(s) alive — a job may be mid-handover`); ok = false; }
    else console.log(`   ok no worker pods (nothing in flight)`);

    // A PDB that blocks eviction turns `drain` into an infinite hang.
    const pdbs = j<any>(["kubectl", "get", "pdb", "-A", "-o", "json"], true);
    const n = (pdbs?.items ?? []).length;
    if (n > 0) console.log(`   !  ${n} PodDisruptionBudget(s) exist — drain can block. Review them.`);
    else console.log(`   ok no PodDisruptionBudgets to block the drain`);

    if (!ok) {
        console.log(`\n   not quiet. If the worker is warm:  bun tools/keda-scaler.ts idle`);
        console.log(`   then wait for the queue to clear and the pods to drain.`);
    }
    return ok;
}

// Config fields that must match between blue and green, or the migration is a
// silent downgrade. This is the "recheck the work" step, in code.
const MUST_MATCH: [string, (c: any) => unknown][] = [
    ["imageType", (c) => c.config?.imageType],
    ["serviceAccount", (c) => c.config?.serviceAccount],
    ["oauthScopes", (c) => (c.config?.oauthScopes ?? []).slice().sort().join(",")],
    ["workloadMetadata", (c) => c.config?.workloadMetadataConfig?.mode ?? "(unset)"],
    ["secureBoot", (c) => c.config?.shieldedInstanceConfig?.enableSecureBoot ?? false],
    ["integrityMonitor", (c) => c.config?.shieldedInstanceConfig?.enableIntegrityMonitoring ?? false],
    ["autoUpgrade", (c) => c.management?.autoUpgrade ?? false],
    ["autoRepair", (c) => c.management?.autoRepair ?? false],
    ["version", (c) => c.version],
    ["diskType", (c) => c.config?.diskType],
    ["diskSizeGb", (c) => c.config?.diskSizeGb],
];

function verifyGreen(blue: string, green: string): boolean {
    const b = describe(blue), g = describe(green);
    if (!b || !g) { console.error(`  ! could not describe both pools`); return false; }

    console.log(`── config parity: ${green} vs ${blue} ──`);
    let ok = true;
    for (const [label, get] of MUST_MATCH) {
        const bv = String(get(b)), gv = String(get(g));
        if (bv === gv) console.log(`   ok ${label.padEnd(18)} ${gv}`);
        else { ok = false; console.log(`   x  ${label.padEnd(18)} blue=${bv}  green=${gv}`); }
    }
    const taints = g.config?.taints ?? [];
    if (taints.length > 0) { ok = false; console.log(`   x  green carries taints: ${JSON.stringify(taints)}`); }
    else console.log(`   ok green has no taints`);
    if ((g.config?.labels ?? {}).role === "worker") {
        ok = false; console.log(`   x  green is labelled role=worker — the worker would land on it`);
    }
    if (!ok) console.log(`\n   PARITY FAILED. rollback, fix the create flags, retry.`);
    return ok;
}

function confirm(promptText: string, want: string): boolean {
    const got = prompt(`${promptText}: `);
    if (got !== want) { console.log(`got "${got ?? ""}" — aborted, nothing changed.`); return false; }
    return true;
}

// ── verbs ────────────────────────────────────────────────────────────────
function cmdStatus(): void {
    const st = detect();
    const ns = nodes(), ps = pods(), use = usage();
    console.log(`cluster ${CLUSTER} · zone ${ZONE} · phase ${st.phase}`);
    console.log(st.phase !== "IDLE" ? `blue=${st.blue}  green=${st.green ?? "-"}\n` : "");

    for (const pool of [...new Set(ns.map((n) => n.pool))].sort()) {
        const mine = ns.filter((n) => n.pool === pool);
        const names = new Set(mine.map((n) => n.name));
        const mp = ps.filter((p) => names.has(p.node));
        const reqCpu = mp.reduce((a, p) => a + p.cpu, 0);
        const reqMem = mp.reduce((a, p) => a + p.mem, 0);
        const allocCpu = mine.reduce((a, n) => a + n.allocCpu, 0);
        const allocMem = mine.reduce((a, n) => a + n.allocMem, 0);
        const useCpu = mine.reduce((a, n) => a + (use[n.name]?.cpu ?? 0), 0);
        const useMem = mine.reduce((a, n) => a + (use[n.name]?.mem ?? 0), 0);
        const cfg = describe(pool)?.config ?? {};

        const flags = [
            pool === WORKER_POOL ? "READ-ONLY (KEDA owns it)" : "",
            pool === st.green ? "GREEN" : "",
            pool === st.blue && st.phase === "CUTOVER" ? "BLUE (drained, soaking)" : "",
            mine.length > 0 && mine.every((n) => !n.schedulable) ? "cordoned" : "",
        ].filter(Boolean).join(" · ");

        console.log(`── ${pool} ${flags ? `[${flags}]` : ""}`);
        console.log(`   ${cfg.machineType ?? "?"} · ${cfg.diskSizeGb ?? "?"} GB ${cfg.diskType ?? "?"} · ${mine.length} node(s)`);
        if (mine.length === 0) { console.log(`   no nodes — not billing\n`); continue; }
        console.log(`   CPU     booked ${reqCpu.toFixed(2)} / ${allocCpu.toFixed(2)} (${pct(reqCpu, allocCpu)})    busy ${useCpu.toFixed(2)} (${pct(useCpu, allocCpu)})`);
        console.log(`   memory  booked ${fmtGb(reqMem)} / ${fmtGb(allocMem)} (${pct(reqMem, allocMem)})    busy ${fmtGb(useMem)} (${pct(useMem, allocMem)})`);
        const bad = mine.flatMap((n) => n.conditions);
        if (bad.length) console.log(`   !  node conditions: ${bad.join(", ")}`);
        const sick = mp.filter((p) => !p.ready);
        if (sick.length) console.log(`   !  not ready: ${sick.map((p) => p.name).join(", ")}`);
        const rs = mp.filter((p) => p.restarts > 0);
        if (rs.length) console.log(`   !  restarts: ${rs.map((p) => `${p.name}x${p.restarts}`).join(", ")}`);
        console.log("");
    }
    console.log(`booked = what the scheduler reserved. busy = what is actually running.`);
    console.log(`they diverge wildly here. Size on busy; schedule on booked.`);
}

function cmdRoom(): void {
    const st = detect();
    if (!st.blue) { console.error("no core pool found"); process.exit(1); }
    const ns = nodes().filter((n) => n.pool === st.blue);
    const names = new Set(ns.map((n) => n.name));
    const ps = pods().filter((p) => names.has(p.node));
    const allocCpu = ns.reduce((a, n) => a + n.allocCpu, 0);
    const allocMem = ns.reduce((a, n) => a + n.allocMem, 0);
    const freeCpu = allocCpu * HEADROOM - ps.reduce((a, p) => a + p.cpu, 0);
    const freeMem = allocMem * HEADROOM - ps.reduce((a, p) => a + p.mem, 0);

    console.log(`${st.blue}: room for something new\n`);
    console.log(`   CPU     ${freeCpu.toFixed(3)} available (to the ${(HEADROOM * 100).toFixed(0)}% gate)`);
    console.log(`   memory  ${fmtGb(freeMem)} available\n`);
    if (freeCpu <= 0) {
        console.log(`   CPU is booked out — nothing new schedules here.`);
        console.log(`   This is a BOOKING limit, not a load limit: the node is ~11% busy.`);
        console.log(`   To grow:  bun tools/pool-sizer.ts plan --cpu 4    (-> core-4c8g, ~+$35/mo)`);
        console.log(`   That also unlocks api strategy:RollingUpdate — no downtime per deploy (tripwire #16).`);
    }
}

function targetShape(argv: string[]): Shape {
    const st = detect();
    const cur = shapeOf(describe(st.blue!)?.config?.machineType ?? "");
    if (!cur) { console.error("cannot model the current machine type"); process.exit(1); }
    const val = (f: string) => { const i = argv.indexOf(f); return i >= 0 ? Number(argv[i + 1]) : NaN; };
    const vcpu = Number.isNaN(val("--cpu")) ? cur.vcpu : val("--cpu");
    const memMib = Number.isNaN(val("--memory")) ? cur.memMib : val("--memory");

    // The ratchet. No override flag, deliberately.
    if (vcpu < cur.vcpu || memMib < cur.memMib) {
        console.error(`refusing to shrink: current ${cur.vcpu}c/${cur.memMib}Mi -> asked ${vcpu}c/${memMib}Mi`);
        console.error(`This tool grows only. Shrink in the console, deliberately, if you truly mean it.`);
        process.exit(1);
    }
    const name = vcpu === cur.vcpu && memMib === cur.memMib ? cur.name : e2Custom(vcpu, memMib);
    if (typeof name === "object") { console.error(name.error); process.exit(1); }
    return { vcpu, memMib, name };
}

const createArgs = (shape: Shape): string[] => [
    ...gcloudBase, "create", poolName(shape), ...gcloudCtx,
    "--machine-type", shape.name,
    "--disk-type", DISK_TYPE, "--disk-size", String(DISK_GB),
    "--num-nodes", "1", "--no-enable-autoscaling",
    "--node-labels", CORE_LABEL,
    "--enable-autoupgrade", "--enable-autorepair",
];

function cmdPlan(argv: string[]): void {
    const st = detect();
    const shape = targetShape(argv);
    console.log(`blue  ${st.blue}`);
    console.log(`green ${poolName(shape)}  (${shape.name}, ${DISK_GB} GB ${DISK_TYPE}, zone ${ZONE})\n`);
    console.log(`  1. create   build green, cordoned on arrival. Blue untouched — ZERO risk.`);
    console.log(`              then read green's REAL allocatable + check config parity vs blue`);
    console.log(`  2. cutover  uncordon green, cordon blue, drain blue.`);
    console.log(`              THE OUTAGE: redis PVC detaches/attaches, api+router restart (~60s)`);
    console.log(`  3. soak     burn-in 10/10 + one real job end-to-end. Blue alive, cordoned.`);
    console.log(`  4. finish   delete blue. IRREVERSIBLE.\n`);
    console.log(`  rollback works at any point before finish.\n`);
    console.log(`  create will run:\n    ${createArgs(shape).join(" ")}\n`);
    console.log(`  Compare that against:  gcloud container node-pools describe ${st.blue} ${gcloudCtx.join(" ")}`);
    console.log(`  create verifies parity automatically afterwards, but eyes on it first is free.`);
    if (st.phase !== "IDLE") console.log(`\n  ! phase is ${st.phase} — resolve the existing migration first.`);
}

function cmdCreate(argv: string[]): void {
    const st = detect();
    if (st.phase !== "IDLE") { console.error(`phase is ${st.phase}, not IDLE. rollback or finish first.`); process.exit(1); }
    const shape = targetShape(argv);
    const green = poolName(shape);
    if (green === st.blue) { console.error(`green would be named ${green} — same as blue. Change the shape.`); process.exit(1); }

    console.log(`creating ${green} alongside ${st.blue}. Blue is not touched.\n`);
    if (!shLive(createArgs(shape))) process.exit(1);

    // Cordon on arrival so nothing drifts onto green before we've checked it.
    // DaemonSets still land (they tolerate the unschedulable taint) — which is
    // what we want: they must be Ready before the allocatable number means anything.
    console.log(`\nwaiting for the node, then cordoning it...`);
    let node = "";
    for (let i = 0; i < 40; i++) {
        const n = nodes().find((x) => x.pool === green && x.ready);
        if (n) { node = n.name; break; }
        Bun.sleepSync(15_000);
    }
    if (!node) { console.error(`node never became Ready. Investigate, then rollback.`); process.exit(1); }
    shLive(["kubectl", "cordon", node]);

    console.log(`\nwaiting ~90s for DaemonSets to settle...`);
    Bun.sleepSync(90_000);

    const ok = verifyGreen(st.blue!, green);
    const gn = nodes().find((x) => x.name === node)!;
    const dp = pods().filter((p) => p.node === node);
    console.log(`\n── green measured, not modelled ──`);
    console.log(`   allocatable  ${gn.allocCpu.toFixed(2)} CPU / ${fmtGb(gn.allocMem)}`);
    console.log(`   daemonsets   ${dp.length} pods, ${dp.reduce((a, p) => a + p.cpu, 0).toFixed(2)} CPU / ${fmtGb(dp.reduce((a, p) => a + p.mem, 0))}`);

    const bn = new Set(nodes().filter((n) => n.pool === st.blue).map((n) => n.name));
    const app = pods().filter((p) => bn.has(p.node) && p.ns === NS);
    const needCpu = app.reduce((a, p) => a + p.cpu, 0) + dp.reduce((a, p) => a + p.cpu, 0);
    const needMem = app.reduce((a, p) => a + p.mem, 0) + dp.reduce((a, p) => a + p.mem, 0);
    console.log(`\n   after cutover, at least: ${needCpu.toFixed(2)} CPU / ${fmtGb(needMem)} booked`);
    console.log(`   (kube-system Deployments reschedule here too — worker-pool's taint sends them`);
    console.log(`    to core, so expect roughly blue's current booking. Check status after cutover.)`);

    saveState({ phase: "CREATED", blue: st.blue, green, node });
    console.log(ok
        ? `\nGREEN IS READY AND PARKED. Blue still serving, nothing disrupted.\n` +
        `  next: bun tools/pool-sizer.ts cutover\n` +
        `  or:   bun tools/pool-sizer.ts rollback   # deletes green, no outage — the free rehearsal`
        : `\nparity failed — rollback, fix the create flags, retry.`);
}

function cmdCutover(): void {
    const st = detect();
    if (st.phase !== "CREATED" || !st.blue || !st.green) { console.error(`phase is ${st.phase}; need CREATED.`); process.exit(1); }
    if (!verifyGreen(st.blue, st.green)) process.exit(1);
    console.log("");
    if (!quiescent()) process.exit(1);

    console.log(`\nTHIS IS THE OUTAGE. redis detaches its PVC and reattaches on green.`);
    console.log(`api and router restart. Expect ~60s where /api/run is unavailable.\n`);
    if (!confirm(`type "cutover" to proceed`, "cutover")) process.exit(1);

    for (const n of nodes().filter((n) => n.pool === st.green)) shLive(["kubectl", "uncordon", n.name]);
    for (const n of nodes().filter((n) => n.pool === st.blue)) shLive(["kubectl", "cordon", n.name]);
    for (const n of nodes().filter((n) => n.pool === st.blue)) {
        if (!shLive(["kubectl", "drain", n.name, "--ignore-daemonsets",
            "--delete-emptydir-data", `--timeout=${DRAIN_TIMEOUT}`])) {
            console.error(`\ndrain failed. Blue is cordoned but still holds pods.`);
            console.error(`  bun tools/pool-sizer.ts rollback   # uncordon blue, put it back`);
            process.exit(1);
        }
    }
    saveState({ phase: "CUTOVER", blue: st.blue, green: st.green });

    console.log(`\n── verify ──`);
    Bun.sleepSync(20_000);
    const bad = pods().filter((p) => p.ns === NS && !p.ready);
    console.log(bad.length ? `   x  not ready: ${bad.map((p) => p.name).join(", ")}` : `   ok all ${NS} pods ready`);
    const q = sh(["kubectl", "-n", NS, "exec", "deploy/redis", "--", "redis-cli", "LLEN", "job:queue"]);
    console.log(q !== null ? `   ok redis answering (job:queue = ${q.trim()})` : `   x  redis not answering`);

    console.log(`
SOAK. Blue is cordoned but alive — rollback is ~60s until you run finish.

  exit the soak when BOTH pass:
    1. tools/burn-in-matrix.sh  -> 10/10
    2. one real enqueued job completes end to end

  roll back immediately on ANY of:
    - a pod not Ready for >5 min
    - any container restart on the green node
    - node condition MemoryPressure / DiskPressure / NotReady
    - /api/health non-200
    - a job reconciled failed with an abort reason you don't recognise

  ceiling: 72h. If no real traffic by then, fire a --enqueue burn-in rather than
  letting blue bill on. A time-only soak on an idle cluster proves almost nothing;
  traffic is the axis that matters, not hours.

  watch:  bun tools/pool-sizer.ts status
  end it: bun tools/pool-sizer.ts finish`);
}

function cmdRollback(): void {
    const st = detect();
    if (st.phase === "IDLE") { console.log(`nothing to roll back.`); return; }
    if (!st.green || !st.blue) { console.error(`ambiguous state; resolve by hand.`); process.exit(1); }

    if (st.phase === "CREATED") {
        console.log(`green ${st.green} was never cut over — deleting it.`);
        console.log(`No outage. Blue was never touched.\n`);
        if (!confirm(`type "${st.green}" to delete it`, st.green)) process.exit(1);
        shLive([...gcloudBase, "delete", st.green, ...gcloudCtx, "--quiet"]);
        saveState({ phase: "IDLE", note: `rolled back ${st.green} before cutover` });
        return;
    }

    console.log(`undoing the cutover: ${st.green} -> ${st.blue}.`);
    console.log(`This is a SECOND outage — redis moves back. Same ~60s.\n`);
    if (!confirm(`type "rollback" to proceed`, "rollback")) process.exit(1);
    for (const n of nodes().filter((n) => n.pool === st.blue)) shLive(["kubectl", "uncordon", n.name]);
    for (const n of nodes().filter((n) => n.pool === st.green)) shLive(["kubectl", "cordon", n.name]);
    for (const n of nodes().filter((n) => n.pool === st.green)) {
        shLive(["kubectl", "drain", n.name, "--ignore-daemonsets",
            "--delete-emptydir-data", `--timeout=${DRAIN_TIMEOUT}`]);
    }
    console.log(`\nblue is serving again. Delete green when you're satisfied:`);
    console.log(`  gcloud container node-pools delete ${st.green} ${gcloudCtx.join(" ")}`);
    saveState({ phase: "IDLE", note: `rolled back to ${st.blue}` });
}

function cmdFinish(): void {
    const st = detect();
    if (st.phase !== "CUTOVER" || !st.blue || !st.green) { console.error(`phase is ${st.phase}; need CUTOVER.`); process.exit(1); }
    console.log(`This DELETES ${st.blue}. After it, there is no 60-second undo —`);
    console.log(`recovery would mean another full migration.\n`);
    console.log(`Confirm you have: burn-in 10/10 · one real job end-to-end · no restarts on green.\n`);
    if (!confirm(`type "${st.blue}" to delete it`, st.blue)) process.exit(1);
    if (!shLive([...gcloudBase, "delete", st.blue, ...gcloudCtx, "--quiet"])) process.exit(1);
    saveState({ phase: "IDLE", note: `finished; core pool is now ${st.green}` });
    console.log(`\ndone. Paste into MEMORY.md §4:`);
    console.log(`  Cluster suvidha-cluster: \`${st.green}\` 1x ${describe(st.green)?.config?.machineType}`);
    console.log(`  (autoscaling OFF, label ${CORE_LABEL}) · \`${WORKER_POOL}\` e2-standard-4,`);
    console.log(`  autoscaling 0-3, label role=worker, taint dedicated=worker:NoSchedule`);
}

function cmdCheck(): void {
    const st = detect();
    console.log(`phase ${st.phase}`);
    if (st.phase !== "IDLE") {
        console.log(`migration in progress: blue=${st.blue} green=${st.green}`);
        process.exit(1);
    }
    const c = describe(st.blue!)?.config;
    const w = describe(WORKER_POOL)?.config;
    console.log(`core   ${st.blue}: ${c?.machineType} · ${c?.diskSizeGb} GB ${c?.diskType}`);
    console.log(`worker ${WORKER_POOL}: ${w?.machineType} · ${w?.diskSizeGb} GB ${w?.diskType}  (read-only)`);
}

// entrypoint 
const [cmd = "status", ...rest] = process.argv.slice(2);
switch (cmd) {
    case "status": cmdStatus(); break;
    case "room": cmdRoom(); break;
    case "plan": cmdPlan(rest); break;
    case "create": cmdCreate(rest); break;
    case "cutover": cmdCutover(); break;
    case "rollback": cmdRollback(); break;
    case "finish": cmdFinish(); break;
    case "check": cmdCheck(); break;
    default:
        console.error(`unknown "${cmd}" — use: status | room | plan | create | cutover | rollback | finish | check`);
        process.exit(1);
}
