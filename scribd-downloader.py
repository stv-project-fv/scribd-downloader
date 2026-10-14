"""
Scribd Document Downloader
==========================

A Selenium-based utility that loads a Scribd embed and saves it as a PDF.

Key behaviors:
1. Converts a Scribd document URL to the embed/content URL.
2. Opens the document in Chrome.
3. Scrolls through every page to trigger lazy loading.
4. Removes UI overlays without stripping layout classes needed for rendering.
5. Waits for fonts, images, and page geometry to settle.
6. Saves the PDF through Chrome DevTools Protocol with a larger timeout and
   stream-based PDF transfer for large documents.
"""

import argparse
import base64
import os
import re
import tempfile
import time
from io import BytesIO
from urllib.parse import unquote, urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options


DEFAULT_CDP_TIMEOUT_SECONDS = int(os.getenv("SCRIBD_CDP_TIMEOUT", "600"))
DEFAULT_RENDER_SETTLE_TIMEOUT_SECONDS = int(
    os.getenv("SCRIBD_RENDER_SETTLE_TIMEOUT", "30")
)
DEFAULT_SCROLL_DELAY_SECONDS = float(os.getenv("SCRIBD_SCROLL_DELAY", "0.15"))
DEFAULT_PAGE_LOAD_CONCURRENCY = max(
    1,
    int(os.getenv("SCRIBD_PAGE_LOAD_CONCURRENCY", "8")),
)
DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS = max(
    10,
    int(os.getenv("SCRIBD_PAGE_LOAD_TIMEOUT", "120")),
)
DEFAULT_EXPORT_BATCH_SIZE = max(
    1,
    int(os.getenv("SCRIBD_EXPORT_BATCH_SIZE", "8")),
)
PDF_STREAM_CHUNK_SIZE = int(os.getenv("SCRIBD_PDF_STREAM_CHUNK_SIZE", str(1024 * 1024)))
HEADLESS_ENABLED = os.getenv("SCRIBD_HEADLESS", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
DEFAULT_PAPER_WIDTH_INCHES = 7.25
DEFAULT_PAPER_HEIGHT_INCHES = 10.5


def build_chrome_options(runtime_profile_dir):
    """Create Chrome options for reliable headless PDF generation."""
    options = Options()

    if HEADLESS_ENABLED:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1600,2200")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument(f"--user-data-dir={runtime_profile_dir}")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--force-color-profile=srgb")
    options.add_argument("--hide-scrollbars")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return options


def convert_scribd_link(url):
    """
    Convert a Scribd document URL to the embed/content URL.

    Args:
        url: Standard Scribd URL such as
            https://www.scribd.com/document/123456789/Document-Title
            or https://www.scribd.com/doc/123456789/Document-Title

    Returns:
        The embeddable content URL, or "Invalid Scribd URL" if no document id
        can be extracted.
    """
    match = re.search(r"https://www\.scribd\.com/(?:document|doc)/(\d+)/", url)
    if not match:
        return "Invalid Scribd URL"

    return f"https://www.scribd.com/embeds/{match.group(1)}/content"


def get_filename_from_url(url):
    """
    Build an output filename from the last URL path segment.

    Args:
        url: Scribd document URL.

    Returns:
        Filename ending in ".pdf".
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    last_segment = path.split("/")[-1] if path else "scribd_document"
    return f"{unquote(last_segment)}.pdf"


def configure_command_timeout(driver, timeout_seconds):
    """
    Increase the Selenium HTTP timeout used for ChromeDriver commands.

    Large image-heavy documents can spend minutes inside Page.printToPDF before
    ChromeDriver responds, so the default 120 second timeout is too small.
    """
    executor = getattr(driver, "command_executor", None)
    if executor is None:
        return

    client_config = getattr(executor, "client_config", None)
    if client_config is None:
        client_config = getattr(executor, "_client_config", None)

    if client_config is not None:
        client_config.timeout = timeout_seconds


def hide_cookie_dialogs(driver):
    """Dismiss and remove common cookie, consent, and privacy banners."""
    driver.execute_script(
        """
        const closeButtonSelectors = [
            '[class*="cookie"] [class*="close"]',
            '[class*="cookie"] [class*="dismiss"]',
            '[class*="cookie"] button[aria-label*="close"]',
            '[class*="cookie"] button[aria-label*="Close"]',
            '[class*="consent"] [class*="close"]',
            '[class*="consent"] [class*="dismiss"]',
            '[class*="banner"] [class*="close"]',
            '[class*="banner"] [class*="dismiss"]',
            '[class*="notice"] [class*="close"]',
            '[class*="notice"] [class*="dismiss"]',
            'button[class*="close"]',
            'button[aria-label="Close"]',
            'button[aria-label="close"]',
            'button[aria-label="Dismiss"]',
            '[data-dismiss]',
            '[role="button"][class*="close"]'
        ];

        closeButtonSelectors.forEach((selector) => {
            try {
                document.querySelectorAll(selector).forEach((button) => button.click());
            } catch (error) {}
        });

        const cookieSelectors = [
            '[class*="cookie"]',
            '[class*="Cookie"]',
            '[class*="consent"]',
            '[class*="Consent"]',
            '[class*="gdpr"]',
            '[class*="GDPR"]',
            '[id*="cookie"]',
            '[id*="Cookie"]',
            '[id*="consent"]',
            '[id*="gdpr"]',
            '[class*="privacy-notice"]',
            '[class*="Privacy"]',
            '[class*="cookie-banner"]',
            '[class*="cookie-notice"]',
            '[class*="cookie-popup"]',
            '[class*="cookie-modal"]',
            '[class*="CookieConsent"]',
            '[class*="notice-banner"]',
            '.cc-window',
            '.cc-banner',
            '#onetrust-consent-sdk',
            '#onetrust-banner-sdk',
            '.evidon-banner',
            '.truste_box_overlay',
            '[class*="osano-cm"]',
            '[id*="osano"]'
        ];

        cookieSelectors.forEach((selector) => {
            try {
                document.querySelectorAll(selector).forEach((element) => element.remove());
            } catch (error) {}
        });

        document.querySelectorAll('*').forEach((element) => {
            try {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const text = (element.innerText || '').toLowerCase();
                const fixedAtTop =
                    (style.position === 'fixed' || style.position === 'sticky') &&
                    rect.top < 100;

                if (
                    fixedAtTop &&
                    (
                        text.includes('cookie') ||
                        text.includes('privacy') ||
                        text.includes('consent') ||
                        text.includes('analytics') ||
                        text.includes('advertising') ||
                        text.includes('personalization')
                    )
                ) {
                    element.remove();
                }
            } catch (error) {}
        });
        """
    )


def scroll_through_pages(driver, scroll_delay_seconds):
    """
    Scroll through all detected pages until the page count stabilizes.

    Scribd lazily renders more page nodes while scrolling, so a single snapshot
    of "[class*='page']" is not always enough for long documents.
    """
    scrolled_count = 0
    stable_rounds = 0
    last_total_pages = -1

    while stable_rounds < 2:
        page_elements = driver.find_elements("css selector", "[class*='page']")
        total_pages = len(page_elements)

        if total_pages == 0:
            print("No page elements were detected.")
            return 0

        if total_pages == last_total_pages:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_total_pages = total_pages

        if scrolled_count == 0:
            print(f"Found {total_pages} pages, scrolling...")
        elif total_pages > scrolled_count:
            print(f"Detected {total_pages} pages after lazy loading, continuing...")

        for index in range(scrolled_count, total_pages):
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});",
                page_elements[index],
            )
            time.sleep(scroll_delay_seconds)

            if (index + 1) % 10 == 0:
                print(f"  Scrolled {index + 1}/{total_pages} pages...")

        scrolled_count = total_pages
        time.sleep(0.5)

    print(f"All {scrolled_count} pages loaded.")
    return scrolled_count


def load_all_pages(driver):
    """Load Scribd pages directly, without simulating user scrolling."""
    page_count = driver.execute_script(
        """
        return window.docManager && window.docManager.pages
            ? Object.values(window.docManager.pages).filter(Boolean).length
            : 0;
        """
    )
    batch_count = max(
        1,
        (page_count + DEFAULT_PAGE_LOAD_CONCURRENCY - 1)
        // DEFAULT_PAGE_LOAD_CONCURRENCY,
    )
    document_timeout_seconds = (
        DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS * batch_count
    )
    configure_command_timeout(driver, document_timeout_seconds + 10)
    driver.set_script_timeout(document_timeout_seconds + 10)

    result = driver.execute_async_script(
        """
        const concurrency = arguments[0];
        const timeoutMs = arguments[1];
        const documentTimeoutMs = arguments[2];
        const done = arguments[arguments.length - 1];
        const manager = window.docManager;

        if (!manager || !manager.pages) {
            done({supported: false});
            return;
        }

        const pages = Object.values(manager.pages).filter(Boolean);
        const pending = pages.filter((page) => !page.innerPageElem);
        const active = new Map();
        const failed = [];
        const startedAt = Date.now();
        let nextIndex = 0;
        let completed = pages.length - pending.length;
        let finished = false;

        function finish() {
            if (finished) {
                return;
            }

            finished = true;
            clearInterval(timer);

            pages.forEach((page) => {
                if (!page.innerPageElem) {
                    return;
                }

                try {
                    page.display();
                } catch (error) {}

                try {
                    page.turnOnImages();
                } catch (error) {}
            });

            done({
                supported: true,
                total: pages.length,
                loaded: pages.filter((page) => page.innerPageElem).length,
                failed,
                elapsedMs: Date.now() - startedAt
            });
        }

        function launchMore() {
            while (active.size < concurrency && nextIndex < pending.length) {
                const page = pending[nextIndex++];

                try {
                    if (!page.loadHasStarted) {
                        page.load();
                    }

                    active.set(page.pageNum, {
                        page,
                        startedAt: Date.now()
                    });
                } catch (error) {
                    failed.push({
                        pageNum: page.pageNum,
                        reason: String(error)
                    });
                }
            }

            if (completed + failed.length >= pages.length) {
                finish();
            }
        }

        const timer = setInterval(() => {
            for (const [pageNum, state] of active) {
                if (state.page.innerPageElem) {
                    active.delete(pageNum);
                    completed += 1;
                    continue;
                }

                if (Date.now() - state.startedAt >= timeoutMs) {
                    active.delete(pageNum);
                    failed.push({
                        pageNum,
                        reason: 'page load timed out'
                    });
                }
            }

            if (Date.now() - startedAt >= documentTimeoutMs) {
                for (const [pageNum] of active) {
                    failed.push({
                        pageNum,
                        reason: 'document load timed out'
                    });
                }
                active.clear();
                finish();
                return;
            }

            launchMore();
        }, 50);

        launchMore();
        """,
        DEFAULT_PAGE_LOAD_CONCURRENCY,
        DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS * 1000,
        document_timeout_seconds * 1000,
    )

    if not result.get("supported"):
        print("Direct page loader unavailable; using scrolling fallback.")
        return scroll_through_pages(driver, DEFAULT_SCROLL_DELAY_SECONDS)

    total_pages = result["total"]
    loaded_pages = result["loaded"]
    elapsed_seconds = result["elapsedMs"] / 1000

    print(
        f"Loaded {loaded_pages}/{total_pages} pages directly "
        f"in {elapsed_seconds:.2f}s "
        f"(concurrency: {DEFAULT_PAGE_LOAD_CONCURRENCY})."
    )

    if result["failed"]:
        failed_pages = ", ".join(
            str(item["pageNum"])
            for item in result["failed"]
        )
        raise RuntimeError(
            "Failed to load Scribd page(s): "
            f"{failed_pages}"
        )

    return loaded_pages


def prepare_document_for_print(driver):
    """
    Remove UI chrome and make the scroll containers printable.

    The old version removed the .document_scroller class entirely, which can
    break descendant CSS needed by math- and font-heavy documents. We keep the
    class and only override the few layout properties that interfere with print.
    """
    result = driver.execute_script(
        """
        const removed = { toolbarTop: false, toolbarBottom: false, containers: 0 };

        const toolbarTop = document.querySelector('.toolbar_top');
        if (toolbarTop) {
            toolbarTop.remove();
            removed.toolbarTop = true;
        }

        const toolbarBottom = document.querySelector('.toolbar_bottom');
        if (toolbarBottom) {
            toolbarBottom.remove();
            removed.toolbarBottom = true;
        }

        document.querySelectorAll('.document_scroller').forEach((element) => {
            element.setAttribute('data-scribd-print-root', 'true');
            element.style.position = 'static';
            element.style.top = 'auto';
            element.style.bottom = 'auto';
            element.style.left = 'auto';
            element.style.right = 'auto';
            element.style.overflow = 'visible';
            element.style.maxHeight = 'none';
            element.style.height = 'auto';
            element.style.margin = '0';
            element.style.padding = '0';
            removed.containers += 1;
        });

        return removed;
        """
    )

    if result["toolbarTop"]:
        print("Top toolbar removed.")
    if result["toolbarBottom"]:
        print("Bottom toolbar removed.")

    print(f"Adjusted {result['containers']} scroll containers for print.")


def inject_print_styles(driver):
    """Install conservative print CSS without hiding Scribd document content."""
    driver.execute_script(
        """
        const existing = document.getElementById('scribd-print-styles');
        if (existing) {
            existing.remove();
        }

        const style = document.createElement('style');
        style.id = 'scribd-print-styles';
        style.textContent = `
            [class*="cookie"],
            [class*="Cookie"],
            [class*="consent"],
            [class*="Consent"],
            [class*="gdpr"],
            [class*="privacy-notice"],
            [class*="notice-banner"],
            [id*="cookie"],
            [id*="consent"],
            [class*="osano-cm"],
            [id*="osano"] {
                display: none !important;
                visibility: hidden !important;
                opacity: 0 !important;
                height: 0 !important;
                overflow: hidden !important;
            }

            [data-scribd-print-root="true"],
            .document_scroller {
                position: static !important;
                top: auto !important;
                right: auto !important;
                bottom: auto !important;
                left: auto !important;
                overflow: visible !important;
                height: auto !important;
                max-height: none !important;
                margin: 0 !important;
                padding: 0 !important;
            }

            @media print {
                html,
                body {
                    margin: 0 !important;
                    padding: 0 !important;
                    -webkit-print-color-adjust: exact !important;
                    print-color-adjust: exact !important;
                }

                .toolbar_top,
                .toolbar_bottom {
                    display: none !important;
                }

                [data-scribd-print-root="true"],
                .document_scroller {
                    position: static !important;
                    top: auto !important;
                    right: auto !important;
                    bottom: auto !important;
                    left: auto !important;
                    overflow: visible !important;
                    height: auto !important;
                    max-height: none !important;
                    margin: 0 !important;
                    padding: 0 !important;
                }

                mjx-container,
                .MathJax,
                .katex,
                math,
                svg {
                    visibility: visible !important;
                    overflow: visible !important;
                }
            }
        `;

        document.head.appendChild(style);
        """
    )

    print("Print CSS injected.")


def wait_for_render_stability(driver, timeout_seconds):
    """
    Wait for fonts, images, and page dimensions to settle before printing.

    This lowers the risk of exporting before math glyphs, SVG content, or web
    fonts finish rendering.
    """
    driver.set_script_timeout(timeout_seconds + 5)

    try:
        result = driver.execute_async_script(
            """
            const settleBudgetMs = arguments[0];
            const done = arguments[arguments.length - 1];
            const start = performance.now();
            let stableTicks = 0;
            let lastSample = '';

            function sample() {
                const pages = Array.from(document.querySelectorAll("[class*='page']"));
                const heights = pages.slice(0, 12).map((element) =>
                    Math.round(element.getBoundingClientRect().height)
                );
                const pendingImages = Array.from(document.images || []).filter(
                    (image) => !image.complete
                ).length;
                return JSON.stringify({
                    pageCount: pages.length,
                    heights,
                    pendingImages
                });
            }

            function finish(timedOut) {
                done({
                    timedOut,
                    sample: lastSample || sample()
                });
            }

            function tick() {
                lastSample = sample();
                const parsed = JSON.parse(lastSample);
                const isBusy = parsed.pendingImages > 0;

                if (!isBusy && lastSample === window.__scribdLastRenderSample) {
                    stableTicks += 1;
                } else {
                    stableTicks = 0;
                }

                window.__scribdLastRenderSample = lastSample;

                if (stableTicks >= 2) {
                    finish(false);
                    return;
                }

                if (performance.now() - start >= settleBudgetMs) {
                    finish(true);
                    return;
                }

                requestAnimationFrame(() => setTimeout(tick, 200));
            }

            const fontsReady = document.fonts && document.fonts.ready
                ? document.fonts.ready.catch(() => undefined)
                : Promise.resolve();

            fontsReady.finally(() => {
                requestAnimationFrame(() => setTimeout(tick, 200));
            });
            """,
            int(timeout_seconds * 1000),
        )
    except WebDriverException as error:
        print(f"Render settle check failed; continuing with best effort: {error}")
        return

    if result.get("timedOut"):
        print("Render settle reached its time budget; continuing with best effort.")
    else:
        print("Document render settled before export.")


def detect_document_paper_size(driver):
    """
    Infer a paper size from the first rendered Scribd page.

    Scribd pages often render as absolutely positioned HTML at a fixed CSS size.
    Using that page box as the print sheet size avoids splitting one Scribd page
    across multiple PDF pages.
    """
    paper_size = driver.execute_script(
        """
        const candidates = [
            '.outer_page',
            '.newpage',
            '.outer_page_container',
            "[class*='page']"
        ];

        for (const selector of candidates) {
            const element = document.querySelector(selector);
            if (!element) {
                continue;
            }

            const rect = element.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                return {
                    widthInches: rect.width / 96,
                    heightInches: rect.height / 96,
                    selector
                };
            }
        }

        return null;
        """
    )

    if not paper_size:
        return {
            "widthInches": DEFAULT_PAPER_WIDTH_INCHES,
            "heightInches": DEFAULT_PAPER_HEIGHT_INCHES,
            "selector": "default",
        }

    return {
        "widthInches": max(1.0, round(paper_size["widthInches"], 3)),
        "heightInches": max(1.0, round(paper_size["heightInches"], 3)),
        "selector": paper_size["selector"],
    }


def read_pdf_stream_to_file(driver, stream_handle, filename):
    """Read a streamed CDP PDF result and write it to disk in chunks."""
    try:
        with open(filename, "wb") as file_handle:
            while True:
                chunk = driver.execute_cdp_cmd(
                    "IO.read",
                    {
                        "handle": stream_handle,
                        "size": PDF_STREAM_CHUNK_SIZE,
                    },
                )

                data = chunk.get("data", "")
                if not data and chunk.get("eof"):
                    break

                if chunk.get("base64Encoded"):
                    file_handle.write(base64.b64decode(data))
                else:
                    file_handle.write(data.encode("utf-8"))

                if chunk.get("eof"):
                    break
    finally:
        driver.execute_cdp_cmd("IO.close", {"handle": stream_handle})


def load_page_batch(driver, page_numbers):
    """Load one bounded batch of page DOM and image assets."""
    configure_command_timeout(
        driver,
        DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS + 10,
    )
    driver.set_script_timeout(DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS + 10)

    result = driver.execute_async_script(
        """
        const pageNumbers = arguments[0];
        const timeoutMs = arguments[1];
        const done = arguments[arguments.length - 1];
        const manager = window.docManager;

        if (!manager || !manager.pages) {
            done({supported: false});
            return;
        }

        const states = pageNumbers.map((pageNum) => ({
            pageNum,
            page: manager.pages[pageNum],
            error: null
        }));
        const startedAt = Date.now();

        for (const state of states) {
            if (!state.page) {
                state.error = 'page object missing';
                continue;
            }

            try {
                if (!state.page.innerPageElem && !state.page.loadHasStarted) {
                    state.page.load();
                }
            } catch (error) {
                state.error = String(error);
            }
        }

        const timer = setInterval(() => {
            let ready = 0;

            for (const state of states) {
                if (state.error) {
                    ready += 1;
                    continue;
                }

                const page = state.page;
                if (!page.innerPageElem) {
                    continue;
                }

                try {
                    page.display();
                    if (!page._imagesTurnedOn) {
                        page.turnOnImages();
                    }
                } catch (error) {
                    state.error = String(error);
                    ready += 1;
                    continue;
                }

                const images = Array.from(
                    page.innerPageElem.querySelectorAll('img')
                );
                const pending = images.filter((image) => !image.complete);

                if (pending.length === 0) {
                    ready += 1;
                }
            }

            if (ready === states.length) {
                clearInterval(timer);
                done({
                    supported: true,
                    failed: states
                        .filter((state) => state.error)
                        .map((state) => ({
                            pageNum: state.pageNum,
                            reason: state.error
                        }))
                });
                return;
            }

            if (Date.now() - startedAt >= timeoutMs) {
                clearInterval(timer);
                done({
                    supported: true,
                    failed: states
                        .filter((state) => (
                            state.error ||
                            !state.page ||
                            !state.page.innerPageElem ||
                            Array.from(
                                state.page.innerPageElem.querySelectorAll('img')
                            ).some((image) => !image.complete)
                        ))
                        .map((state) => ({
                            pageNum: state.pageNum,
                            reason: state.error || 'page or image load timed out'
                        }))
                });
            }
        }, 50);
        """,
        list(page_numbers),
        DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS * 1000,
    )

    if not result.get("supported"):
        raise RuntimeError("Scribd direct page loader is unavailable.")

    if result["failed"]:
        details = ", ".join(
            f"{item['pageNum']} ({item['reason']})"
            for item in result["failed"]
        )
        raise RuntimeError(f"Failed to load Scribd page(s): {details}")


def release_page_batch(driver, page_numbers):
    """Release printed page DOM and image resources from Chrome."""
    driver.execute_script(
        """
        const manager = window.docManager;
        if (!manager || !manager.pages) {
            return;
        }

        for (const pageNum of arguments[0]) {
            const page = manager.pages[pageNum];
            if (!page) {
                continue;
            }

            try {
                page.remove();
            } catch (error) {
                const container = document.getElementById(`outer_page_${pageNum}`);
                if (container) {
                    const inner = container.querySelector('.newpage');
                    if (inner) {
                        inner.remove();
                    }
                }
            }
        }
        """,
        list(page_numbers),
    )

    try:
        driver.execute_cdp_cmd("HeapProfiler.collectGarbage", {})
    except WebDriverException:
        pass


def save_pdf_pages_individually(
    driver,
    filename,
    timeout_seconds=DEFAULT_CDP_TIMEOUT_SECONDS,
):
    from pypdf import PdfReader, PdfWriter

    configure_command_timeout(
        driver,
        timeout_seconds,
    )

    page_count = driver.execute_script(
        """
        return document.querySelectorAll(
            '.outer_page'
        ).length;
        """
    )

    if page_count <= 0:
        raise RuntimeError(
            "No .outer_page elements found."
        )

    print(
        f"Exporting {page_count} "
        "document pages in bounded batches "
        f"of {DEFAULT_EXPORT_BATCH_SIZE}..."
    )

    spool = tempfile.TemporaryDirectory(
        prefix="scribd-pdf-pages-"
    )
    page_files = []

    try:
        for index in range(page_count):
            if index % DEFAULT_EXPORT_BATCH_SIZE == 0:
                batch_end = min(
                    page_count,
                    index + DEFAULT_EXPORT_BATCH_SIZE,
                )
                batch_page_numbers = list(
                    range(index + 1, batch_end + 1)
                )
                print(
                    f"  Loading page batch "
                    f"{index + 1}-{batch_end}/{page_count}..."
                )
                load_page_batch(
                    driver,
                    batch_page_numbers,
                )

            page_info = driver.execute_script(
                """
                const targetIndex = arguments[0];

                const pages = Array.from(
                    document.querySelectorAll(
                        '.outer_page'
                    )
                );

                const target = pages[targetIndex];

                if (!target) {
                    return null;
                }

                /*
                 * Remove previous isolated-print style.
                 */
                const oldStyle = document.getElementById(
                    'isolated-page-print-style'
                );

                if (oldStyle) {
                    oldStyle.remove();
                }

                /*
                 * Restore all pages before measuring.
                 */
                pages.forEach((page) => {
                    page.style.removeProperty('display');
                    page.style.removeProperty('visibility');
                    page.style.removeProperty('position');
                    page.style.removeProperty('top');
                    page.style.removeProperty('left');
                    page.style.removeProperty('right');
                    page.style.removeProperty('bottom');
                    page.style.removeProperty('margin');
                    page.style.removeProperty('break-after');
                    page.style.removeProperty('page-break-after');
                    page.style.removeProperty('break-before');
                    page.style.removeProperty('page-break-before');
                });

                const rect = target.getBoundingClientRect();

                const width = Math.ceil(rect.width);
                const height = Math.ceil(rect.height);

                /*
                 * Mark the target instead of relying on nth-child.
                 */
                pages.forEach((page) => {
                    page.removeAttribute(
                        'data-export-target'
                    );
                });

                target.setAttribute(
                    'data-export-target',
                    'true'
                );

                const style = document.createElement(
                    'style'
                );

                style.id = 'isolated-page-print-style';

                style.textContent = `
                    @page {
                        size: ${width}px ${height}px;
                        margin: 0;
                    }

                    @media print {
                        html,
                        body {
                            width: ${width}px !important;
                            height: ${height}px !important;
                            min-width: ${width}px !important;
                            min-height: ${height}px !important;
                            max-width: ${width}px !important;
                            max-height: ${height}px !important;

                            margin: 0 !important;
                            padding: 0 !important;

                            overflow: hidden !important;

                            -webkit-print-color-adjust:
                                exact !important;

                            print-color-adjust:
                                exact !important;
                        }

                        .outer_page {
                            display: none !important;
                        }

                        .outer_page[
                            data-export-target="true"
                        ] {
                            display: block !important;
                            visibility: visible !important;

                            position: absolute !important;

                            top: 0 !important;
                            left: 0 !important;
                            right: auto !important;
                            bottom: auto !important;

                            width: ${width}px !important;
                            height: ${height}px !important;

                            min-width: 0 !important;
                            min-height: 0 !important;

                            max-width: none !important;
                            max-height: none !important;

                            margin: 0 !important;
                            padding: 0 !important;

                            transform: none !important;

                            break-before: auto !important;
                            break-after: auto !important;
                            break-inside: auto !important;

                            page-break-before:
                                auto !important;

                            page-break-after:
                                auto !important;

                            page-break-inside:
                                auto !important;

                            overflow: hidden !important;
                        }
                    }
                `;

                document.head.appendChild(style);

                return {
                    width,
                    height
                };
                """,
                index,
            )

            if not page_info:
                print(
                    f"  Skipping page "
                    f"{index + 1}: element missing"
                )
                continue

            width_px = int(page_info["width"])
            height_px = int(page_info["height"])

            if width_px <= 0 or height_px <= 0:
                print(
                    f"  Skipping page {index + 1}: "
                    f"invalid geometry "
                    f"{width_px}x{height_px}"
                )
                continue

            width_inches = width_px / 96.0
            height_inches = height_px / 96.0

            print(
                f"  Page {index + 1}/{page_count} "
                f"{width_px}x{height_px}px "
                f"-> "
                f'{width_inches:.3f}"'
                f'x{height_inches:.3f}"'
            )

            driver.execute_cdp_cmd(
                "Emulation.setEmulatedMedia",
                {
                    "media": "print",
                },
            )

            result = driver.execute_cdp_cmd(
                "Page.printToPDF",
                {
                    "landscape": False,
                    "displayHeaderFooter": False,
                    "printBackground": True,

                    "scale": 1,

                    "paperWidth": width_inches,
                    "paperHeight": height_inches,

                    "marginTop": 0,
                    "marginBottom": 0,
                    "marginLeft": 0,
                    "marginRight": 0,

           
                    "preferCSSPageSize": True,

                
                    "pageRanges": "1",

                    "transferMode": "ReturnAsBase64",
                },
            )

            pdf_bytes = base64.b64decode(
                result["data"]
            )

            page_reader = PdfReader(BytesIO(pdf_bytes))
            if len(page_reader.pages) != 1:
                raise RuntimeError(
                    f"Document page "
                    f"{index + 1} produced "
                    f"{len(page_reader.pages)} "
                    "PDF sheets; expected exactly 1."
                )

            page_path = os.path.join(
                spool.name,
                f"page-{index + 1:08d}.pdf",
            )
            with open(page_path, "wb") as page_handle:
                page_handle.write(pdf_bytes)
            page_files.append(page_path)

            print(
                f"    OK: exactly 1 PDF sheet"
            )

            is_batch_end = (
                (index + 1) % DEFAULT_EXPORT_BATCH_SIZE == 0
                or index + 1 == page_count
            )
            if is_batch_end:
                release_page_batch(
                    driver,
                    batch_page_numbers,
                )

        if not page_files:
            raise RuntimeError(
                "No valid document pages "
                "were exported."
            )

        print(
            f"Merging {len(page_files)} "
            "disk-spooled PDF pages..."
        )
        writer = PdfWriter()
        try:
            for page_path in page_files:
                writer.append(page_path)
            with open(filename, "wb") as output_handle:
                writer.write(output_handle)
        finally:
            writer.close()

    finally:
        spool.cleanup()

    return os.path.abspath(filename)

def is_issuu_url(url):
    """Check if the given URL is an Issuu document link."""
    return bool(re.search(r"(?:issuu\.com|isu\.pub)", url, re.IGNORECASE))


def parse_issuu_url(url):
    """
    Extract username and document slug from Issuu URL.
    Example: https://issuu.com/bomvarela/docs/bomweb10_2024
    """
    match = re.search(r"issuu\.com/([^/]+)/docs/([^/?#]+)", url, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return None, None


def parse_page_range(range_str, total_pages):
    """
    Parse a page range string like '225,226', '225-226', '1-5, 8, 10-12', or 'all'.
    Returns a sorted list of unique 1-based page numbers within [1, total_pages].
    """
    if not range_str or range_str.strip().lower() in ("all", "*", ""):
        return list(range(1, total_pages + 1))

    selected = set()
    parts = range_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                start = int(bounds[0].strip())
                end = int(bounds[1].strip())
                for p in range(min(start, end), max(start, end) + 1):
                    if 1 <= p <= total_pages:
                        selected.add(p)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    selected.add(p)
            except ValueError:
                continue

    return sorted(selected)


def download_issuu_document(url, page_range_str=None, output_filename=None):
    """
    Download specified pages from an Issuu publication and merge into a single PDF.
    """
    import io
    import requests
    from PIL import Image
    from pypdf import PdfWriter

    username, slug = parse_issuu_url(url)
    if not username or not slug:
        raise RuntimeError(
            f"Invalid Issuu URL: {url}\n"
            "Expected format: https://issuu.com/<username>/docs/<document-name>"
        )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    print(f"\nFetching Issuu document metadata for '{slug}' by '{username}'...")
    reader_url = f"https://reader3.isu.pub/{username}/{slug}/reader3_4.json"

    page_image_urls = {}
    total_pages = 0

    try:
        resp = requests.get(reader_url, headers=headers, timeout=30)
        if resp.status_code == 200:
            doc_data = resp.json().get("document", {})
            pages_list = doc_data.get("pages", [])
            total_pages = len(pages_list)
            for idx, p_info in enumerate(pages_list):
                page_num = idx + 1
                uri = p_info.get("imageUri", "")
                if uri:
                    if not uri.startswith("http"):
                        uri = f"https://{uri}"
                    page_image_urls[page_num] = uri
    except Exception as e:
        print(f"Warning: reader API lookup failed ({e}), attempting fallback...")

    if not page_image_urls:
        html_resp = requests.get(url, headers=headers, timeout=30)
        if html_resp.status_code != 200:
            raise RuntimeError(f"Failed to access Issuu page (HTTP {html_resp.status_code})")

        match = re.search(r'https?://image\.isu\.pub/([a-zA-Z0-9_-]+)/jpg/page_1', html_resp.text)
        if not match:
            match = re.search(r'publicationId["\':\s]+([a-zA-Z0-9_-]+)', html_resp.text)
        if not match:
            raise RuntimeError("Could not locate publication identifier for this Issuu document.")

        pub_id = match.group(1)
        count_match = re.search(r'pageCount["\':\s]+(\d+)', html_resp.text)
        total_pages = int(count_match.group(1)) if count_match else 1
        page_image_urls = {
            i: f"https://image.isu.pub/{pub_id}/jpg/page_{i}.jpg"
            for i in range(1, total_pages + 1)
        }

    print(f"Total pages in publication: {total_pages}")

    selected_pages = parse_page_range(page_range_str, total_pages)
    if not selected_pages:
        raise RuntimeError("No valid pages selected for download.")

    if not output_filename:
        if len(selected_pages) == total_pages:
            output_filename = f"{slug}.pdf"
        elif len(selected_pages) == 1:
            output_filename = f"{slug}_pagina_{selected_pages[0]}.pdf"
        elif selected_pages == list(range(selected_pages[0], selected_pages[-1] + 1)):
            output_filename = f"{slug}_paginas_{selected_pages[0]}-{selected_pages[-1]}.pdf"
        else:
            output_filename = f"{slug}_paginas_seleccionadas.pdf"

    print(f"Exporting {len(selected_pages)} page(s) to: {output_filename}")
    print(f"Selected pages: {selected_pages}")

    with tempfile.TemporaryDirectory(prefix="issuu-pdf-pages-") as spool_dir:
        spooled_pdfs = []
        for p in selected_pages:
            img_url = page_image_urls.get(p)
            print(f"  Downloading page {p}/{total_pages} from {img_url}...")
            img_resp = requests.get(img_url, headers=headers, timeout=60)
            if img_resp.status_code != 200:
                raise RuntimeError(f"Failed to download image for page {p} (HTTP {img_resp.status_code})")

            img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
            page_pdf_path = os.path.join(spool_dir, f"page_{p:06d}.pdf")
            img.save(page_pdf_path, "PDF", resolution=300.0)
            img.close()
            spooled_pdfs.append(page_pdf_path)

        print(f"Merging {len(spooled_pdfs)} disk-spooled pages into final PDF...")
        writer = PdfWriter()
        for pdf_path in spooled_pdfs:
            writer.append(pdf_path)

        with open(output_filename, "wb") as f_out:
            writer.write(f_out)
        writer.close()

    abs_path = os.path.abspath(output_filename)
    print(f"\nPDF saved successfully to: {abs_path}")
    return abs_path


def main():
    """Run the exporter interactively or via CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Download Scribd and Issuu documents as PDF."
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="URL of the Scribd or Issuu document.",
    )
    parser.add_argument(
        "--pages",
        "-p",
        default=None,
        help="Page range to download (e.g. '225-226', '1-10', 'all'). Currently supported for Issuu.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Custom output PDF filename.",
    )

    args = parser.parse_args()

    input_url = args.url
    if not input_url:
        input_url = input("Input link (Scribd or Issuu): ").strip()

    if not input_url:
        print("Error: No URL provided.")
        raise SystemExit(1)

    if is_issuu_url(input_url):
        page_range = args.pages
        if not page_range:
            page_range = input(
                "Input page range (e.g. 225-226, 1-10, or Enter for all): "
            ).strip()
        download_issuu_document(
            input_url,
            page_range_str=page_range,
            output_filename=args.output,
        )
        return

    converted_url = convert_scribd_link(input_url)
    pdf_filename = args.output or get_filename_from_url(input_url)

    print(f"Link embed: {converted_url}")
    print(f"Output filename: {pdf_filename}")

    if converted_url == "Invalid Scribd URL":
        print("Error: Please provide a valid Scribd or Issuu document URL")
        print(
            "Example Scribd: "
            "https://www.scribd.com/document/"
            "123456789/Document-Title"
        )
        print(
            "Example Issuu:  "
            "https://issuu.com/username/docs/document-name"
        )
        raise SystemExit(1)

    with tempfile.TemporaryDirectory(
        prefix="scribd-chrome-profile-"
    ) as runtime_profile_dir:
        driver = None

        try:
            print("\nStarting Chrome browser...")

            options = build_chrome_options(
                runtime_profile_dir
            )

            driver = webdriver.Chrome(
                options=options
            )

            driver.get(converted_url)
            time.sleep(1)

            hide_cookie_dialogs(driver)
            print("Cookie dialogs hidden.")

            total_pages = driver.execute_script(
                """
                return document.querySelectorAll('.outer_page').length;
                """
            )

            if total_pages == 0:
                raise RuntimeError(
                    "No printable document pages "
                    "were detected."
                )

            prepare_document_for_print(driver)

            inject_print_styles(driver)

            print(
                f"\nSaving PDF as: {pdf_filename}"
            )

            print(
                "  Export mode: "
                "Individual document pages"
            )

            print("  Margins: None")

            print(
                "  Headers/Footers: Disabled"
            )

            print(
                "  ChromeDriver command timeout: "
                f"{DEFAULT_CDP_TIMEOUT_SECONDS}s"
            )

            driver.execute_script(
                "window.scrollTo(0, 0)"
            )

            saved_path = (
                save_pdf_pages_individually(
                    driver,
                    pdf_filename,
                )
            )

            if not saved_path:
                raise RuntimeError(
                    "PDF export failed."
                )

            print(
                "PDF saved successfully to: "
                f"{saved_path}"
            )

        except (
            RuntimeError,
            WebDriverException,
        ) as error:
            print(
                f"Export failed: {error}"
            )

            raise SystemExit(1)

        finally:
            if driver is not None:
                driver.quit()
                print("Browser closed.")


if __name__ == "__main__":
    main()
    
