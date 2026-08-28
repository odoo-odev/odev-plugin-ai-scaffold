"""Build the prompt an analysis run hands to the agent.

The prompt is assembled from sections, one method each, so a plugin extending this one
can replace the part it cares about without rewriting the rest. :meth:`_get_reporting_prompt`
is the seam that matters most: it says what to do with the finished analysis, and that
is the one thing a plugin delivering analyses somewhere - a tracker, a database - has to
say differently.
"""

from __future__ import annotations

from pathlib import Path

from odev.common.logging import logging
from odev.common.version import OdooVersion

from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis


logger = logging.getLogger(__name__)

PLATFORM_RULES = {
    "saas": (
        "Odoo Online (SaaS): no custom module and no custom code are deployable, "
        "so everything has to be achieved with Studio, automation rules and "
        "server actions. Say so explicitly for any requirement that cannot be."
    ),
    "sh": (
        "Odoo.sh: a custom module is deployable, and the estimation has to account "
        "for the branch, the build and the deployment of that module. Account for "
        "that once - its own line, or a caveat in the analysis - never folded into "
        "the description or estimate of an unrelated one: a post_init_hook stays "
        "scoped to what its code does, not to how the module reaches the client."
    ),
    "op": (
        "On-Premise: a custom module is deployable, but nothing about the hosting, "
        "the deployment or the third-party modules already installed can be assumed."
    ),
}
"""What each hosting allows, keyed by the platform of the client's database."""


class BasePrompt:
    """Build the analysis prompt, one section per method.

    Acts as a prototype: a subclass tailors the prompt for a given Odoo version, and a
    plugin tailors it for wherever the analysis has to end up.
    """

    def __init__(self, version: OdooVersion | None = None):
        self.version = version
        self.platform: str | None = None
        self.source_path: Path | None = None
        # Overridable defaults: the command passes the user's odev.cfg values through
        # build_prompt, these only cover direct use of the class (e.g. tests).
        self.loc_per_hour_python: float = 20
        self.loc_per_hour_xml: float = 50
        self.loc_per_hour_js: float = 20
        self.minimum_dev_hours: float = 4

    def build_prompt(  # noqa: PLR0913 - the estimation throughputs all come from odev.cfg
        self,
        analysis_obj: Analysis,
        artifacts_dir: Path | None = None,
        platform: str | None = None,
        source_path: Path | None = None,
        loc_per_hour_python: float | None = None,
        loc_per_hour_xml: float | None = None,
        loc_per_hour_js: float | None = None,
        minimum_dev_hours: float | None = None,
    ) -> str:
        """Construct the full prompt sent to the sandboxed AI agent.

        Args:
            analysis_obj: The task analysis to build the prompt from.
            artifacts_dir: Directory the agent has access to, where the images of the
                description and the exported diagrams are written. Both are skipped
                when omitted, since the agent would have nowhere to read them from.
            platform: Hosting of the client database, which decides what the analysis
                may propose. No hosting constraint is stated if omitted.
            source_path: Worktree holding the standard Odoo source of the target
                version, mounted read-only in the sandbox. The agent is told to read it
                rather than guess what Odoo already does; omitted when no worktree
                could be prepared.
            loc_per_hour_python: Throughput used to ground a time estimate in a line
                count for Python code, read from ``[ai_scaffold] loc_per_hour_python``
                in odev.cfg. Falls back to the class default when omitted.
            loc_per_hour_xml: Same as above, for XML.
            loc_per_hour_js: Same as above, for JavaScript.
            minimum_dev_hours: Floor put on the total estimated development time.

        Returns:
            str: The prompt to hand over to the agent CLI.
        """
        self.analysis_obj = analysis_obj
        self.platform = platform
        self.source_path = source_path

        if loc_per_hour_python is not None:
            self.loc_per_hour_python = loc_per_hour_python
        if loc_per_hour_xml is not None:
            self.loc_per_hour_xml = loc_per_hour_xml
        if loc_per_hour_js is not None:
            self.loc_per_hour_js = loc_per_hour_js
        if minimum_dev_hours is not None:
            self.minimum_dev_hours = minimum_dev_hours

        sections: dict[str, list[str]] = {
            "task": self._get_task_prompt(),
            "instructions": self._get_main_prompt(),
            "screenshots": self._get_embedded_images_prompt(artifacts_dir),
        }

        if analysis_obj.load_excalidraw:
            sections["diagrams"] = self._get_excalidraw_prompt(artifacts_dir)

        sections["reporting"] = self._get_reporting_prompt()

        content: list[str] = []

        for title, points in sections.items():
            if points:
                content.append(f"## {title.capitalize()}")
                content.extend(f"- {point}" for point in points)

        return "\n".join(content)

    def _get_task_prompt(self) -> list[str]:
        analysis = self.analysis_obj
        points = [f"Task id: {analysis.task_id}"]

        if analysis.client_name:
            points.append(f"Client: {analysis.client_name}")
        if analysis.owner_name:
            points.append(f"Task owner: {analysis.owner_name}")
        if self.version:
            points.append(f"Target Odoo version: {self.version}")
        if self.platform:
            points.append(f"Hosting: {PLATFORM_RULES.get(self.platform, self.platform)}")
        if analysis.description:
            points.append(f"Task description:\n{analysis.description}")

        return points

    def _get_main_prompt(self) -> list[str]:
        points = [
            "You are tasked with analyzing the provided data, requirements, and diagrams "
            "to perform a comprehensive Odoo development analysis.",
            "Read the relevant source code available in your working directory before drawing conclusions.",
            "Identify the impacted Odoo models, views, and modules, and flag any missing requirement.",
        ]

        if self.source_path:
            points.append(
                f"The standard Odoo {self.version or ''} source is mounted read-only at {self.source_path} "
                "(odoo/, enterprise/ and design-themes/). Read it to tell what Odoo already does from what "
                "has to be built: only the second is estimated. Do not attempt to modify it."
            )

        return points

    def _get_embedded_images_prompt(self, artifacts_dir: Path | None = None) -> list[str]:
        """Save the images of the description to disk and tell the agent where they are.

        The description refers to them by file name, so the agent can tell which image
        belongs to which paragraph.
        """
        analysis = self.analysis_obj

        if not analysis.embedded_images or artifacts_dir is None:
            return []

        image_paths = analysis.save_embedded_images(artifacts_dir)

        if not image_paths:
            return []

        return [
            f"The task description embeds {len(image_paths)} image(s), exported for you to: "
            f"{', '.join(f'`{path}`' for path in image_paths)}.",
            "Read them: the description refers to each of them by that same file name, where the picture belonged.",
        ]

    def _get_excalidraw_prompt(self, artifacts_dir: Path | None = None) -> list[str]:
        """Export the linked diagrams to disk and tell the agent where to find them."""
        if artifacts_dir is None or not self.analysis_obj.excalidraw_urls:
            return []

        diagram_paths = self.analysis_obj.export_excalidraw_diagrams(artifacts_dir)

        if not diagram_paths:
            return []

        return [
            f"{len(diagram_paths)} Excalidraw architecture diagram(s) were exported for you to: "
            f"{', '.join(f'`{path}`' for path in diagram_paths)}. Read those images.",
            "Carefully analyze the components, relationships, and text within them.",
            "Ensure the proposed Odoo models and views match the technical structure they show.",
        ]

    def _get_reporting_prompt(self) -> list[str]:
        """Instructions on what to do with the finished analysis.

        Written out in the conversation, for the developer to read and argue with: an
        analysis is a proposal, and this is where it is discussed before it is worth
        recording anywhere. A plugin that has somewhere to deliver it to overrides this.
        """
        return [
            "Write the analysis as markdown, in your response, structured per functional requirement.",
            "For each requirement, state the impacted models and fields, the proposed implementation, "
            "and an estimate in hours.",
            "Ground each estimate in the lines of code the requirement takes to write, not a round "
            f"guess: count the lines, divide by a throughput of {self.loc_per_hour_python:g} Python "
            f"lines/hour, {self.loc_per_hour_xml:g} XML lines/hour or {self.loc_per_hour_js:g} JavaScript "
            "lines/hour - whichever the code is written in - and round to the nearest quarter hour.",
            f"The total development time should not fall below {self.minimum_dev_hours:g} hours: even a "
            "small requirement carries setup, testing and review overhead that per-line estimates alone "
            "tend to undercut. Raise the smallest item rather than inflate every one.",
            "Say once, up front, whether this is built as a new custom module or as a change to one that "
            "already exists - read the source at hand rather than guessing. A new module is the default; "
            "name an existing one only when extending it genuinely makes more sense.",
            "State the assumptions you had to make and the requirements you left out of the estimation. "
            "Keep that to a few sentences: it is read next to the analysis, not instead of it.",
        ]
