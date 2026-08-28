"""The task an analysis is made from, parsed into what a prompt needs.

:class:`Analysis` wraps the raw task :mod:`~.task` read off the tracker: the description
as plain text, the images pulled out of it as files, the Excalidraw diagram it links to,
and who and what it is about.

Where the analysis comes from is a seam on purpose. :class:`AnalysisFactory` reads the
task and nothing else, which is all this plugin knows about; a plugin that keeps its
analyses somewhere richer overrides :meth:`AnalysisFactory.get_analysis` to look there
first and fall back to the task.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import html2text

from odev.common.logging import logging

from odev.plugins.odev_plugin_ai_scaffold.common.task import TaskReader


logger = logging.getLogger(__name__)

EXCALIDRAW_URL = re.compile(r"(https://app\.excalidraw\.com/[A-Za-z0-9/_\-#=]+)")

# The task reader inlines the images of the description as data URIs, so that the
# description travels as one self-contained string rather than a set of urls needing
# credentials to follow. This is where they are turned back into files.
IMAGE_DATA_URI = re.compile(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)")

MIMETYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
}


class AnalysisFactory:
    """Build an :class:`Analysis` from wherever the analysis of a task is kept."""

    @staticmethod
    def get_analysis(
        task_id: str,
        task_url: str,
        task_database: str = "",
        load_excalidraw: bool = True,
    ) -> Analysis | None:
        """Return the analysis of ``task_id``, or None when there is no such task.

        The task itself is the only source here. A plugin holding written analyses of
        its own overrides this to try those first, and calls super() for the fallback.
        """
        task = TaskReader(task_url, task_database).read(task_id)

        if task is None:
            return None

        logger.info(f"Task {task_id} found on {task_url}.")
        return Analysis(task_id, task, load_excalidraw)


class Analysis:
    """A task, parsed into the material an analysis prompt is built from."""

    data: dict[str, Any]
    client_name: str | None
    owner_name: str | None
    task_id: str
    odoo_version: str | None
    platform: str | None
    description: str | None
    embedded_images: list[tuple[str, str]]
    excalidraw_url: str | None
    load_excalidraw: bool

    def __init__(self, task_id: str, data: dict[str, Any], load_excalidraw: bool = True):
        self.data = data
        self.client_name = data.get("client")
        self.owner_name = self.parse_owner()
        self.task_id = task_id
        # Resolved from the databases the customer's subscription carries, and left
        # empty when it carries none: an analysis makes no sense without them, so the
        # command asks for whatever is missing.
        self.odoo_version = data.get("odoo_version")
        self.platform = data.get("platform")
        self.load_excalidraw = load_excalidraw
        self.excalidraw_url = None
        self.embedded_images = []
        self.description = self.parse()

    def parse_owner(self) -> str | None:
        """Return the task owner: its assignees, falling back to the task creator."""
        if assignees := self.data.get("assignee_names"):
            return ", ".join(assignees)

        # Many2one fields are read as an (id, display_name) pair.
        create_uid = self.data.get("create_uid")
        if isinstance(create_uid, list | tuple) and len(create_uid) == 2:
            return str(create_uid[1])

        return None

    def parse(self) -> str:
        """Return the description of the task as plain text, images pulled out of it."""
        description_html = self.data.get("description")

        if not description_html or not isinstance(description_html, str):
            return ""

        # Only the url is looked for here. Playwright is started later, when the diagram
        # is actually fetched: starting its sync API while the command may still prompt
        # for the version, platform or client leaves asyncio in a state where those
        # prompts fail with "asyncio.run() cannot be called from a running event loop".
        if self.load_excalidraw and (match := EXCALIDRAW_URL.search(description_html)):
            self.excalidraw_url = match.group(1)
            logger.info(f"Excalidraw url found in the description: {self.excalidraw_url}")

        description_html = self.extract_embedded_images(description_html)

        h = html2text.HTML2Text()
        h.ignore_links = True
        return h.handle(description_html).strip()

    def extract_embedded_images(self, description_html: str) -> str:
        """Move the data URIs of the description into :attr:`embedded_images`.

        The agent CLI takes the prompt as a single command line argument, which the
        kernel caps at 128kB: a couple of screenshots inlined as base64 are enough to
        get the whole run killed with E2BIG. They would be dead weight anyway, as an
        agent cannot read a data URI as an image. Replace each of them with a
        placeholder here, and :meth:`save_embedded_images` writes them next to the
        prompt as files the agent can actually open.
        """

        def replace(match: re.Match[str]) -> str:
            self.embedded_images.append((match.group(1), match.group(2)))
            return f"embedded-image-{len(self.embedded_images)}"

        description_html, count = IMAGE_DATA_URI.subn(replace, description_html)

        if count:
            logger.info(f"Extracted {count} image(s) embedded in the description.")

        return description_html

    def save_embedded_images(self, artifacts_dir: Path) -> list[Path]:
        """Write the extracted images to ``artifacts_dir`` and return their paths."""
        image_paths: list[Path] = []

        for index, (mimetype, payload) in enumerate(self.embedded_images, start=1):
            extension = MIMETYPE_EXTENSIONS.get(mimetype, f".{mimetype.removeprefix('image/')}")
            image_path = artifacts_dir / f"embedded-image-{index}{extension}"

            try:
                # The base64 of a data URI may be split over several lines.
                image_path.write_bytes(base64.b64decode("".join(payload.split())))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not save the image embedded in the description: {e}")
                continue

            image_paths.append(image_path)

        return image_paths

    @property
    def databases(self) -> list[dict[str, Any]]:
        """The databases the customer's subscription carries, url included."""
        return self.data.get("databases") or []

    @property
    def attachments(self) -> list[dict[str, Any]]:
        """Files attached to the task, as ``name`` / ``mimetype`` / ``datas`` dicts."""
        return self.data.get("attachments") or []
