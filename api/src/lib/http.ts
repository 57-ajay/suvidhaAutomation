export function corsHeaders(): Record<string, string> {
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Internal-Key",
    };
}

export function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json", ...corsHeaders() },
    });
}

export async function readJson(req: Request): Promise<Record<string, any>> {
    try {
        const body = await req.json();
        return typeof body === "object" && body !== null
            ? (body as Record<string, any>)
            : {};
    } catch {
        return {};
    }
}
