// Dashboard smoke test: boots the FastAPI backend and the built Next.js app,
// then verifies the UI proxy delivers real /api/v1 data (v3.1 §33).
import { spawn } from "node:child_process";
import http from "node:http";

function wait(port, timeoutMs = 60000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/", timeout: 2000 }, (res) => {
        res.resume();
        resolve();
      });
      req.on("error", () => {
        if (Date.now() - started > timeoutMs) reject(new Error(`port ${port} timeout`));
        else setTimeout(tryOnce, 500);
      });
      req.on("timeout", () => req.destroy());
    };
    tryOnce();
  });
}

function get(port, path, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: "127.0.0.1", port, path, headers }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve({ status: res.statusCode, body }));
    });
    req.on("error", reject);
  });
}

function post(port, path, data, headers = {}) {
  // headers: optional auth headers
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(data);
    const req = http.request(
      { host: "127.0.0.1", port, path, method: "POST", headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload), ...headers } },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () => resolve({ status: res.statusCode, body }));
      },
    );
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

const failures = [];
const check = (name, cond, detail = "") => {
  console.log(`${cond ? "PASS" : "FAIL"} — ${name}${cond ? "" : ` (${detail})`}`);
  if (!cond) failures.push(name);
};

let backend, frontend;
try {
  backend = spawn("uv", ["run", "uvicorn", "agent_system.api.main:app", "--port", "8123"], {
    cwd: "../backend",
    stdio: "pipe",
  });
  frontend = spawn("npx", ["next", "start", "-p", "3100"], {
    stdio: "pipe",
    env: { ...process.env, AGENT_SYSTEM_API_URL: "http://127.0.0.1:8123" },
  });

  await wait(8123);
  await wait(3100);

  // 1. UI shell renders
  const shell = await get(3100, "/");
  check("dashboard shell renders", shell.status === 200 && shell.body.includes("Agent System"));

  // 2. Real API through proxy: health
  const health = await get(3100, "/api/v1/health");
  check(
    "proxy delivers real /api/v1/health",
    health.status === 200 && JSON.parse(health.body).status === "ok",
    `status=${health.status} body=${health.body.slice(0, 100)}`,
  );

  // 3. Auth: unauthenticated write is rejected, then mint a token via the
  // bootstrap-secret contract and use it for authenticated calls.
  const anon = await post(3100, "/api/v1/sessions", { goal: "should be rejected" });
  check("unauthenticated write rejected (401)", anon.status === 401, `status=${anon.status}`);
  const tokenRes = await post(3100, "/api/v1/auth/token", {
    session_secret: "dev-only-secret-change-me",
  });
  check("auth token minted via bootstrap secret", tokenRes.status === 200 && !!JSON.parse(tokenRes.body).token, `status=${tokenRes.status}`);
  const token = JSON.parse(tokenRes.body).token;
  const authHeaders = { Authorization: `Bearer ${token}` };

  // 3b. Write path: create a session through the proxy
  const created = await post(3100, "/api/v1/sessions", { goal: "dashboard smoke test" }, authHeaders);
  check(
    "proxy delivers real POST /api/v1/sessions",
    created.status === 201 && JSON.parse(created.body).id.startsWith("ses_"),
    `status=${created.status} body=${created.body.slice(0, 100)}`,
  );
  const sessionId = JSON.parse(created.body).id;

  // 4. Read-back: sessions list contains the new session
  const sessions = await get(3100, "/api/v1/sessions", authHeaders);
  check(
    "created session visible via /api/v1/sessions",
    sessions.status === 200 && JSON.parse(sessions.body).some((s) => s.id === sessionId),
  );

  // 5. Events stream has the session.created event
  const events = await get(3100, "/api/v1/events?limit=10", authHeaders);
  const eventList = JSON.parse(events.body);
  check(
    "canonical events flowing (session.created present)",
    events.status === 200 && eventList.some((e) => e.type === "session.created"),
    `n=${eventList.length}`,
  );

  // 6. Approvals endpoint usable (list, empty OK)
  const approvals = await get(3100, "/api/v1/approvals?pending_only=true", authHeaders);
  check("approvals endpoint reachable via proxy", approvals.status === 200);

  // 7. Chat tab renders
  const chat = await get(3100, "/chat");
  check("chat tab renders", chat.status === 200);

  // 8. Kanban tab renders
  const kanban = await get(3100, "/kanban");
  check("kanban tab renders", kanban.status === 200);

  // 9. Audit tab renders
  const audit = await get(3100, "/audit");
  check("audit tab renders", audit.status === 200);

  // 10. Theme toggle: markup + cookie-persisted toggle + OS-default script.
  check("theme toggle rendered", shell.body.includes("theme-toggle"), "no .theme-toggle in /");
  check(
    "theme init script present (cookie || prefers-color-scheme)",
    shell.body.includes("bob-theme") && shell.body.includes("prefers-color-scheme"),
    "no theme init in /",
  );

  // 11. Headless-browser theme round-trip when playwright-core is available
  // (CI installs it); otherwise the HTTP markup checks above stand in.
  let browserChecked = false;
  try {
    const { chromium } = await import("playwright-core").catch(() => ({}));
    if (chromium) {
      const browser = await chromium.launch();
      try {
        const page = await browser.newPage();
        await page.goto("http://127.0.0.1:3100/", { waitUntil: "domcontentloaded" });
        const toggle = page.getByRole("button", { name: /theme/i });
        await toggle.waitFor({ timeout: 10000 });
        const before = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
        await toggle.click();
        const after = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
        const cookies = await page.context().cookies();
        check(
          "theme toggle flips data-theme in a real browser",
          before !== after && (after === "light" || after === "dark"),
          `before=${before} after=${after}`,
        );
        check(
          "theme choice persisted in bob-theme cookie",
          cookies.some((c) => c.name === "bob-theme" && (c.value === "light" || c.value === "dark")),
          JSON.stringify(cookies.map((c) => c.name)),
        );
        // Narrow viewport: drawer affordance exists for the collapsed sidebar.
        await page.setViewportSize({ width: 375, height: 800 });
        const menuVisible = await page.getByRole("button", { name: /navigation/i }).first().isVisible();
        check("dashboard usable at 375px (nav drawer affordance)", menuVisible);
        browserChecked = true;
      } finally {
        await browser.close();
      }
    }
  } catch (err) {
    check("headless browser theme round-trip", false, String(err).slice(0, 200));
  }
  if (!browserChecked) console.log("SKIP — headless browser theme round-trip (playwright-core not installed)");
} catch (err) {
  console.error("SMOKE SETUP ERROR:", err);
  failures.push("setup");
} finally {
  try {
    if (frontend) frontend.kill("SIGKILL");
    if (backend) backend.kill("SIGKILL");
  } catch {}
  // give children a moment to die, then force-exit (spawned shells may linger)
  await new Promise((r) => setTimeout(r, 300));
  process.exit(failures.length ? 1 : 0);
}

if (failures.length) {
  console.error(`\n${failures.length} smoke checks failed`);
  process.exit(1);
}
console.log("\nAll dashboard smoke checks passed.");
