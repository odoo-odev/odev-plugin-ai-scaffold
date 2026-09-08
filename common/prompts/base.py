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

METHOD_SKILL = "odoo_task_analysis"
"""Skill carrying the method an analysis is made by.

The prompt states the facts of a run: the task and its description, the client, the
target version, the hosting, where the diagrams and the standard source were mounted,
the throughputs to estimate with and where the finished analysis goes. Everything that
is the same from one run to the next - what to read before concluding, what each
hosting allows, how an estimate is grounded, what the analysis has to contain - lives
in that skill instead, and the command installs it before the agent starts: see
``AnalyzeCommand.required_skills``.
"""

SAAS_SKILL = "odoo_saas_development"
"""Skill carrying what a data-only module can express, which is what SaaS deploys."""

PLATFORM_LABELS = {
    "saas": "Odoo Online (SaaS)",
    "sh": "Odoo.sh",
    "op": "On-Premise",
}
"""Hosting of the client database, keyed by the platform of that database.

Only named here. What each hosting allows is method, stated once in the "What the
hosting allows" section of the ``odoo_task_analysis`` skill, so the prompt says which
one applies rather than repeating the rules of all three on every run.
"""


class BasePrompt:
    """Build the analysis prompt, one section per method.

    Acts as a prototype: a subclass tailors the prompt for a given Odoo version, and a
    plugin tailors it for wherever the analysis has to end up.
    """

    def __init__(self, version: OdooVersion | None = None, odev=None):
        self.version = version
        self.odev = odev
        self.platform: str | None = None
        self.source_path: Path | None = None
        self.comment: str | None = None
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
        comment: str | None = None,
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
            comment: What the developer running the command asked for on the command
                line, which outranks the rest of the prompt. Nothing is said about it
                when omitted.

        Returns:
            str: The prompt to hand over to the agent CLI.
        """
        self.analysis_obj = analysis_obj
        self.platform = platform
        self.source_path = source_path
        self.comment = comment

        if loc_per_hour_python is not None:
            self.loc_per_hour_python = loc_per_hour_python
        if loc_per_hour_xml is not None:
            self.loc_per_hour_xml = loc_per_hour_xml
        if loc_per_hour_js is not None:
            self.loc_per_hour_js = loc_per_hour_js
        if minimum_dev_hours is not None:
            self.minimum_dev_hours = minimum_dev_hours

        # The comment comes first, and is the frame the rest is read in: that is what
        # "takes precedence" has to mean when everything is one prompt.
        sections: dict[str, list[str]] = {
            "priority instructions": self._get_comment_prompt(),
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

    def _get_comment_prompt(self) -> list[str]:
        """Return what the developer asked for on the command line, if anything.

        The task is what a client wrote and the method is what is always true on every
        run; the comment is what the person running this analysis knows on top of both -
        a requirement to leave out, a direction already agreed with the client, a part
        already built. It is stated first and as outranking the rest, so a conflict with
        the task description resolves the way the developer meant it to.
        """
        if not self.comment:
            return []

        return [
            "The developer running this analysis gave the instructions below. They take precedence over "
            "everything that follows - the task description, the method skill and the rest of this "
            "prompt - wherever they disagree, and the rest still applies where they are silent.",
            f"Instructions:\n{self.comment}",
        ]

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
            points.append(
                f"Hosting: {PLATFORM_LABELS.get(self.platform, self.platform)}. What it allows is the "
                f"section under that name in the `{METHOD_SKILL}` skill; read it before proposing an "
                "implementation."
            )
        if analysis.description:
            points.append(f"Task description:\n{analysis.description}")

        return points

    def _get_main_prompt(self) -> list[str]:
        points = [
            "You are analyzing an Odoo task: say what has to be built for the requirements above, and what it costs.",
            f"Load the `{METHOD_SKILL}` skill and work by it. It holds the method, the same on every run; "
            "this prompt holds only the facts of this one.",
        ]

        if self.source_path:
            points.append(
                f"The standard Odoo {self.version or ''} source is mounted read-only at {self.source_path}, "
                "as odoo/, enterprise/ and design-themes/."
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

        diagram_paths = self.analysis_obj.export_excalidraw_diagrams(artifacts_dir, self.odev)

        if not diagram_paths:
            return []

        return [
            f"{len(diagram_paths)} Excalidraw architecture diagram(s) were exported for you to: "
            f"{', '.join(f'`{path}`' for path in diagram_paths)}. Read those files: they are SVG, "
            "so every label of the board is text you can read straight out of the markup.",
            "Carefully analyze the components, relationships, and text within them.",
            "Ensure the proposed Odoo models and views match the technical structure they show.",
        ]

    def _get_reporting_prompt(self) -> list[str]:
        """Where the analysis goes, and what this run estimates with.

        Written out in the conversation, for the developer to read and argue with: an
        analysis is a proposal, and this is where it is discussed before it is worth
        recording anywhere. A plugin that has somewhere to deliver it to overrides this.

        What an analysis has to contain is the ``odoo_task_analysis`` skill's, not this
        prompt's; what is here is the delivery target and the numbers odev.cfg
        configured this run with.
        """
        return [
            f"Write the analysis as markdown, in your response, in the shape the `{METHOD_SKILL}` skill "
            "asks for: there is nowhere to deliver it to in this run.",
            f"Estimate with the throughputs this run is configured for: {self.loc_per_hour_python:g} Python "
            f"lines/hour, {self.loc_per_hour_xml:g} XML lines/hour, {self.loc_per_hour_js:g} JavaScript "
            f"lines/hour, and a total that does not fall below {self.minimum_dev_hours:g} hours.",
        ]
