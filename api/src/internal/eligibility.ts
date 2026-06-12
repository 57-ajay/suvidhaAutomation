// Pre-launch eligibility check. PUCC/insurance/fitness expiry is the #1
// "die four pages deep" failure on the portal — catching it here cancels the
// request before a browser slot is ever spent.
//
// Ships as a pass-through. Wire `checkEligibility` to the RC/VAHAN validity
// source; return { eligible: false, reason } with a driver-readable reason and
// /api/run will mark the request cancelled up front.

export interface EligibilityResult {
    eligible: boolean;
    reason?: string;
}

export async function checkEligibility(
    _params: Record<string, string>,
): Promise<EligibilityResult> {
    return { eligible: true };
}
