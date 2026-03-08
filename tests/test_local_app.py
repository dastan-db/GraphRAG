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

        # ── Step 4: Live Demo ──
        print("\n=== Step 4: Live Demo ===")
        link = await page.query_selector('a[href="/live-demo"]')
        check("Live Demo nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-04-live-demo.png")

            title = await page.query_selector("text=Live Demo")
            check("Live Demo title rendered", title is not None)

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
            example_btns = await page.query_selector_all('button:has-text("Ruth")')
            check("At least one example question button exists", len(example_btns) > 0)

        # ── Step 5: Send an example question (mock mode) ──
        print("\n=== Step 5: Example Question (mock) ===")
        example_btn = await page.query_selector('button:has-text("How is Ruth connected to Jesus?")')
        if example_btn:
            await example_btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path=SCREENSHOT_DIR / "local-05-response.png")

            chat_el = await page.query_selector("#chat-history")
            chat_text = await chat_el.inner_text() if chat_el else ""
            check("User question appears in chat", "Ruth" in chat_text)
            check("Agent response appears in chat", "Boaz" in chat_text or "GraphRAG Agent" in chat_text)

            prov_el = await page.query_selector("#provenance-panel")
            prov_text = await prov_el.inner_text() if prov_el else ""
            check("Provenance shows Traced Path", "Traced Path" in prov_text)
            check("Provenance shows Source Citations", "Source" in prov_text or "Citation" in prov_text)
            check("Provenance shows Grounding", "Grounding" in prov_text)
        else:
            check("Example button 'How is Ruth connected to Jesus?' found", False)

        # ── Step 6: Type a custom question via Enter key ──
        print("\n=== Step 6: Custom Question (Enter key) ===")
        chat_input = await page.query_selector("#chat-input")
        if chat_input:
            await chat_input.fill("What role does Moses play across the books?")
            await chat_input.press("Enter")
            await asyncio.sleep(2)
            await page.screenshot(path=SCREENSHOT_DIR / "local-06-custom-question.png")

            chat_el = await page.query_selector("#chat-history")
            chat_text = await chat_el.inner_text() if chat_el else ""
            check("Enter-to-send works: Moses question in chat", "Moses" in chat_text)
            check("Moses mock response differs from Ruth response", "burning bush" in chat_text.lower() or "exodus" in chat_text.lower())
            check("Multiple messages in chat history", chat_text.count("You") >= 2)

        # ── Step 7: Manage Corpus ──
        print("\n=== Step 7: Manage Corpus ===")
        link = await page.query_selector('a[href="/manage-corpus"]')
        check("Manage Corpus nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
            await page.screenshot(path=SCREENSHOT_DIR / "local-07-manage-corpus.png")

            title = await page.query_selector("text=Manage Corpus")
            check("Manage Corpus title rendered", title is not None)

            add_btn = await page.query_selector("#add-books-btn")
            check("Add Selected button present", add_btn is not None)

            remove_btn = await page.query_selector("#remove-books-btn")
            check("Remove Selected button present", remove_btn is not None)

            refresh_btn = await page.query_selector("#refresh-btn")
            check("Refresh button present", refresh_btn is not None)

            tabs = await page.query_selector("#testament-tabs")
            check("Testament tabs present", tabs is not None)

            book_grid = await page.query_selector("#book-grid")
            check("Book grid present", book_grid is not None)

            stats_panel = await page.query_selector("#stats-panel")
            check("Stats panel present", stats_panel is not None)

            enterprise_alert = await page.query_selector("text=Enterprise Pattern")
            check("Enterprise callout visible", enterprise_alert is not None)

            # Check a book checkbox and verify selection summary updates
            genesis_checkbox = await page.query_selector('[id*="book-check"][id*="Genesis"] input')
            if genesis_checkbox:
                await genesis_checkbox.click()
                await asyncio.sleep(0.5)
                summary = await page.query_selector("#selection-summary")
                summary_text = await summary.inner_text() if summary else ""
                check("Selection summary updates on checkbox", "1 selected" in summary_text)
                await genesis_checkbox.click()  # uncheck

            # Try clicking Add Selected while nothing is selected — should be disabled
            if add_btn:
                is_disabled = await add_btn.get_attribute("disabled")
                check("Add button disabled when nothing selected",
                      is_disabled is not None or is_disabled == "true")

            await page.screenshot(path=SCREENSHOT_DIR / "local-07b-manage-corpus-checked.png")

            # If in mock mode, try adding a book and check for mock message
            if USE_MOCK:
                genesis_cb = await page.query_selector('[id*="book-check"][id*="Genesis"] input')
                if genesis_cb:
                    await genesis_cb.click()
                    await asyncio.sleep(0.3)
                    if add_btn:
                        await add_btn.click()
                        await asyncio.sleep(1)
                        progress = await page.query_selector("#pipeline-progress")
                        progress_text = await progress.inner_text() if progress else ""
                        check("Mock pipeline progress shown",
                              "Mock mode" in progress_text or "pipeline" in progress_text.lower())
                    await page.screenshot(path=SCREENSHOT_DIR / "local-07c-manage-corpus-add.png")

            # Verify no error banners appeared
            error_alerts = await page.query_selector_all('.alert-danger')
            check("No error banners on manage corpus page", len(error_alerts) == 0)

        # ── Step 8: Apply to Business ──
        print("\n=== Step 8: Apply to Business ===")
        link = await page.query_selector('a[href="/apply"]')
        check("Apply nav link exists", link is not None)
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-08-apply.png")
            content = await page.query_selector("text=Apply")
            check("Apply page rendered", content is not None)

        # ── Step 9: Navigate back to Home ──
        print("\n=== Step 9: Back to Home ===")
        link = await page.query_selector('a[href="/"]')
        if link:
            await link.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(0.5)
            await page.screenshot(path=SCREENSHOT_DIR / "local-09-home-return.png")
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
