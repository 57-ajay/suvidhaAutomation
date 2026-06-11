// api/src/internal/eligibility.ts
//
// Pre-launch eligibility gate. The PUCC-expired popup (and insurance/fitness
// equivalents) is the single most common reason a run dies four pages deep.
// When vehicle validity data is available from RC lookup, we catch it HERE —
// at /api/run, before a browser slot is ever spent — and cancel cheaply with
// the same message the in-flow popup would have produced.
//
// This ships as a no-op that always passes, with the integration point clearly
// marked. Wire it to your RC/VAHAN validity source and return ineligible with a
// human-readable reason when PUCC / insurance / fitness is expired.

export interface EligibilityResult {
    eligible: boolean;
    reason?: string; // shown to the driver, e.g. "PUCC expired on 12/May/2026 — please renew."
    checked: string[]; // which checks actually ran
}

export async function checkEligibility(
    params: Record<string, string>,
): Promise<EligibilityResult> {
    // TODO(pivot-a): call the RC/VAHAN validity service for params.vehicleNumber
    // and compare PUCC / insurance / fitness expiry against today.
    //
    // Example of the intended shape:
    //   const v = await rcValidity(params.vehicleNumber);
    //   if (v.puccValidUpto && v.puccValidUpto < today)
    //     return { eligible: false, reason: `PUCC expired on ${fmt(v.puccValidUpto)} — please renew.`, checked: ["pucc"] };
    //
    // Until wired, we pass through and rely on the worker's in-flow popup handler
    // (phase 3 / phase 6) as the safety net.
    return { eligible: true, checked: [] };
}
