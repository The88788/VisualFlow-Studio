"""Standalone Veo API test — no prompts, just run it."""

import asyncio
import json
import os
import sys
import time
import uuid

import httpx
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright

# ── Config (edit here) ────────────────────────────────────────────────────────
AT = os.getenv("VEOAPI_TEST_ACCESS_TOKEN", "")
CAPSOLVER_KEY = os.getenv("VEOAPI_TEST_CAPSOLVER_KEY", "")
CAPMONSTER_KEY = os.getenv("VEOAPI_TEST_CAPMONSTER_KEY", "")
NEXTCAPTCHA_KEY = os.getenv("VEOAPI_TEST_NEXTCAPTCHA_KEY", "")
PROMPT = "котик"
MODEL_KEY = "veo_3_1_t2v_fast_ultra_relaxed"
PROXY = ""

# ── Constants ─────────────────────────────────────────────────────────────────
VEO_BASE_URL = "https://aisandbox-pa.googleapis.com"
VEO_RECAPTCHA_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
CAPSOLVER_URL = "https://api.capsolver.com"
CAPMONSTER_URL = "https://api.capmonster.cloud"
NEXTCAPTCHA_URL = "https://api.nextcaptcha.com"
MAX_CAPTCHA_ATTEMPTS = 15


async def solve_captcha() -> str | None:
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            print(f"  [capsolver] attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
            try:
                r = await client.post(
                    f"{CAPSOLVER_URL}/createTask",
                    json={
                        "clientKey": CAPSOLVER_KEY,
                        "task": {
                            "type": "ReCaptchaV3EnterpriseTask" if PROXY else "ReCaptchaV3EnterpriseTaskProxyLess",
                            "websiteURL": "https://labs.google/",
                            "websiteKey": VEO_RECAPTCHA_KEY,
                            "pageAction": "VIDEO_GENERATION",
                            "minScore": 0.9,
                            **( {"proxy": PROXY.rstrip("/")} if PROXY else {} ),
                        },
                    },
                )
                data = r.json()
                if data.get("errorId", 0) != 0:
                    print(f"  [capsolver] error: {data.get('errorDescription')}")
                    await asyncio.sleep(2)
                    continue

                task_id = data["taskId"]
                print(f"  [capsolver] task_id={task_id}, polling...")

                for _ in range(60):
                    await asyncio.sleep(2)
                    res = await client.post(
                        f"{CAPSOLVER_URL}/getTaskResult",
                        json={"clientKey": CAPSOLVER_KEY, "taskId": task_id},
                    )
                    res_data = res.json()
                    status = res_data.get("status")
                    if status == "ready":
                        token = res_data.get("solution", {}).get("gRecaptchaResponse")
                        if token:
                            print(f"  [capsolver] solved! token[:40]={token[:40]}...")
                            return token, None
                        break
                    if status == "failed":
                        print("  [capsolver] task failed")
                        break
                    print(f"  [capsolver] status={status}")

            except Exception as e:
                print(f"  [capsolver] exception: {e}")
                await asyncio.sleep(2)

    print("[capsolver] all attempts exhausted")
    return None, None


def _parse_proxy(proxy_url: str) -> dict:
    """Parse proxy URL into CapMonster separate fields."""
    # http://user:pass@ip:port/  →  {proxyType, proxyAddress, proxyPort, proxyLogin, proxyPassword}
    import re
    m = re.match(r"(\w+)://([^:]+):([^@]+)@([^:]+):(\d+)", proxy_url)
    if not m:
        return {}
    return {
        "proxyType": m.group(1),
        "proxyAddress": m.group(4),
        "proxyPort": int(m.group(5)),
        "proxyLogin": m.group(2),
        "proxyPassword": m.group(3),
    }


async def solve_captcha_capmonster() -> tuple[str, str] | tuple[None, None]:
    """Solve reCAPTCHA v3 Enterprise via CapMonster Cloud.
    Returns (token, user_agent) — user_agent may be None if not provided.
    """
    task_type = "RecaptchaV3EnterpriseTaskProxyless"

    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            print(f"  [capmonster] attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
            try:
                task: dict = {
                    "type": task_type,
                    "websiteURL": "https://labs.google/",
                    "websiteKey": VEO_RECAPTCHA_KEY,
                    "pageAction": "VIDEO_GENERATION",
                    "minScore": 0.9,
                }
                r = await client.post(
                    f"{CAPMONSTER_URL}/createTask",
                    json={"clientKey": CAPMONSTER_KEY, "task": task},
                )
                data = r.json()
                if data.get("errorId", 0) != 0:
                    print(f"  [capmonster] error: {data.get('errorDescription') or data.get('errorCode')}")
                    await asyncio.sleep(2)
                    continue

                task_id = data["taskId"]
                print(f"  [capmonster] task_id={task_id}, polling...")

                for _ in range(60):
                    await asyncio.sleep(2)
                    res = await client.post(
                        f"{CAPMONSTER_URL}/getTaskResult",
                        json={"clientKey": CAPMONSTER_KEY, "taskId": task_id},
                    )
                    res_data = res.json()
                    status = res_data.get("status")
                    if status == "ready":
                        solution = res_data.get("solution", {})
                        token = solution.get("gRecaptchaResponse")
                        ua = solution.get("userAgent")
                        if token:
                            print(f"  [capmonster] solved! token[:40]={token[:40]}...")
                            if ua:
                                print(f"  [capmonster] userAgent={ua}")
                            return token, ua
                        break
                    if status == "failed":
                        print("  [capmonster] task failed")
                        break
                    print(f"  [capmonster] status={status}")

            except Exception as e:
                print(f"  [capmonster] exception: {e}")
                await asyncio.sleep(2)

    print("[capmonster] all attempts exhausted")
    return None, None


async def solve_captcha_nextcaptcha() -> tuple[str, str] | tuple[None, None]:
    """Solve reCAPTCHA v3 Enterprise via NextCaptcha (ReCaptchaV3HSTaskProxyLess — high-score).
    Returns (token, None).
    """
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
            print(f"  [nextcaptcha] attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")
            try:
                r = await client.post(
                    f"{NEXTCAPTCHA_URL}/createTask",
                    json={
                        "clientKey": NEXTCAPTCHA_KEY,
                        "task": {
                            "type": "ReCaptchaV3HSTaskProxyLess",
                            "websiteURL": "https://labs.google/",
                            "websiteKey": VEO_RECAPTCHA_KEY,
                            "pageAction": "VIDEO_GENERATION",
                        },
                    },
                )
                data = r.json()
                if data.get("errorId", 0) != 0:
                    print(f"  [nextcaptcha] error: {data.get('errorDescription') or data.get('errorCode')}")
                    await asyncio.sleep(2)
                    continue

                task_id = data["taskId"]
                print(f"  [nextcaptcha] task_id={task_id}, polling...")

                for _ in range(60):
                    await asyncio.sleep(2)
                    res = await client.post(
                        f"{NEXTCAPTCHA_URL}/getTaskResult",
                        json={"clientKey": NEXTCAPTCHA_KEY, "taskId": task_id},
                    )
                    res_data = res.json()
                    status = res_data.get("status")
                    if status == "ready":
                        token = res_data.get("solution", {}).get("gRecaptchaResponse")
                        if token:
                            print(f"  [nextcaptcha] solved! token[:40]={token[:40]}...")
                            return token, None
                        break
                    if status == "failed":
                        print("  [nextcaptcha] task failed")
                        break
                    print(f"  [nextcaptcha] status={status}")

            except Exception as e:
                print(f"  [nextcaptcha] exception: {e}")
                await asyncio.sleep(2)

    print("[nextcaptcha] all attempts exhausted")
    return None, None


async def solve_captcha_browser() -> tuple[str, str] | tuple[None, None]:
    """Solve reCAPTCHA v3 Enterprise via Playwright browser (same approach as flow2api).

    Intercepts labs.google/fx/api/auth/providers and serves a fake HTML that only
    loads enterprise.js, then executes grecaptcha.enterprise.execute() in-page.
    """
    PAGE_URL = "https://labs.google/fx/api/auth/providers"
    SITE_KEY = VEO_RECAPTCHA_KEY
    ACTION = "VIDEO_GENERATION"
    SETTLE_SECONDS = 2

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-zygote",
                "--disable-infobars",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--window-size=1280,800",
                "--window-position=-32000,-32000",  # off-screen on Windows
                "--start-minimized",
            ],
        )
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-US")
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            # Intercept the target URL and serve minimal HTML that only loads enterprise.js.
            # All google/gstatic/recaptcha requests pass through; everything else is blocked.
            async def handle_route(route):
                if route.request.url.rstrip("/") == PAGE_URL.rstrip("/"):
                    html = f"""<html><head><script>
                    (() => {{
                        const urls = [
                            'https://www.google.com/recaptcha/enterprise.js?render={SITE_KEY}',
                            'https://www.recaptcha.net/recaptcha/enterprise.js?render={SITE_KEY}'
                        ];
                        const loadScript = (index) => {{
                            if (index >= urls.length) return;
                            const script = document.createElement('script');
                            script.src = urls[index];
                            script.async = true;
                            script.onerror = () => loadScript(index + 1);
                            document.head.appendChild(script);
                        }};
                        loadScript(0);
                    }})();
                    </script></head><body></body></html>"""
                    await route.fulfill(status=200, content_type="text/html", body=html)
                elif any(d in route.request.url for d in ["google.com", "gstatic.com", "recaptcha.net"]):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", handle_route)

            try:
                await page.goto(PAGE_URL, wait_until="load", timeout=15000)
            except Exception as e:
                print(f"  [browser] goto failed: {e}")
                return None, None

            try:
                await page.wait_for_function("typeof grecaptcha !== 'undefined'", timeout=10000)
            except Exception as e:
                print(f"  [browser] grecaptcha not ready: {e}")
                return None, None

            print("  [browser] grecaptcha ready, executing...")

            token = await asyncio.wait_for(
                page.evaluate(f"""
                    (actionName) => {{
                        return new Promise((resolve, reject) => {{
                            const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                            grecaptcha.enterprise.execute('{SITE_KEY}', {{action: actionName}})
                                .then(t => {{ clearTimeout(timeout); resolve(t); }})
                                .catch(e => {{ clearTimeout(timeout); reject(e); }});
                        }});
                    }}
                """, ACTION),
                timeout=30,
            )

            # Read actual UA from the browser
            ua = await page.evaluate("navigator.userAgent")
            print(f"  [browser] token[:40]={token[:40]}...")
            print(f"  [browser] userAgent={ua}")

            if SETTLE_SECONDS > 0:
                await asyncio.sleep(SETTLE_SECONDS)

            return token, ua

        except Exception as e:
            print(f"  [browser] exception: {e}")
            return None, None
        finally:
            await browser.close()


async def send_veo_request(recaptcha_token: str | None, user_agent: str | None = None) -> dict:
    project_id = str(uuid.uuid4())
    batch_id = str(uuid.uuid4())
    session_id = f";{int(time.time() * 1000)}"

    headers = {
        "accept": "*/*",
        "accept-language": "ru,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
        "authorization": f"Bearer {AT}",
        "content-type": "text/plain;charset=UTF-8",
        "origin": "https://labs.google",
        "referer": "https://labs.google/",
        "user-agent": user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    payload: dict = {
        "mediaGenerationContext": {"batchId": batch_id},
        "clientContext": {
            "projectId": project_id,
            "tool": "PINHOLE",
            "userPaygateTier": "PAYGATE_TIER_TWO",
            "sessionId": session_id,
        },
        "requests": [
            {
                "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                "seed": 11100,
                "textInput": {"structuredPrompt": {"parts": [{"text": PROMPT}]}},
                "videoModelKey": MODEL_KEY,
                "metadata": {},
            }
        ],
        "useV2ModelConfig": True,
    }

    if recaptcha_token:
        payload["clientContext"]["recaptchaContext"] = {
            "token": recaptcha_token,
            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
        }

    url = f"{VEO_BASE_URL}/v1/video:batchAsyncGenerateVideoText"
    print(f"\n[veo] POST {url}")
    print(f"[veo] project_id={project_id}")
    print(f"[veo] model={MODEL_KEY}, captcha={'yes' if recaptcha_token else 'NO'}")

    async with AsyncSession(impersonate="chrome124", timeout=120, proxy=PROXY) as session:
        response = await session.post(
            url,
            headers=headers,
            data=json.dumps(payload, separators=(",", ":")),
        )

    print(f"\n[veo] HTTP {response.status_code}")
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:2000]}


async def run_benchmark(name: str, solver_fn):
    """Run up to MAX_CAPTCHA_ATTEMPTS, stop on first non-403."""
    success = 0
    fail = 0
    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        print(f"\n[{name}] attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}")
        result_tuple = await solver_fn()
        if isinstance(result_tuple, tuple):
            token, ua = result_tuple
        else:
            token, ua = result_tuple, None

        result = await send_veo_request(token, ua)
        code = result.get("error", {}).get("code")
        status_str = result.get("error", {}).get("details", [{}])[0].get("reason", "OK") if code else "OK"
        print(f"  → HTTP {200 if not code else code} | {status_str}")

        if not code:
            success += 1
            print(json.dumps(result.get("operations", result), indent=2, ensure_ascii=False))
            break
        fail += 1

    print(f"\n[{name}] done: {success} success, {fail} fail out of {attempt} attempts")
    return success > 0


async def main():
    missing = [
        name
        for name, value in (
            ("VEOAPI_TEST_ACCESS_TOKEN", AT),
            ("VEOAPI_TEST_CAPSOLVER_KEY", CAPSOLVER_KEY),
            ("VEOAPI_TEST_CAPMONSTER_KEY", CAPMONSTER_KEY),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    print("\n" + "="*60)
    print("BENCHMARK: Capsolver vs CapMonster vs NextCaptcha")
    print("="*60)

    results = {}

    # 1. Тест CapSolver
    print("\n>>> TESTING CAPSOLVER <<<")
    cs_success = await run_benchmark("capsolver", solve_captcha)
    results["CapSolver"] = "SUCCESS" if cs_success else "FAILED"

    print("\n" + "-"*30)

    # 2. Тест CapMonster
    print("\n>>> TESTING CAPMONSTER <<<")
    cm_success = await run_benchmark("capmonster", solve_captcha_capmonster)
    results["CapMonster"] = "SUCCESS" if cm_success else "FAILED"

    print("\n" + "-"*30)

    # 3. Тест NextCaptcha
    if NEXTCAPTCHA_KEY:
        print("\n>>> TESTING NEXTCAPTCHA <<<")
        nc_success = await run_benchmark("nextcaptcha", solve_captcha_nextcaptcha)
        results["NextCaptcha"] = "SUCCESS" if nc_success else "FAILED"
    else:
        print("\n>>> NEXTCAPTCHA: skipped (no key) <<<")
        results["NextCaptcha"] = "SKIPPED"

    # Итоговое сравнение
    print("\n" + "="*60)
    print(f"{'SERVICE':<15} | {'RESULT':<10}")
    print("-" * 30)
    for service, status in results.items():
        print(f"{service:<15} | {status:<10}")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(main())
