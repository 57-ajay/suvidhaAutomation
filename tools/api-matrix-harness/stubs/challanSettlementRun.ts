export async function handleChallanSettlementRun(_p: any, _s: string): Promise<Response> {
  return new Response(JSON.stringify({ ok: true, stub: "challan" }), { status: 200 });
}
