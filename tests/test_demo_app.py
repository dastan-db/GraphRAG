#!/usr/bin/env python3
"""
Playwright script to test the GraphRAG demo app.
Captures screenshots and reports on page load, layout, and Live Demo functionality.
"""
import asyncio
import os
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("Installing playwright...")
    import subprocess
    subprocess.check_call(["pip", "install", "playwright"])
    subprocess.check_call(["playwright", "install", "chromium"])
    from playwright.async_api import async_playwright

URL = "https://graphrag-demo-v2-dev-7474658617709921.aws.databricksapps.com"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"


async def main():
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    report = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1400, "height": 900},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # Capture console errors
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            # Step 1: Load home page
            report.append("=== Step 1: Home Page ===")
            await page.goto(URL, wait_until="networkidle", timeout=30000)
            await page.screenshot(path=SCREENSHOT_DIR / "01-home.png")
            report.append("Screenshot: 01-home.png")

            # Check sidebar
            sidebar = await page.query_selector('[style*="position: fixed"]')
            nav_home = await page.query_selector('a[href="/"]')
            report.append(f"Sidebar present: {sidebar is not None}")
            report.append(f"Home nav link present: {nav_home is not None}")

            # Check content
            graphrag_title = await page.query_selector('text=GraphRAG')
            report.append(f"GraphRAG title visible: {graphrag_title is not None}")

            # Step 2: How It Works
            report.append("\n=== Step 2: How It Works ===")
            await page.click('a[href="/how-it-works"]')
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            await page.screenshot(path=SCREENSHOT_DIR / "02-how-it-works.png")
            report.append("Screenshot: 02-how-it-works.png")
            how_content = await page.query_selector('text=How It Works')
            report.append(f"How It Works page loaded: {how_content is not None}")

            # Step 3: Architecture
            report.append("\n=== Step 3: Architecture ===")
            await page.click('a[href="/architecture"]')
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            await page.screenshot(path=SCREENSHOT_DIR / "03-architecture.png")
            report.append("Screenshot: 03-architecture.png")
            arch_content = await page.query_selector('text=Architecture')
            report.append(f"Architecture page loaded: {arch_content is not None}")

            # Step 4: Live Demo
            report.append("\n=== Step 4: Live Demo ===")
            await page.click('a[href="/live-demo"]')
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            await page.screenshot(path=SCREENSHOT_DIR / "04-live-demo-initial.png")
            report.append("Screenshot: 04-live-demo-initial.png")

            live_demo_title = await page.query_selector('text=Live Demo')
            report.append(f"Live Demo page loaded: {live_demo_title is not None}")

            # Step 5: Click example question
            report.append("\n=== Step 5: Example Question (How is Ruth connected to Jesus?) ===")
            example_btn = await page.query_selector('button:has-text("How is Ruth connected to Jesus?")')
            if example_btn:
                await example_btn.click()
                report.append("Clicked example question button")
                # Wait for response (agent may take a few seconds)
                await asyncio.sleep(8)
                await page.screenshot(path=SCREENSHOT_DIR / "05-live-demo-response.png")
                report.append("Screenshot: 05-live-demo-response.png")

                # Check for response in chat
                chat_history = await page.query_selector('#chat-history')
                chat_text = await chat_history.inner_text() if chat_history else ""
                report.append(f"Chat has content: {len(chat_text) > 50}")

                # Check provenance panel
                prov_panel = await page.query_selector('#provenance-panel')
                prov_text = await prov_panel.inner_text() if prov_panel else ""
                has_path = "Traced Path" in prov_text or "path" in prov_text.lower()
                has_sources = "Source" in prov_text or "citation" in prov_text.lower()
                has_grounding = "Grounding" in prov_text or "grounded" in prov_text.lower()
                report.append(f"Provenance shows path: {has_path}")
                report.append(f"Provenance shows sources: {has_sources}")
                report.append(f"Provenance shows grounding: {has_grounding}")
            else:
                report.append("ERROR: Example button not found")

            # Step 6: Apply to Business
            report.append("\n=== Step 6: Apply to Business ===")
            await page.click('a[href="/apply"]')
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            await page.screenshot(path=SCREENSHOT_DIR / "06-apply.png")
            report.append("Screenshot: 06-apply.png")
            apply_content = await page.query_selector('text=Apply to Business')
            report.append(f"Apply to Business page loaded: {apply_content is not None}")

            # Back to Home
            report.append("\n=== Step 7: Back to Home ===")
            await page.click('a[href="/"]')
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            await page.screenshot(path=SCREENSHOT_DIR / "07-home-return.png")
            report.append("Screenshot: 07-home-return.png")

        except Exception as e:
            report.append(f"\nERROR during test: {e}")
            try:
                await page.screenshot(path=SCREENSHOT_DIR / "error-state.png")
                report.append("Error screenshot saved: error-state.png")
            except Exception:
                pass

        # Console errors
        if console_errors:
            report.append(f"\nConsole errors ({len(console_errors)}):")
            for err in console_errors[:10]:
                report.append(f"  - {err[:200]}")

        await browser.close()

    # Print report
    print("\n".join(report))
    return report


if __name__ == "__main__":
    asyncio.run(main())
