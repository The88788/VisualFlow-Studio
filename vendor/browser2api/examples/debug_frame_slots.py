"""Dump DOM elements visible after config panel closes — for debugging frame slot detection."""

import asyncio
import logging
from browser2api import BrowserManager, Platform
from browser2api.platforms.flow import FlowCount, FlowOrientation, FlowVideoModel
from browser2api.platforms.flow.client import FlowVideoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def run():
    bm = BrowserManager()
    try:
        context, page = await bm.launch_for_login(Platform.FLOW)
        client = FlowVideoClient(
            page, context,
            model=FlowVideoModel.VEO_3_1_FAST,
            orientation=FlowOrientation.LANDSCAPE,
            count=FlowCount.X1,
        )

        await client._ensure_generation_page()
        await client._setup_video_config()
        # Config panel is now closed — dump what's visible

        result = await page.evaluate("""() => {
            const out = { tabs: [], buttons: [], textNodes: [] };

            // All visible [role="tab"]
            for (const el of document.querySelectorAll('[role="tab"]')) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    out.tabs.push({
                        text: (el.textContent || '').trim().slice(0, 80),
                        x: Math.round(rect.x), y: Math.round(rect.y),
                        w: Math.round(rect.width), h: Math.round(rect.height),
                        ariaSelected: el.getAttribute('aria-selected'),
                        ariaLabel: el.getAttribute('aria-label'),
                    });
                }
            }

            // Visible buttons with interesting text
            for (const el of document.querySelectorAll('button')) {
                const rect = el.getBoundingClientRect();
                const text = (el.textContent || '').trim();
                if (rect.width > 0 && rect.height > 0 && text.length > 0 && text.length < 100) {
                    out.buttons.push({
                        text: text.slice(0, 80),
                        x: Math.round(rect.x), y: Math.round(rect.y),
                        ariaLabel: el.getAttribute('aria-label'),
                    });
                }
            }

            // Find text nodes containing 'swap_horiz'
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while (node = walker.nextNode()) {
                if (node.textContent.includes('swap_horiz')) {
                    const p = node.parentElement;
                    const rect = p ? p.getBoundingClientRect() : null;
                    out.textNodes.push({
                        text: node.textContent.trim().slice(0, 80),
                        tag: p ? p.tagName : '?',
                        role: p ? p.getAttribute('role') : null,
                        x: rect ? Math.round(rect.x) : null,
                        y: rect ? Math.round(rect.y) : null,
                    });
                }
            }

            return out;
        }""")

        print("\n=== VISIBLE [role=tab] ELEMENTS ===")
        for t in result['tabs']:
            print(f"  [{t['x']},{t['y']} {t['w']}x{t['h']}] selected={t['ariaSelected']} aria-label={t['ariaLabel']!r} text={t['text']!r}")

        print("\n=== VISIBLE BUTTONS (first 30) ===")
        for b in result['buttons'][:30]:
            print(f"  [{t['x']},{b['y']}] aria-label={b['ariaLabel']!r} text={b['text']!r}")

        print("\n=== ELEMENTS CONTAINING 'swap_horiz' ===")
        for n in result['textNodes']:
            print(f"  tag={n['tag']} role={n['role']} pos=[{n['x']},{n['y']}] text={n['text']!r}")

    finally:
        await bm.close()


asyncio.run(run())
