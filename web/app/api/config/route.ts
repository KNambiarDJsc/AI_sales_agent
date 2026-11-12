import { connection } from "next/server";

// Where the console's backend (the FastAPI server that places the calls) lives right now.
//
// The backend runs on a machine in India behind a tunnel whose address can change, so
// the address is not baked into the build. Order:
//   1. Edge Config item "backend_url" — the backend's tunnel script updates it whenever
//      the address changes (scripts/start_tunnel.ps1), no redeploy needed.
//   2. BACKEND_URL / NEXT_PUBLIC_API_URL env vars — a fixed address.
//   3. http://127.0.0.1:8000 — local development.
export async function GET() {
  await connection(); // always answer at request time, never from the build
  let apiUrl = "";
  const edgeConfig = process.env.EDGE_CONFIG;
  if (edgeConfig) {
    try {
      const url = new URL(edgeConfig);
      const token = url.searchParams.get("token") ?? "";
      const item = `${url.origin}${url.pathname.replace(/\/$/, "")}/item/backend_url?token=${encodeURIComponent(token)}`;
      const resp = await fetch(item, { cache: "no-store", signal: AbortSignal.timeout(4000) });
      if (resp.ok) {
        const value = await resp.json();
        if (typeof value === "string" && /^https?:\/\//.test(value)) apiUrl = value;
      }
    } catch {
      /* fall through to the env vars */
    }
  }
  apiUrl ||= process.env.BACKEND_URL || process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
  return Response.json({ apiUrl: apiUrl.replace(/\/$/, "") }, { headers: { "Cache-Control": "no-store" } });
}
