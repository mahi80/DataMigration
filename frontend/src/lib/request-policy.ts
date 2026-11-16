export class ConsoleError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
    this.name = "ConsoleError";
  }
}

export function configuredOrigin(raw: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("Invalid console origin configuration"); }
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if ((url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) ||
      url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("Console origin requires HTTPS except for loopback development");
  }
  return url;
}

export function configuredBackend(raw: string): URL {
  let url: URL;
  try { url = new URL(raw); } catch { throw new Error("Invalid backend configuration"); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("Backend configuration requires an HTTP or HTTPS service URL without credentials");
  }
  return url;
}

export function requireSameOrigin(request: Request, origin: string): void {
  if (request.headers.get("origin") !== origin) {
    throw new ConsoleError(403, "This request must come from the configured console origin.");
  }
  const site = request.headers.get("sec-fetch-site");
  if (site && !["same-origin", "none"].includes(site)) {
    throw new ConsoleError(403, "Cross-site requests are not accepted.");
  }
}

export function backendTarget(method: string, path: string[], query: URLSearchParams): string {
  const joined = path.join("/");
  const runId = "[A-Za-z0-9_-]{1,64}";
  const read = /^(me|health|connections|connectors|runs)$/;
  const runRead = new RegExp(`^runs/${runId}(?:/plan)?$`);
  const write = new RegExp(`^(discover|assistant/chat|runs|runs/${runId}/(?:approval|retry))$`);
  if (!((method === "GET" && (read.test(joined) || runRead.test(joined))) ||
        (method === "POST" && write.test(joined)))) {
    throw new ConsoleError(404, "This console operation is not available.");
  }
  const allowed = joined === "runs" && method === "GET" ? new Set(["limit", "offset", "status"]) : new Set<string>();
  const clean = new URLSearchParams();
  for (const [key, value] of query) {
    if (!allowed.has(key) || clean.has(key) || value.length > 64) {
      throw new ConsoleError(400, "Unsupported request parameters.");
    }
    clean.set(key, value);
  }
  return joined + (clean.size ? `?${clean.toString()}` : "");
}

export async function readJsonBody(input: Request | Response, maxBytes = 1_000_000): Promise<unknown> {
  if (!input.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
    throw new ConsoleError(415, "A JSON request is required.");
  }
  const reader = input.body?.getReader();
  if (!reader) throw new ConsoleError(400, "A JSON body is required.");
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      length += part.value.byteLength;
      if (length > maxBytes) {
        await reader.cancel();
        throw new ConsoleError(413, "The request exceeds the console size limit.");
      }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(length);
    let position = 0;
    for (const chunk of chunks) { bytes.set(chunk, position); position += chunk.byteLength; }
    try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
    catch { throw new ConsoleError(400, "Invalid JSON content."); }
  } finally {
    reader.releaseLock();
  }
}
