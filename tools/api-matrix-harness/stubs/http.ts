export function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}
export async function readJson(req: any): Promise<any> { return req.__body ?? {}; }
