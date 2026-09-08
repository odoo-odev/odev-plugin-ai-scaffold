"""Export the Excalidraw diagrams a task links to, as SVGs the agent can read.

A diagram is a live document: there is no url that hands back a picture, so the only
way to get one is to open the board in a browser and drive its own export. That is what
Playwright is here for, and it is the slowest thing in a run - a browser launch, a wait
for the scene to settle, then the export dialog - hence the spinner.

Two kinds of board, because a link decides which editor opens. A share link opens the
editor itself, which has the export dialog. An Excalidraw+ read-only link opens a viewer
that has no menu at all - only a zoom - so there is no dialog to drive: that page ships
the whole scene in its own html, and the fix is to read it from there and hand it to
excalidraw.com, which does have the dialog. See :func:`_readonly_scene`.

SVG rather than PNG, because the agent reads a diagram far better that way: the export
keeps every label of the board as text and every box as a shape with coordinates, so the
model reads the names of the models and the arrows between them instead of inferring
them from pixels. It is the same dialog and the same cost, and an SVG stays legible at
any zoom for the human who opens the artifact afterwards.

Nothing here writes to stdout. The browser is chatty by nature and every step of it is
a debug detail: on a normal run the user sees a spinner, and with ``--log-level debug``
they see why the export took as long as it did, or where it gave up.
"""

from __future__ import annotations

import json
import re
import unicodedata
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odev.common import progress
from odev.common.logging import logging


if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

EXCALIDRAW_URL = re.compile(
    r"https://(?:app\.|link\.|plus\.)?excalidraw\.com/[A-Za-z0-9/_\-#=?&]+(?:,[A-Za-z0-9_\-]+)?"
)
"""Matches an Excalidraw board linked from a task description.

Every host Excalidraw hands a board out on, because which one a link carries says
nothing about the board: ``excalidraw.com/#json=<id>,<key>`` is what the app itself
shares and what the Odoo editor stores for a Draw block, ``link.excalidraw.com/l/...``
is the shortened form of that same share, ``plus.excalidraw.com`` an Excalidraw+ board,
and ``app.`` an alias of the first. A link that is not the plain share redirects to one,
which the browser follows on its own - so all of them are worth opening.

The trailing group is the encryption key of a collaboration link, whose fragment reads
``#room=<id>,<key>``. Without it the board cannot be decrypted and the export opens an
empty canvas, so the comma is taken only when a key actually follows it - never the
comma that merely ends a sentence the url happens to sit in.
"""

DIAGRAM_FILENAME = "excalidraw-{index}-{name}.svg"
"""What an exported diagram is called on disk.

Numbered in the order the description links it, then named after the board itself -
Excalidraw suggests the title it was saved under, which is the only thing that tells
four diagrams of the same task apart. The prompt names the files it hands the agent, so
a board called "Dev flows" is a diagram the agent can refer to by name rather than by
the position it happened to hold in the description.
"""

FALLBACK_NAME = "diagram"
"""Stands in for the board's name when Excalidraw suggests none worth keeping."""

NAME_MAX_LENGTH = 60
"""How much of a board's name to keep, in characters, so a path stays a path."""

JOIN_TIMEOUT = 5000
"""How long to wait for the "Join room" dialog a shared board opens with, in ms."""

NETWORK_TIMEOUT = 15000
"""How long to wait for the images of a scene to load, in ms."""

REPAINT_DELAY = 500
"""How long to let the canvas repaint once its images have landed, in ms."""

NO_FILE_PICKER = "delete window.showSaveFilePicker; delete window.showOpenFilePicker;"
"""Hide the File System Access API from the page, so a save becomes a download.

Excalidraw saves through ``showSaveFilePicker`` wherever the browser has it - Chrome
does - and a headless one has no picker to show, so the call aborts and the export ends
in nothing at all: no file, no error on the page, just a download event that never
fires. Taking the api away leaves Excalidraw on its other branch, the anchor download
every browser without the api gets, which is the one Playwright can intercept.
"""

EDITOR_URL = "https://excalidraw.com"
"""Where a scene we hold ourselves is exported, having no board of its own to open."""

FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")')
"""One chunk of the payload the read-only viewer, a Next.js app, renders itself from.

Read out of the html of the page rather than out of the array those calls fill, because
React has drained that array by the time the board is on screen. The chunks live on in
the script tags that pushed them, each a javascript string literal - which is json, so
``json.loads`` is what gives back the text one holds.
"""

SCENE_CONTENTS = '"sceneContents":'
"""Where, in that payload, the scene of the board itself starts.

The value is an ``.excalidraw`` document, the very thing the editor's own "Save to file"
writes: type, version, appState and every element. Which is why the export can go
through the editor unchanged - it is handed a scene it recognises as one of its own.
"""

SCENE_METADATA = '"sceneMetadata":'
"""Where, in that payload, what Excalidraw+ knows *about* the board starts.

Only for its ``name``: a read-only export has no filename to be suggested from, so
without it every diagram of a task would land as ``excalidraw-<n>-diagram.svg``.
"""


def find_diagram_urls(description: str | None) -> list[str]:
    """Return every Excalidraw board linked from ``description``, in order, once each.

    A task may link several - one per flow, or a board per iteration of the same one -
    and taking only the first quietly loses the rest.

    Read from the html rather than from the text it becomes: a board pasted in the Odoo
    editor may still be a ``data-embedded="draw"`` block holding its url in a json
    attribute, which the html-to-text pass drops entirely. Entities are resolved first,
    so a query string written ``&amp;`` in the markup is followed as the ``&`` it is.
    """
    if not description:
        return []

    return list(dict.fromkeys(EXCALIDRAW_URL.findall(unescape(description))))


def export_diagrams(urls: list[str], directory: Path, odev=None) -> list[Path]:
    """Export each board of ``urls`` to an SVG in ``directory``, and return the paths.

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
                    exported = _export_one(browser, url)

                if exported is None:
                    logger.warning(f"Could not export the Excalidraw diagram at {url}.")
                    continue

                suggested, svg = exported
                path = directory / DIAGRAM_FILENAME.format(index=index, name=_slugify(suggested))
                path.write_bytes(svg)
                paths.append(path)
                logger.debug(f"Exported {url} to {path}")
    except Exception as e:  # noqa: BLE001 - a missing diagram must not cost us the run
        logger.warning(f"Could not start a browser to export the Excalidraw diagrams: {e}")

    if paths:
        logger.info(f"Exported {len(paths)} Excalidraw diagram(s).")

    return paths


def _slugify(filename: str) -> str:
    """Return the name Excalidraw saved a board under, as a name of our own.

    Accents are folded rather than dropped, so a board named in French keeps the word
    it was named with instead of the holes its accents leave behind.
    """
    stem = unicodedata.normalize("NFKD", Path(filename).stem).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:NAME_MAX_LENGTH].strip("-") or FALLBACK_NAME


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


def _export_one(browser: Any, url: str) -> tuple[str, bytes] | None:
    """Open ``url`` and export the board it holds, whichever kind of page it is.

    Returns the name the board is saved under, and the image bytes.
    """
    page = _new_page(browser)

    try:
        logger.debug(f"Loading {url}")
        page.goto(url)

        scene = _readonly_scene(page)

        if scene is not None:
            logger.debug("Read-only board: exporting its scene through the editor")
            return _export_scene(browser, scene)

        _join_room(page)

        logger.debug("Waiting for the drawing")
        page.locator("canvas").first.wait_for(state="visible")
        _settle(page)

        return _download_svg(page)
    except Exception as e:  # noqa: BLE001 - reported by the caller, per diagram
        logger.debug(f"Export of {url} failed: {e}", exc_info=True)
        return None
    finally:
        page.close()


def _new_page(browser: Any, scene: dict[str, Any] | None = None) -> Any:
    """Open a page with the file picker hidden, and ``scene`` waiting for the editor.

    A scene is planted in local storage rather than opened as a file, because that is
    where the editor looks first: it restores the board it was last left on, before it
    has drawn anything, so the canvas comes up on our scene and no dialog is involved.
    Written in an init script so it is there before the editor's own scripts run.
    """
    page = browser.new_page()
    page.add_init_script(NO_FILE_PICKER)

    if scene is not None:
        elements = json.dumps(json.dumps(scene.get("elements", [])))
        app_state = json.dumps(json.dumps(scene.get("appState", {})))
        page.add_init_script(
            # Guarded: local storage throws on the about:blank a page starts its life on.
            f"try {{ localStorage.setItem('excalidraw', {elements});"
            f" localStorage.setItem('excalidraw-state', {app_state}); }} catch (e) {{}}"
        )

    return page


def _readonly_scene(page: Any) -> dict[str, Any] | None:
    """Return the scene an Excalidraw+ read-only viewer ships in its own page, if any.

    None for every other kind of board - the editor is on the page, and driving its
    dialog is both cheaper and truer to what the board looks like.

    The images of a scene do not come along: the viewer holds them encrypted and fetches
    the key on its own terms, so they export as the empty frames they are here. Every
    label, shape and arrow does, which is what the agent reads a diagram for.
    """
    try:
        payload = "".join(json.loads(chunk) for chunk in FLIGHT_CHUNK.findall(page.content()))
    except Exception as e:  # noqa: BLE001 - not a viewer page, then
        logger.debug(f"Could not read the payload of the page: {e}")
        return None

    scene = _json_at(payload, SCENE_CONTENTS)

    if not isinstance(scene, dict) or "elements" not in scene:
        return None

    metadata = _json_at(payload, SCENE_METADATA)
    name = metadata.get("name") if isinstance(metadata, dict) else None

    if name:
        # What the editor suggests as a filename, and so what the diagram is named after.
        scene.setdefault("appState", {})["name"] = name

    logger.debug(f"Read {len(scene['elements'])} elements of read-only board {name or '(unnamed)'}")

    return scene


def _json_at(payload: str | None, marker: str) -> Any:
    """Return the json object that follows ``marker`` in ``payload``, or None.

    The payload is a stream of react output rather than a document: the objects that
    interest us sit somewhere inside it, and only a decoder that stops of its own accord
    at the end of a value can pick one out of it.
    """
    if not payload or marker not in payload:
        return None

    try:
        return json.JSONDecoder().raw_decode(payload, payload.index(marker) + len(marker))[0]
    except ValueError as e:
        logger.debug(f"Could not read {marker} out of the page: {e}")
        return None


def _export_scene(browser: Any, scene: dict[str, Any]) -> tuple[str, bytes] | None:
    """Export a scene we hold ourselves, by handing it to the editor on excalidraw.com."""
    page = _new_page(browser, scene=scene)

    try:
        logger.debug(f"Loading the editor at {EDITOR_URL}")
        page.goto(EDITOR_URL)
        page.locator("canvas").first.wait_for(state="visible")
        _settle(page)

        return _download_svg(page)
    finally:
        page.close()


def _join_room(page: Any) -> None:
    """Get past the "Join room" dialog a shared board opens on, if there is one.

    The canvas stays behind it, and an export driven over it exports nothing.
    """
    try:
        join = page.get_by_role("button", name=re.compile("Join", re.IGNORECASE))
        join.wait_for(state="visible", timeout=JOIN_TIMEOUT)
        join.click()
        logger.debug("Joined the room")
        page.wait_for_timeout(2000)
    except Exception:  # noqa: BLE001 - most boards open without one
        logger.debug("No 'Join' dialog, proceeding")


def _settle(page: Any) -> None:
    """Wait for the scene to finish arriving on the canvas.

    The images of a scene are fetched asynchronously after the canvas becomes visible:
    exporting right away bakes their "broken image" placeholder into the export instead
    of the picture. Wait for the network to settle rather than guessing a delay; the
    short wait after is for the canvas to repaint once the data lands, which is neither
    a network nor a DOM event to wait on.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_TIMEOUT)
    except Exception:  # noqa: BLE001
        logger.debug("Network did not settle in time, exporting anyway")

    page.wait_for_timeout(REPAINT_DELAY)


def _download_svg(page: Any) -> tuple[str, bytes]:
    """Drive the editor's own SVG export, and return the name it saves under and bytes."""
    logger.debug("Opening the export dialog")
    page.keyboard.press("Control+Shift+E")

    with page.expect_download() as download:
        page.locator('[aria-label="Export to SVG"]').click()

    return download.value.suggested_filename, Path(download.value.path()).read_bytes()
