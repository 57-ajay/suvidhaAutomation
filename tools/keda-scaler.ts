//  flip the worker fleet between warm and scale-to-zero.
//
//   bun keda-scaler.ts            # status: ScaledObject, pods, nodes, queue
//   bun keda-scaler.ts idle       # min=0  — worker node exists ONLY when
//                                       #          jobs are queued (KEDA wakes it)
//   bun keda-scaler.ts live       # min=1  — today's config, warm worker
//   bun keda-scaler.ts set --min 0 --max 5   # explicit values
//
// Patches the LIVE ScaledObject (KEDA reconciles the HPA within seconds; no
// restarts, no yaml edits). k8s/scaledobject.yaml keeps the committed "live"
// values, so `kubectl apply -f k8s/scaledobject.yaml` is always the reset
// button — `idle` is a temporary override by design.
//
// Cost math this controls: the e2-standard-4 worker node (~$100/mo) bills
// whenever it exists. min=1 keeps one forever; min=0 lets the cluster
// autoscaler remove it ~10 min after the last pod drains, and re-create it
// ~2–4 min after the next job is queued (KEDA poll ≤15s + node provision +
// image pull). The API/redis/router node is NOT touched — gkep stays up.

// ── editable knobs ───────────────────────────────────────────────────────
const NS = "suvidha";
const SCALED_OBJECT = "worker-scaler";
const WORKER_DEPLOY = "worker";
const PRESETS: Record<string, { min: number; max: number }> = {
    idle: { min: 0, max: 3 }, // scale-to-zero, auto-wake on queue activity
    live: { min: 1, max: 3 }, // matches k8s/scaledobject.yaml
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

    console.log(`ScaledObject ${SCALED_OBJECT}: min/max = ${so}`);
    console.log(`worker deploy: ${deploy.replace("undefined", "0").replace(", undefined ready", ", 0 ready")}`);
    console.log(`worker pods:\n  ${pods.split("\n").join("\n  ")}`);
    console.log(`worker nodes:\n  ${nodes.split("\n").join("\n  ")}`);
    console.log(`job:queue length: ${queue}`);
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
  • queue empty -> pods drop to 0 within KEDA's cooldown (~5 min); a pod
    mid-job first DRAINS on SIGTERM (proven) — in-flight work finishes.
  • the empty worker node is removed by the cluster autoscaler ~10 min
    later — that's when the ~$100/mo meter stops.
  • ANY queued job (yours or the client's integration test) wakes it:
    expect ~2–4 min before the first job starts (poll + node + image pull).
    Tell the client that first-run latency is expected in this mode.
  • back to warm: bun tools/keda-scaler.ts live   (or kubectl apply -f
    k8s/scaledobject.yaml). Do that before scheduled test sessions.`);
    } else {
        console.log(`
[${label}] Warm mode: a worker pod (and its node, ~1–2 min if none exists)
comes up now and stays. This matches the committed k8s/scaledobject.yaml.`);
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
