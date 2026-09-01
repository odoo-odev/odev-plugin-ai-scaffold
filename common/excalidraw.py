"""Export the Excalidraw diagrams a task links to, as PNGs the agent can open.

A diagram is a live document: there is no url that hands back a picture, so the only
way to get one is to open the board in a browser and drive its own export. That is what
Playwright is here for, and it is the slowest thing in a run - a browser launch, a wait
for the scene to settle, then the export dialog - hence the spinner.

Nothing here writes to stdout. The browser is chatty by nature and every step of it is
a debug detail: on a normal run the user sees a spinner, and with ``--log-level debug``
they see why the export took as long as it did, or where it gave up.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odev.common import progress
from odev.common.logging import logging


if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

EXCALIDRAW_URL = re.compile(r"https://app\.excalidraw\.com/[A-Za-z0-9/_\-#=]+(?:,[A-Za-z0-9_\-]+)?")
"""Matches an Excalidraw board linked from a task description.

The trailing group is the encryption key of a collaboration link, whose fragment reads
``#room=<id>,<key>``. Without it the board cannot be decrypted and the export opens an
empty canvas, so the comma is taken only when a key actually follows it - never the
comma that merely ends a sentence the url happens to sit in.
"""

DIAGRAM_FILENAME = "excalidraw-diagram-{index}.png"
"""What an exported diagram is called, numbered in the order the description links it."""

JOIN_TIMEOUT = 5000
"""How long to wait for the "Join room" dialog a shared board opens with, in ms."""

NETWORK_TIMEOUT = 15000
"""How long to wait for the images of a scene to load, in ms."""

REPAINT_DELAY = 500
"""How long to let the canvas repaint once its images have landed, in ms."""


def find_diagram_urls(description: str | None) -> list[str]:
    """Return every Excalidraw board linked from ``description``, in order, once each.

    A task may link several - one per flow, or a board per iteration of the same one -
    and taking only the first quietly loses the rest.
    """
    if not description:
        return []

    return list(dict.fromkeys(EXCALIDRAW_URL.findall(description)))


def export_diagrams(urls: list[str], directory: Path, odev=None) -> list[Path]:
    """Export each board of ``urls`` to a PNG in ``directory``, and return the paths.

    One browser for all of them: launching it is most of the cost, and a task linking
    four diagrams should not pay it four times. A board that cannot be exported is
    skipped with a warning rather than failing the run - an analysis without its diagram
    is worth more than no analysis.

    :param odev: The odev instance, to reuse the Chrome it provisions. Falls back on
        Playwright's own bundled Chromium when not given, or when odev has none.
    """
    if not urls:
        return []

    paths: list[Path] = []

    try:
        with _browser(odev) as browser:
            for index, url in enumerate(urls, start=1):
                label = f"Exporting Excalidraw diagram {index}/{len(urls)}"

                with progress.spinner(label):
                    png = _export_one(browser, url)

                if png is None:
                    logger.warning(f"Could not export the Excalidraw diagram at {url}.")
                    continue

                path = directory / DIAGRAM_FILENAME.format(index=index)
                path.write_bytes(png)
                paths.append(path)
                logger.debug(f"Exported {url} to {path}")
    except Exception as e:  # noqa: BLE001 - a missing diagram must not cost us the run
        logger.warning(f"Could not start a browser to export the Excalidraw diagrams: {e}")

    if paths:
        logger.info(f"Exported {len(paths)} Excalidraw diagram(s).")

    return paths


@contextmanager
def _browser(odev=None) -> Iterator[Any]:
    """Start Playwright and yield a Chrome instance, closing both afterwards.

    Chrome, and where possible the very build odev already provisions for tours - the
    version Runbot pins - rather than a browser of our own. It is what Odoo is developed
    against, and reusing it means no second browser to install: `playwright install` is
    only needed when odev has no Chrome to lend.
    """
    # Imported here rather than at module level: Playwright is a heavy import, and a
    # task linking no diagram should not pay for it just to load the plugin.
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    executable = _odev_chrome(odev)

    logger.debug(f"Starting Playwright (Chrome{f' at {executable}' if executable else ', bundled'})")
    playwright = sync_playwright().start()

    try:
        browser = playwright.chromium.launch(headless=True, executable_path=executable)

        try:
            yield browser
        finally:
            browser.close()
    finally:
        playwright.stop()
        logger.debug("Stopped Playwright")


def _odev_chrome(odev=None) -> str | None:
    """Return the Chrome odev provisions, or None to use Playwright's own.

    Under a spinner because the first call downloads the browser, and ``Chrome.provision``
    captures the output of the npx doing it: without one, a first run sits silent for the
    length of a 150MB download with nothing on screen to say why. The message says which
    download it is, since a wait that explains itself is a different thing from a hang.
    """
    if odev is None:
        return None

    try:
        from odev.common.browsers import Chrome  # noqa: PLC0415

        with progress.spinner("Preparing the browser") as status:
            chrome = Chrome(odev)

            if not chrome.executable.exists():
                status.update(
                    f"Downloading Chrome {chrome.version} - first run only, "
                    "and shared with the browser odev runs tours with"
                )

            executable = chrome.provision()
    except Exception as e:  # noqa: BLE001 - Playwright's own Chromium is a fine fallback
        logger.debug(f"Could not provision odev's Chrome: {e}", exc_info=True)
        return None

    if not executable:
        logger.debug("odev has no Chrome to lend, falling back on Playwright's own Chromium.")

    return str(executable) if executable else None


def _export_one(browser: Any, url: str) -> bytes | None:
    """Open ``url`` and drive Excalidraw's own PNG export, returning the image bytes."""
    page = browser.new_page()

    try:
        logger.debug(f"Loading {url}")
        page.goto(url)

        # A shared board opens on a "Join room" dialog, and the canvas stays behind it.
        try:
            join = page.get_by_role("button", name=re.compile("Join", re.IGNORECASE))
            join.wait_for(state="visible", timeout=JOIN_TIMEOUT)
            join.click()
            logger.debug("Joined the room")
            page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001 - most boards open without one
            logger.debug("No 'Join' dialog, proceeding")

        logger.debug("Waiting for the drawing")
        page.locator("canvas").first.wait_for(state="visible")

        # The images of a scene are fetched asynchronously after the canvas becomes
        # visible: exporting right away bakes their "broken image" placeholder into the
        # PNG instead of the picture. Wait for the network to settle rather than
        # guessing a delay; the short wait after is for the canvas to repaint once the
        # data lands, which is neither a network nor a DOM event to wait on.
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_TIMEOUT)
        except Exception:  # noqa: BLE001
            logger.debug("Network did not settle in time, exporting anyway")

        page.wait_for_timeout(REPAINT_DELAY)

        logger.debug("Opening the export dialog")
        page.keyboard.press("Control+Shift+E")

        with page.expect_download() as download:
            page.locator('[aria-label="Export to PNG"]').click()

        return Path(download.value.path()).read_bytes()
    except Exception as e:  # noqa: BLE001 - reported by the caller, per diagram
        logger.debug(f"Export of {url} failed: {e}", exc_info=True)
        return None
    finally:
        page.close()
