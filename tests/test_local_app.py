#!/usr/bin/env python3
"""Playwright E2E test for the GraphRAG demo app running locally in mock mode."""
import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

URL = os.getenv("TEST_APP_URL", "http://localhost:8000")
USE_MOCK = os.getenv("USE_MOCK_BACKEND", "true").lower() == "true"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"

PASS = 0
FAIL = 0


def check(label: str, condition: bool):
    global PASS, FAIL
    status = "PASS" if condition else "FAIL"
    if condition:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{status}] {label}")


async def main():
    SCREENSHOT_DIR.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # ── Step 1: Home page ──
        print("\n=== Step 1: Home Page ===")
        resp = await page.goto(URL, wait_until="networkidle", timeout=15000)
        check("Home page HTTP 200", resp.status == 200)
        await page.screenshot(path=SCREENSHOT_DIR / "local-01-home.png")

        sidebar = await page.query_selector('[style*="position: fixed"]')
        check("Sidebar present", sidebar is not None)

        graphrag_text = await page.query_selector("text=GraphRAG")
        check("GraphRAG branding visible", graphrag_text is not None)

        auditable_text = await page.query_selector("text=Auditable AI")
        check("'Auditable AI' tagline visible", auditable_text is not None)

        understand_label = await page.query_selector("text=UNDERSTAND")
        experience_label = await page.query_selector("text=EXPERIENCE")
        adopt_label = await page.query_selector("text=ADOPT")
        check("Sidebar labels are distinct (Understand/Experience/Adopt)",
              understand_label is not None and experience_label is not None and adopt_label is not None)

        # ── Step 2: How It Works ──
        print("\n=== Step 2: How It Works ===")
        link = await page.query_selector('a[href="/how-it-works"]')
        check("How It Works nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-02-how-it-works.png")
            content = await page.query_selector("text=How It Works")
            check("How It Works page rendered", content is not None)

        # ── Step 3: Architecture ──
        print("\n=== Step 3: Architecture ===")
        link = await page.query_selector('a[href="/architecture"]')
        check("Architecture nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-03-architecture.png")
            content = await page.query_selector("text=Architecture")
            check("Architecture page rendered", content is not None)
            diagram = await page.query_selector("text=Application Layer")
            check("Architecture diagram renders (not raw Mermaid)", diagram is not None)

        # ── Step 4: Corporate Demo ──
        print("\n=== Step 4: Corporate Demo ===")
        link = await page.query_selector('a[href="/corporate-demo"]')
        check("Corporate Demo nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-04-corporate-demo.png")

            title = await page.query_selector("text=Corporate Demo")
            check("Corporate Demo title rendered", title is not None)

            mock_banner = await page.query_selector("text=Running in demo mode")
            check("Mock mode banner visible", mock_banner is not None)

            chat_history = await page.query_selector("#chat-history")
            check("Chat history container present", chat_history is not None)

            prov_panel = await page.query_selector("#provenance-panel")
            check("Provenance panel present", prov_panel is not None)

            chat_input = await page.query_selector("#chat-input")
            check("Chat input field present", chat_input is not None)

            send_btn = await page.query_selector("#send-btn")
            check("Send button present", send_btn is not None)

            # Example question buttons
            example_btns = await page.query_selector_all('button:has-text("Kenneth Lay")')
            check("At least one example question button exists", len(example_btns) > 0)

        # ── Step 5: Send an example question (mock mode) ──
        print("\n=== Step 5: Example Question (mock) ===")
        example_btn = await page.query_selector('button:has-text("Who communicated most frequently with Kenneth Lay?")')
        if example_btn:
            await example_btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path=SCREENSHOT_DIR / "local-05-response.png")

            chat_el = await page.query_selector("#chat-history")
            chat_text = await chat_el.inner_text() if chat_el else ""
            check("User question appears in chat", "Kenneth Lay" in chat_text)
            check(
                "Agent response appears in chat",
                "Rosalee Fleming" in chat_text or "Jeffrey Skilling" in chat_text or "GraphRAG Agent" in chat_text,
            )

            prov_el = await page.query_selector("#provenance-panel")
            prov_text = await prov_el.inner_text() if prov_el else ""
            check("Provenance shows Traced Path", "Traced Path" in prov_text)
            check("Provenance shows Source Citations", "Source" in prov_text or "Citation" in prov_text)
            check("Provenance shows Grounding", "Grounding" in prov_text)
        else:
            check("Example button 'Who communicated most frequently with Kenneth Lay?' found", False)

        # ── Step 6: Type a custom question via Enter key ──
        print("\n=== Step 6: Custom Question (Enter key) ===")
        chat_input = await page.query_selector("#chat-input")
        if chat_input:
            await chat_input.fill("Who was involved in the California energy trading decisions?")
            await chat_input.press("Enter")
            await asyncio.sleep(2)
            await page.screenshot(path=SCREENSHOT_DIR / "local-06-custom-question.png")

            chat_el = await page.query_selector("#chat-history")
            chat_text = await chat_el.inner_text() if chat_el else ""
            check("Enter-to-send works: California question in chat", "California" in chat_text)
            check(
                "California mock response differs from Kenneth Lay response",
                "Tim Belden" in chat_text or "David Delainey" in chat_text or "energy trading" in chat_text.lower(),
            )
            check("Multiple messages in chat history", chat_text.count("You") >= 2)

        # ── Step 7: Apply to Business ──
        print("\n=== Step 7: Apply to Business ===")
        link = await page.query_selector('a[href="/apply"]')
        check("Apply nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-07-apply.png")
            content = await page.query_selector("text=Apply")
            check("Apply page rendered", content is not None)

        # ── Step 8: Navigate back to Home ──
        print("\n=== Step 8: Back to Home ===")
        link = await page.query_selector('a[href="/"]')
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-08-home-return.png")
            check("Returned to home page", True)

        # Console errors
        if console_errors:
            print(f"\nConsole errors ({len(console_errors)}):")
            for err in console_errors[:10]:
                print(f"  - {err[:200]}")

        await browser.close()

    # Summary
    total = PASS + FAIL
    print(f"\n{'=' * 60}")
    print(f"  RESULTS: {PASS}/{total} passed, {FAIL} failed")
    print(f"{'=' * 60}")
    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
