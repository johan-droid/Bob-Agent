import { NextRequest, NextResponse } from "next/server";
import fs from "fs";
import path from "path";

/**
 * Runtime proxy to the FastAPI backend (v3.1 §33: the dashboard consumes
 * real /api/v1 contracts only).
 *
 * Automatically manages session auth tokens using AGENT_BOOTSTRAP_SECRET
 * so the frontend UI connects seamlessly without manual token bootstrapping.
 */

const BACKEND =
  process.env.AGENT_SYSTEM_API_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";

let cachedToken: string | null = null;
let lastTokenFetch = 0;

function getBootstrapSecret(): string {
  if (process.env.AGENT_BOOTSTRAP_SECRET) {
    return process.env.AGENT_BOOTSTRAP_SECRET;
  }
  const candidatePaths = [
    path.resolve(process.cwd(), "../backend/.env.local"),
    path.resolve(process.cwd(), "backend/.env.local"),
    path.resolve(process.cwd(), ".env.local"),
  ];
  for (const p of candidatePaths) {
    if (fs.existsSync(p)) {
      try {
        const content = fs.readFileSync(p, "utf-8");
        for (const line of content.split("\n")) {
          const trimmed = line.trim();
          if (trimmed.startsWith("AGENT_BOOTSTRAP_SECRET=")) {
            return trimmed.split("=")[1].trim();
          }
        }
      } catch (_) {}
    }
  }
  return "dev-only-secret-change-me";
}

async function getAuthToken(forceRefresh = false): Promise<string | null> {
  const now = Date.now();
  if (!forceRefresh && cachedToken && now - lastTokenFetch < 3600_000) {
    return cachedToken;
  }
  const secret = getBootstrapSecret();
  try {
    const res = await fetch(`${BACKEND}/api/v1/auth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_secret: secret }),
    });
    if (res.ok) {
      const data = (await res.json()) as { token: string };
      cachedToken = data.token;
      lastTokenFetch = now;
      return cachedToken;
    }
  } catch (_) {}
  return null;
}

type Ctx = { params: Promise<{ path: string[] }> };

async function proxy(req: NextRequest, ctx: Ctx): Promise<NextResponse> {
  const { path: pathSegments } = await ctx.params;
  const target = `${BACKEND}/api/v1/${pathSegments.join("/")}${req.nextUrl.search}`;

  const headers = new Headers();
  for (const key of ["content-type", "accept"]) {
    const value = req.headers.get(key);
    if (value) headers.set(key, value);
  }

  // Inject authentication header if not explicitly provided
  const userAuth = req.headers.get("authorization");
  if (userAuth) {
    headers.set("authorization", userAuth);
  } else {
    const token = await getAuthToken();
    if (token) {
      headers.set("authorization", `Bearer ${token}`);
    }
  }

  let bodyBuffer: ArrayBuffer | undefined = undefined;
  if (!["GET", "HEAD"].includes(req.method)) {
    bodyBuffer = await req.arrayBuffer();
  }

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: "no-store",
    body: bodyBuffer,
  };

  try {
    let backendRes = await fetch(target, init);

    // If 401 Unauthorized, refresh the cached token and retry once
    if (backendRes.status === 401 && !userAuth) {
      const freshToken = await getAuthToken(true);
      if (freshToken) {
        headers.set("authorization", `Bearer ${freshToken}`);
        backendRes = await fetch(target, { ...init, headers });
      }
    }

    const resHeaders = new Headers();
    backendRes.headers.forEach((val, key) => {
      resHeaders.set(key, val);
    });

    return new NextResponse(backendRes.body, {
      status: backendRes.status,
      headers: resHeaders,
    });
  } catch (err) {
    return NextResponse.json(
      { detail: `backend unreachable: ${(err as Error).message}` },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
