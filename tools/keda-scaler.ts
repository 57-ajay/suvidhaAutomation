//  flip the worker fleet between warm and scale-to-zero.
//
//   bun keda-scaler.ts            # status: ScaledObject, pods, nodes, queue
//   bun keda-scaler.ts idle       # min=0  — today's config: worker node exists
//                                       #          ONLY while jobs are queued or in
//                                       #          flight (KEDA wakes it)
//   bun keda-scaler.ts live       # min=1  — warm worker, no cold start
//   bun keda-scaler.ts set --min 0 --max 5   # explicit values
//
// Patches the LIVE ScaledObject (KEDA reconciles the HPA within seconds; no
// restarts, no yaml edits). k8s/scaledobject.yaml keeps the committed "idle"
// values, so `kubectl apply -f k8s/scaledobject.yaml` is always the reset
// button — `live` is a temporary override (e.g. for a test session).
//
// Cost math this controls: the e2-highcpu-4 worker node bills whenever it
// exists. min=1 keeps one forever; min=0 lets the cluster autoscaler remove it
// ~10 min after the last pod drains, and re-create it ~2–4 min after the next
// job is queued (KEDA poll ≤15s + node provision + image pull). The pod stays
// up while job:active is non-empty, so a long handover is never cut short.
// The API/redis/router node is NOT touched — gkep stays up.

// ── editable knobs ───────────────────────────────────────────────────────
const NS = "suvidha";
const SCALED_OBJECT = "worker-scaler";
const WORKER_DEPLOY = "worker";
const PRESETS: Record<string, { min: number; max: number }> = {
    idle: { min: 0, max: 4 }, // matches k8s/scaledobject.yaml
    live: { min: 1, max: 4 }, // warm worker for test sessions
};
// ─────────────────────────────────────────────────────────────────────────

function k(...args: string[]): string {
    const proc = Bun.spawnSync(["kubectl", "-n", NS, ...args], {
        stdout: "pipe",
        stderr: "pipe",
        stdin: "ignore",
    });
    if (!proc.success) {
        const err = proc.stderr.toString().trim().split("\n")[0] || `exit ${proc.exitCode}`;
        return `(kubectl failed: ${err})`;
    }
    return proc.stdout.toString().trim();
}

function showStatus(): void {
    const so = k(
        "get", "scaledobject", SCALED_OBJECT,
        "-o", "jsonpath={.spec.minReplicaCount}/{.spec.maxReplicaCount}",
    );
    const deploy = k(
        "get", "deploy", WORKER_DEPLOY,
        "-o", "jsonpath={.spec.replicas} desired, {.status.readyReplicas} ready",
    );
    const pods = k("get", "pods", "-l", "app=worker", "--no-headers") || "(none)";
    const nodes = k(
        "get", "nodes", "-l", "role=worker", "--no-headers", "--ignore-not-found",
    ) || "(none — not billing)";
    const queue = k("exec", "deploy/redis", "--", "redis-cli", "LLEN", "job:queue");
    const active = k("exec", "deploy/redis", "--", "redis-cli", "LLEN", "job:active");

    console.log(`ScaledObject ${SCALED_OBJECT}: min/max = ${so}`);
    console.log(`worker deploy: ${deploy.replace("undefined", "0").replace(", undefined ready", ", 0 ready")}`);
    console.log(`worker pods:\n  ${pods.split("\n").join("\n  ")}`);
    console.log(`worker nodes:\n  ${nodes.split("\n").join("\n  ")}`);
    console.log(`job:queue length: ${queue}`);
    console.log(`job:active length: ${active}`);
}

function apply(min: number, max: number, label: string): void {
    if (!Number.isInteger(min) || !Number.isInteger(max) || min < 0 || max < 1 || min > max || max > 10) {
        console.error(`refusing: min=${min} max=${max} (need 0 <= min <= max <= 10)`);
        process.exit(1);
    }
    console.log(`── before ──`);
    showStatus();
    const patch = JSON.stringify({ spec: { minReplicaCount: min, maxReplicaCount: max } });
    const out = k("patch", "scaledobject", SCALED_OBJECT, "--type", "merge", "-p", patch);
    console.log(`\n${out}`);
    console.log(`\n── after (KEDA reconciles within ~15s) ──`);
    showStatus();

    if (min === 0) {
        console.log(`
[idle] What happens next, all automatic:
  • queue AND job:active empty -> pods drop to 0 after KEDA's cooldown
    (~5 min). In-flight jobs keep their pod; a pod that is scaled down
    mid-job anyway DRAINS on SIGTERM (proven) — in-flight work finishes.
  • the empty worker node is removed by the cluster autoscaler ~10 min
    later — that's when the ~$100/mo meter stops.
  • ANY queued job (yours or the client's integration test) wakes it:
    expect ~2–4 min before the first job starts (poll + node + image pull).
    Tell the client that first-run latency is expected in this mode.
  • back to warm: bun tools/keda-scaler.ts live. Do that before scheduled
    test sessions. This matches the committed k8s/scaledobject.yaml.`);
    } else {
        console.log(`
[${label}] Warm mode: a worker pod (and its node, ~1–2 min if none exists)
comes up now and stays. Reset: kubectl apply -f k8s/scaledobject.yaml`);
    }
}

const [cmd = "status", ...rest] = process.argv.slice(2);
if (cmd === "status") {
    showStatus();
} else if (cmd in PRESETS) {
    const p = PRESETS[cmd]!;
    apply(p.min, p.max, cmd);
} else if (cmd === "set") {
    const val = (flag: string): number =>
        Number(rest[rest.indexOf(flag) + 1] ?? NaN);
    apply(val("--min"), val("--max"), "set");
} else {
    console.error(`unknown command "${cmd}" — use: status | idle | live | set --min N --max M`);
    process.exit(1);
}
