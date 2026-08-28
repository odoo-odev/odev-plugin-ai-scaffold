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
from pathlib import Path
from typing import Any

import html2text

from odev.common.logging import logging

from odev.plugins.odev_plugin_ai_scaffold.common.excalidraw import export_diagrams, find_diagram_urls
from odev.plugins.odev_plugin_ai_scaffold.common.task import TaskReader


logger = logging.getLogger(__name__)

MANY2ONE_PAIR_SIZE = 2
"""A many2one field is read over RPC as an ``(id, display_name)`` pair."""


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
    excalidraw_urls: list[str]
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
        self.excalidraw_urls = []
        self.description = self.parse()

    def parse_owner(self) -> str | None:
        """Return the task owner: its assignees, falling back to the task creator."""
        if assignees := self.data.get("assignee_names"):
            return ", ".join(assignees)

        # Many2one fields are read as an (id, display_name) pair.
        create_uid = self.data.get("create_uid")
        if isinstance(create_uid, list | tuple) and len(create_uid) == MANY2ONE_PAIR_SIZE:
            return str(create_uid[1])

        return None

    def parse(self) -> str:
        """Return the description of the task as plain text, images pulled out of it."""
        description_html = self.data.get("description")

        if not description_html or not isinstance(description_html, str):
            return ""

        # Only the urls are collected here. Playwright is started later, when the
        # diagrams are actually exported: starting its sync API while the command may
        # still prompt for the version, platform or client leaves asyncio in a state
        # where those prompts fail with "asyncio.run() cannot be called from a running
        # event loop".
        if self.load_excalidraw:
            self.excalidraw_urls = find_diagram_urls(description_html)

            if self.excalidraw_urls:
                logger.info(f"Found {len(self.excalidraw_urls)} Excalidraw url(s) in the description.")

        h = html2text.HTML2Text()
        h.ignore_links = True
        return h.handle(description_html).strip()

    def save_embedded_images(self, artifacts_dir: Path) -> list[Path]:
        """Write the images of the description to ``artifacts_dir``, return their paths.

        The description refers to each of them by the file name written here, so the
        agent can open the picture the text is talking about.
        """
        image_paths: list[Path] = []

        for image in self.embedded_images:
            image_path = artifacts_dir / image["name"]

            try:
                image_path.write_bytes(base64.b64decode(image["datas"]))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Could not save the image embedded in the description: {e}")
                continue

            image_paths.append(image_path)

        return image_paths

    def export_excalidraw_diagrams(self, artifacts_dir: Path) -> list[Path]:
        """Export every diagram the description links to, and return their paths.

        A diagram is a live document, not something the description can carry: the
        board is opened in a browser and exported through Excalidraw itself.
        """
        return export_diagrams(self.excalidraw_urls, artifacts_dir)

    @property
    def embedded_images(self) -> list[dict[str, Any]]:
        """Images pulled out of the description, as ``name`` / ``mimetype`` / ``datas``.

        The description refers to each by its ``name``, where the picture stood.
        """
        return self.data.get("description_images") or []

    @property
    def databases(self) -> list[dict[str, Any]]:
        """The databases the customer's subscription carries, url included."""
        return self.data.get("databases") or []

    @property
    def attachments(self) -> list[dict[str, Any]]:
        """Files attached to the task, as ``name`` / ``mimetype`` / ``datas`` dicts."""
        return self.data.get("attachments") or []
