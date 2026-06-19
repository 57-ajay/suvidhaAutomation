// State registry. Adding a state:
//   1. Write validators/states/<code>.ts exporting a StateValidator.
//   2. Register it in VALIDATORS below.
//   3. Mirror the runner on the worker (tasks/border_tax/states/<code>.py +
//      its registry entry). Nothing else changes.

import type { StateValidator } from "./types";
import { upValidator } from "./states/up";
import { hrValidator } from "./states/hr";
import { mpValidator } from "./states/mp";
import { pbValidator } from "./states/pb";

const VALIDATORS: StateValidator[] = [upValidator, hrValidator, mpValidator, pbValidator];

const ALIASES: Record<string, string> = {
    "U.P.": "UP",
    "UTTARPRADESH": "UP",
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

export function supportedStates(): Array<{ code: string; name: string }> {
    return VALIDATORS.map((v) => ({ code: v.code, name: v.name }));
}
