// api/src/validators/index.ts
//
// State registry. Adding a state:
//   1. Write validators/states/<code>.ts exporting a StateValidator.
//   2. Register it in VALIDATORS below (and in AUTO_STATE_CODES if the worker
//      has a fully-automated runner for it).
//   3. Mirror the runner on the worker: fully-automated states live in
//      tasks/border_tax/states/<code>.py (+ its registry), scripted states in
//      scripted/states/<code>.py (+ scripted/registry.py). Nothing else
//      changes.
//
// Capability model (path routing lives in routes/run.ts):
//   fullyAutomated -> AUTO_STATE_CODES (worker AI captcha + UPI pipeline)
//   scripted       -> any registered state whose code is in the env
//                     allow-list SCRIPTED_BORDER_TAX_STATES
//                     (config.scriptedStates); empty = scripted disabled.

import type { StateValidator } from "./types";
import { config } from "../config";
import { upValidator } from "./states/up";
import { hrValidator } from "./states/hr";
import { mpValidator } from "./states/mp";
import { pbValidator } from "./states/pb";
import { hpValidator } from "./states/hp";
import { ukValidator } from "./states/uk";
import { tnValidator } from "./states/tn";
import { brValidator } from "./states/br";

const VALIDATORS: StateValidator[] = [
    upValidator,
    hrValidator,
    mpValidator,
    pbValidator,
    hpValidator,
    ukValidator,
    tnValidator,
    brValidator,
];

// States the worker can run END TO END (AI captcha + UPI). Everything else
// registered above is scripted-only (form-fill -> human handover).
export const AUTO_STATE_CODES: ReadonlySet<string> = new Set([
    "UP",
    "HR",
    "MP",
    "PB",
    "HP",
]);

const ALIASES: Record<string, string> = {
    "U.P.": "UP",
    "UTTARPRADESH": "UP",
    "H.P.": "HP",
    "HIMACHALPRADESH": "HP",
    "U.K.": "UK",
    "UTTARAKHAND": "UK",
    "T.N.": "TN",
    "TAMILNADU": "TN",
};

function normalizeKey(input: string): string {
    return input.trim().toUpperCase();
}

export function getStateValidator(
    input: string | undefined | null,
): StateValidator | undefined {
    if (!input) return undefined;
    const key = ALIASES[normalizeKey(input)] ?? normalizeKey(input);
    return VALIDATORS.find((v) => v.code === key || v.name.toUpperCase() === key);
}

/** True when the state can run path=fullyAutomated. */
export function isAutoCapable(code: string): boolean {
    return AUTO_STATE_CODES.has(normalizeKey(code));
}

/** True when the state can run path=scripted RIGHT NOW (env allow-list). */
export function isScriptedEnabled(code: string): boolean {
    return config.scriptedStates.has(normalizeKey(code));
}

/**
 * Capability listing for /api/health and the dashboard. `code` + `name` keep
 * the pre-scripted shape; `auto` and `scriptedEnabled` are additive.
 */
export function supportedStates(): Array<{
    code: string;
    name: string;
    auto: boolean;
    scriptedEnabled: boolean;
}> {
    return VALIDATORS.map((v) => ({
        code: v.code,
        name: v.name,
        auto: AUTO_STATE_CODES.has(v.code),
        scriptedEnabled: config.scriptedStates.has(v.code),
    }));
}
