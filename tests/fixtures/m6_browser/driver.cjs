#!/usr/bin/env node
/* Run the actual dashboard bundle in real React and Playwright against server.py. */
const { build } = require("esbuild");
const { chromium } = require("playwright");
const { spawn } = require("node:child_process");
const { mkdtemp, rm } = require("node:fs/promises");
const { once } = require("node:events");
const os = require("node:os");
const path = require("node:path");

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) throw new Error(`missing ${name}`);
  return process.argv[index + 1];
}

async function main() {
  const repository = path.resolve(argument("--repository"));
  const python = argument("--python");
  const fixture = path.resolve(__dirname);
  const temporary = await mkdtemp(path.join(os.tmpdir(), "m6-browser-bundle-"));
  const harnessBundle = path.join(temporary, "harness.js");
  let server;
  let browser;
  try {
    await build({
      entryPoints: [path.join(fixture, "harness_entry.js")],
      bundle: true,
      format: "iife",
      platform: "browser",
      target: "es2022",
      nodePaths: process.env.NODE_PATH ? process.env.NODE_PATH.split(path.delimiter) : [],
      outfile: harnessBundle,
    });
    server = spawn(python, [path.join(fixture, "server.py"), "--repository", repository, "--bundle", harnessBundle], {
      cwd: repository,
      env: { ...process.env, HERMES_M0_CLI: "" },
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let stderr = "";
    server.stderr.on("data", chunk => { stderr += chunk; });
    const [first] = await Promise.race([
      once(server.stdout, "data"),
      once(server, "exit").then(([code]) => { throw new Error(`fixture server exited ${code}: ${stderr}`); }),
      new Promise((_, reject) => setTimeout(() => reject(new Error(`fixture server did not start: ${stderr}`)), 15000)),
    ]);
    const launch = { headless: true };
    if (process.env.M6_BROWSER_EXECUTABLE) launch.executablePath = process.env.M6_BROWSER_EXECUTABLE;
    browser = await chromium.launch(launch);
    const page = await browser.newPage();
    const fixtureUrl = JSON.parse(first.toString()).url;
    await page.goto(fixtureUrl, { waitUntil: "networkidle", timeout: 15000 });
    await page.getByRole("heading", { name: "Local First" }).waitFor();
    await page.getByRole("heading", { name: "Configured scope and bounded settings" }).waitFor();
    if (await page.getByLabel("Configured scope").inputValue() !== "fixture-board:anchor") throw new Error("configured scope selector was not rendered");
    await page.getByText("implementer").waitFor();
    await page.getByRole("heading", { name: "Runtime metrics" }).waitFor();
    await page.getByText("Local / paid reviews").waitFor();
    await page.getByText("anchor / finding-1 / review_corrections: net 1 of configured limit 1; remaining 0").waitFor();
    const reactVersion = await page.evaluate(() => window.__M6_HARNESS__.reactVersion);
    if (reactVersion !== "19.2.7") throw new Error(`expected React 19.2.7, got ${reactVersion}`);

    const configurationPostsBeforeBlank = await page.evaluate(() => window.__M6_HARNESS__.apiCalls.filter(call => call.method === "POST" && /\/configuration$/.test(call.url)).length);
    await page.getByLabel("paid capacity budget").fill("");
    await page.getByRole("button", { name: "Save bounded configuration" }).click();
    await page.getByText("paid capacity budget is required.").waitFor();
    const configurationPostsAfterBlank = await page.evaluate(() => window.__M6_HARNESS__.apiCalls.filter(call => call.method === "POST" && /\/configuration$/.test(call.url)).length);
    if (configurationPostsAfterBlank !== configurationPostsBeforeBlank) throw new Error("blank budget attempted a configuration POST");
    const unchangedConfiguration = await (await page.request.get(fixtureUrl + "/api/plugins/local-first-orchestrator/configuration")).json();
    if (unchangedConfiguration.budgets.paid_capacity !== 1) throw new Error("blank budget changed fixture configuration");
    await page.getByLabel("paid capacity budget").fill("0");
    await page.getByLabel("Poll interval (seconds)").fill("");
    await page.getByRole("button", { name: "Save bounded configuration" }).click();
    await page.getByText("Poll interval is required (1–3600 seconds).").waitFor();
    const configurationPostsAfterBlankInterval = await page.evaluate(() => window.__M6_HARNESS__.apiCalls.filter(call => call.method === "POST" && /\/configuration$/.test(call.url)).length);
    if (configurationPostsAfterBlankInterval !== configurationPostsBeforeBlank) throw new Error("blank poll interval attempted a configuration POST");
    await page.getByLabel("Poll interval (seconds)").fill("2");
    await page.getByRole("button", { name: "Save bounded configuration" }).click();
    await page.getByText('"poll_interval_seconds": 2').waitFor();
    const callsAfterConfiguration = await page.evaluate(() => window.__M6_HARNESS__.apiCalls.slice());
    if (!callsAfterConfiguration.some(call => call.method === "POST" && /\/configuration$/.test(call.url) && call.status === 200)) {
      throw new Error("bounded configuration save did not reach the fixture API with HTTP 200");
    }

    await page.getByRole("button", { name: "Pause and stop" }).click();
    await page.getByText('"outcome": "partial"').waitFor();
    await page.getByText("run-1").first().waitFor();
    const callsAfterPartial = await page.evaluate(() => window.__M6_HARNESS__.apiCalls.slice());
    if (!callsAfterPartial.some(call => call.method === "POST" && /\/actions\/stop$/.test(call.url) && call.status === 200)) {
      throw new Error("partial-stop action did not reach the fixture API with HTTP 200");
    }

    const writesBeforeStale = await (await page.request.get(fixtureUrl + "__fixture/writes")).json();
    await page.evaluate(() => {
      const originalFetch = window.fetch;
      window.fetch = (url, init = {}) => {
        if (String(url).endsWith("/actions/pause") && init.method === "POST") {
          const body = JSON.parse(init.body);
          body.expected_observation_digest = "sha256:" + "0".repeat(64);
          return originalFetch(url, { ...init, body: JSON.stringify(body) });
        }
        return originalFetch(url, init);
      };
    });
    await page.getByRole("button", { name: "Pause", exact: true }).click();
    await page.getByText(/409:/).waitFor();
    await page.waitForFunction(() => window.__M6_HARNESS__.apiCalls.some(call => call.method === "POST" && /\/actions\/pause$/.test(call.url) && call.status === 409));
    const pause = page.getByRole("button", { name: "Pause", exact: true });
    if (await pause.isDisabled()) throw new Error("controls remained disabled after stale-error refresh");
    const calls = await page.evaluate(() => window.__M6_HARNESS__.apiCalls.slice());
    const staleIndex = calls.findIndex(call => call.method === "POST" && /\/actions\/pause$/.test(call.url) && call.status === 409);
    if (staleIndex < 0 || !calls.slice(staleIndex + 1).some(call => call.method === "GET" && /\/status$/.test(call.url) && call.status === 200)) {
      throw new Error("stale action did not retain its error after a successful refresh GET");
    }
    const writesAfterStale = await (await page.request.get(fixtureUrl + "__fixture/writes")).json();
    if (JSON.stringify(writesAfterStale) !== JSON.stringify(writesBeforeStale)) {
      throw new Error("stale action mutated fixture board writes");
    }
    if (process.env.M6_BROWSER_SCREENSHOT) await page.screenshot({ path: process.env.M6_BROWSER_SCREENSHOT, fullPage: true });
    console.log(JSON.stringify({
      reactVersion,
      calls: calls.map(({ method, url, status }) => ({ method, url, status })),
    }));
  } finally {
    if (browser) await browser.close();
    if (server && server.exitCode === null) {
      try {
        if (process.platform !== "win32") process.kill(-server.pid, "SIGTERM");
        else server.kill("SIGTERM");
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
      await Promise.race([once(server, "exit"), new Promise(resolve => setTimeout(resolve, 3000))]);
      if (server.exitCode === null) {
        try {
          if (process.platform !== "win32") process.kill(-server.pid, "SIGKILL");
          else server.kill("SIGKILL");
        } catch (error) {
          if (error.code !== "ESRCH") throw error;
        }
      }
    }
    await rm(temporary, { recursive: true, force: true });
  }
}

main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
