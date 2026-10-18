/**
 * Browser-based reCAPTCHA Enterprise solver via Puppeteer.
 * Logic ported from flow2api browser_captcha.py (Playwright headed version).
 *
 * Key points (from flow2api):
 *  - Use www.recaptcha.net when behind proxy (google.com may be blocked)
 *  - Route interception: serve custom HTML with recaptcha script injection
 *  - Navigate to labs.google/fx/api/auth/providers (lightweight API page)
 *  - Block all unnecessary requests (only allow google/gstatic/recaptcha)
 *  - Anti-detection: hide navigator.webdriver, random UA + viewport
 *  - Stealth plugin for fingerprint evasion
 *  - Retry up to 3 times with fresh browser + new fingerprint each attempt
 *
 * Usage:
 *   node browser_captcha.js [--proxy http://user:pass@host:port] [--action VIDEO_GENERATION] [--retries 3]
 *
 * Outputs JSON to stdout:
 *   Success: { "token": "...", "length": N, "attempt": N }
 *   Failure: { "error": "...", "attempts": N }
 */

// Two browser launch backends, picked at runtime via --use-cloakbrowser:
//   1. puppeteer-extra + puppeteer-extra-plugin-stealth — JS-level fingerprint
//      shims. Default and historical path.
//   2. cloakbrowser/puppeteer — drop-in puppeteer launcher that runs the
//      CloakBrowser stealth Chromium binary (58 C++ source-level fingerprint
//      patches). The CloakBrowser README itself recommends Playwright for
//      reCAPTCHA Enterprise (Puppeteer's CDP protocol leaks automation
//      signals it doesn't patch). Using it here is a deliberate compromise:
//      the binary patches still help with canvas/WebGL/audio/fonts/GPU/UA,
//      only the CDP-detection vector remains. If score regresses on prod,
//      next step is to migrate this script to Playwright entirely.
const puppeteer = require("puppeteer-extra");
const StealthPlugin = require("puppeteer-extra-plugin-stealth");

puppeteer.use(StealthPlugin());

// Lazy-required only when --use-cloakbrowser is passed; missing dep should
// not break the default puppeteer-extra path.
let cloakLaunch = null;
function tryLoadCloakBrowser() {
  if (cloakLaunch !== null) return cloakLaunch;
  try {
    cloakLaunch = require("cloakbrowser/puppeteer").launch;
  } catch (e) {
    cloakLaunch = false;
    process.stderr.write(
      `[captcha] cloakbrowser/puppeteer not installed: ${e.message}\n` +
      `[captcha] falling back to puppeteer-extra-stealth\n`
    );
  }
  return cloakLaunch;
}

const SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
const PAGE_URL = "https://labs.google/fx/api/auth/providers";

// UA pool (from flow2api, updated 2026-03-01)
const UA_LIST = [
  // Chrome 136 (stable April 2026)
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
  // Chrome 135
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
  // Chrome 134
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
  // Chrome 133
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
  // Chrome 132
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
  // Edge 136/135
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
];

// Resolution pool (from flow2api)
const RESOLUTIONS = [
  [1920, 1080], [2560, 1440], [1366, 768], [1536, 864],
  [1600, 900], [1280, 720], [1440, 900], [1680, 1050],
  [1280, 800], [1920, 1200], [1280, 1024],
];

function randomChoice(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function parseArgs() {
  const args = process.argv.slice(2);
  const result = {
    proxy: null,
    proxyFallbacks: [],
    action: "VIDEO_GENERATION",
    retries: 3,
    useCloak: false,
  };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--proxy" && args[i + 1]) result.proxy = args[++i];
    if (args[i] === "--proxy-fallbacks" && args[i + 1]) {
      result.proxyFallbacks = args[++i].split(",").map(p => p.trim()).filter(Boolean);
    }
    if (args[i] === "--action" && args[i + 1]) result.action = args[++i];
    if (args[i] === "--retries" && args[i + 1]) result.retries = parseInt(args[++i]) || 3;
    if (args[i] === "--use-cloakbrowser") result.useCloak = true;
  }
  return result;
}

function parseProxy(proxyUrl) {
  try {
    const url = new URL(proxyUrl.includes("://") ? proxyUrl : `http://${proxyUrl}`);
    return {
      server: `http://${url.hostname}:${url.port}`,
      username: decodeURIComponent(url.username || ""),
      password: decodeURIComponent(url.password || ""),
    };
  } catch {
    return null;
  }
}

/**
 * Single attempt: launch fresh browser, get token, close browser.
 * Each attempt gets a new random fingerprint (UA + viewport).
 */
async function singleAttempt(proxy, action, attemptNum, useCloak = false) {
  const hasProxy = !!proxy;

  // Random fingerprint per attempt (flow2api: _refresh_browser_profile)
  const userAgent = randomChoice(UA_LIST);
  const [baseW, baseH] = randomChoice(RESOLUTIONS);
  const viewportHeight = baseH - Math.floor(Math.random() * 80);

  const launchArgs = [
    "--disable-blink-features=AutomationControlled",
    "--disable-quic",
    "--disable-features=UseDnsHttpsSvcb",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-first-run",
    "--no-zygote",
    `--window-size=${baseW},${baseH}`,
    "--disable-infobars",
    "--hide-scrollbars",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
    "--disable-default-apps",
    "--no-default-browser-check",
  ];

  let proxyInfo = null;
  if (proxy) {
    proxyInfo = parseProxy(proxy);
    if (proxyInfo) {
      launchArgs.push(`--proxy-server=${proxyInfo.server}`);
    }
  }

  let browser;
  if (useCloak && tryLoadCloakBrowser()) {
    // CloakBrowser launch options are puppeteer-compatible plus its own
    // proxy/humanize/geoip keys. Pass proxy as a string so the binary
    // picks up inline credentials at the network layer (no separate
    // page.authenticate() needed for HTTP), and forward the same
    // anti-flag set we use elsewhere via `args`.
    const cloakOpts = {
      headless: true,
      args: launchArgs.filter(a => !a.startsWith("--proxy-server=")),
    };
    if (proxy) cloakOpts.proxy = proxy;
    browser = await cloakLaunch(cloakOpts);
  } else {
    browser = await puppeteer.launch({
      headless: "new",
      args: launchArgs,
    });
  }

  try {
    const page = await browser.newPage();

    // Set viewport and user agent
    await page.setViewport({ width: baseW, height: viewportHeight });
    await page.setUserAgent(userAgent);

    // Anti-detection: hide webdriver
    await page.evaluateOnNewDocument(() => {
      Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    });

    // Proxy auth
    if (proxyInfo && proxyInfo.username) {
      await page.authenticate({
        username: proxyInfo.username,
        password: proxyInfo.password,
      });
    }

    // Determine recaptcha host (flow2api: use recaptcha.net when behind proxy)
    const primaryHost = hasProxy
      ? "https://www.recaptcha.net"
      : "https://www.google.com";
    const secondaryHost = hasProxy
      ? "https://www.google.com"
      : "https://www.recaptcha.net";

    // Route interception (flow2api _execute_captcha):
    // Intercept page URL → serve custom HTML with recaptcha injection
    // Allow only google.com, gstatic.com, recaptcha.net — block everything else
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      const url = request.url();

      if (url.replace(/\/$/, "") === PAGE_URL.replace(/\/$/, "")) {
        const html = `<html><head><script>
          (() => {
            const urls = [
              '${primaryHost}/recaptcha/enterprise.js?render=${SITE_KEY}',
              '${secondaryHost}/recaptcha/enterprise.js?render=${SITE_KEY}'
            ];
            const loadScript = (index) => {
              if (index >= urls.length) return;
              const script = document.createElement('script');
              script.src = urls[index];
              script.async = true;
              script.onerror = () => loadScript(index + 1);
              document.head.appendChild(script);
            };
            loadScript(0);
          })();
        </script></head><body></body></html>`;

        request.respond({
          status: 200,
          contentType: "text/html",
          body: html,
        });
      } else if (
        url.includes("google.com") ||
        url.includes("gstatic.com") ||
        url.includes("recaptcha.net")
      ) {
        request.continue();
      } else {
        request.abort();
      }
    });

    // Navigate to the lightweight API page
    await page.goto(PAGE_URL, { waitUntil: "load", timeout: 30000 });

    // Wait for grecaptcha.enterprise to load
    await page.waitForFunction(
      () =>
        typeof grecaptcha !== "undefined" &&
        typeof grecaptcha.enterprise !== "undefined" &&
        typeof grecaptcha.enterprise.execute === "function",
      { timeout: 20000 }
    );

    // Execute reCAPTCHA
    const token = await page.evaluate(
      (siteKey, act) =>
        new Promise((resolve, reject) => {
          const timeout = setTimeout(
            () => reject(new Error("execute timeout")),
            25000
          );
          grecaptcha.enterprise
            .execute(siteKey, { action: act })
            .then((t) => {
              clearTimeout(timeout);
              resolve(t);
            })
            .catch((e) => {
              clearTimeout(timeout);
              reject(e);
            });
        }),
      SITE_KEY,
      action
    );

    if (!token || typeof token !== "string") {
      throw new Error(`Invalid token: ${token}`);
    }

    // Post-solve settle time (flow2api: browser_recaptcha_settle_seconds = 3)
    await new Promise((r) => setTimeout(r, 3000));

    return token;
  } finally {
    await browser.close();
  }
}

/**
 * Main: retry loop with fresh browser + fingerprint each attempt (flow2api pattern).
 * On navigation timeout, rotate proxy: try up to 2 proxyless fallback attempts
 * before counting the overall attempt as failed.
 */
async function solve() {
  const { proxy, proxyFallbacks, action, retries, useCloak } = parseArgs();
  const maxRetries = Math.max(1, Math.min(retries, 10));
  let lastError = null;
  if (useCloak) {
    process.stderr.write("[browser_captcha] backend: cloakbrowser/puppeteer\n");
  }

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      if (attempt > 0) {
        // Wait 1s between retries (flow2api: await asyncio.sleep(1))
        process.stderr.write(
          `[browser_captcha] Attempt ${attempt + 1}/${maxRetries} (retrying after ${lastError})\n`
        );
        await new Promise((r) => setTimeout(r, 1000));
      }

      // On nav timeout: rotate through fallback proxies (up to 2), then fail
      let token = null;
      const proxyQueue = [proxy, ...proxyFallbacks.slice(0, 2)];
      for (let fb = 0; fb < proxyQueue.length; fb++) {
        const attemptProxy = proxyQueue[fb];
        try {
          token = await singleAttempt(attemptProxy, action, attempt, useCloak);
          break; // success
        } catch (err) {
          const msg = err.message || String(err);
          const isNavTimeout = msg.includes("Navigation timeout") || msg.includes("net::ERR_");
          if (isNavTimeout && fb < proxyQueue.length - 1) {
            process.stderr.write(
              `[browser_captcha] Nav timeout with proxy=${attemptProxy || "none"}, rotating to fallback proxy ${fb + 1}/${proxyQueue.length - 1}\n`
            );
            await new Promise((r) => setTimeout(r, 500));
            continue;
          }
          throw err; // re-throw if not nav timeout or all proxies exhausted
        }
      }

      process.stdout.write(
        JSON.stringify({ token, length: token.length, attempt: attempt + 1 })
      );
      return;
    } catch (err) {
      lastError = err.message || String(err);
      process.stderr.write(
        `[browser_captcha] Attempt ${attempt + 1}/${maxRetries} failed: ${lastError}\n`
      );
    }
  }

  // All attempts failed
  process.stdout.write(
    JSON.stringify({ error: lastError, attempts: maxRetries })
  );
  process.exit(1);
}

solve().catch((err) => {
  process.stdout.write(
    JSON.stringify({ error: err.message || String(err), attempts: 0 })
  );
  process.exit(1);
});
