export async function setAgentStatus(opts: any): Promise<string> {
  ((globalThis as any).__cap.statusWrites ||= []).push(opts);
  return opts.to;
}
