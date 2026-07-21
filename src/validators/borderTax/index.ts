// api/src/validators/borderTax/index.ts
//
// Registry of per-state validators. Add a state by writing its validator file
// and registering it here. Pivot-a ships UP only.

import type { StateValidator } from "../types";
import { upValidator } from "./up";

const REGISTRY: StateValidator[] = [upValidator];

// Map both "UP" and "UTTAR PRADESH" (and a few aliases) to the validator.
const BY_KEY = new Map<string, StateValidator>();
for (const v of REGISTRY) {
    BY_KEY.set(v.code.toUpperCase(), v);
    BY_KEY.set(v.name.toUpperCase(), v);
}
BY_KEY.set("UTTARPRADESH", upValidator);

export function getStateValidator(
    stateOrCode: string | undefined,
): StateValidator | undefined {
    if (!stateOrCode) return undefined;
    return BY_KEY.get(stateOrCode.trim().toUpperCase());
}

export function supportedStates(): string[] {
    return REGISTRY.map((v) => v.code);
}
