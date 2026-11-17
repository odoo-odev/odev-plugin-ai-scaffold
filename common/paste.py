"""Handing the task analysis in by hand instead of reading it off the tracker."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

from odev.common import args

from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis, AnalysisFactory


if TYPE_CHECKING:
    from argparse import Namespace

    from odev.common.errors import CommandError


class PasteAnalysisMixin:
    """Build the analysis from content handed in, bypassing the tracker and its task id.

    ``--paste`` reads the clipboard, ``--paste-file`` reads a file; either holds the task
    as text or HTML. Shared by the commands that otherwise fetch the analysis by task id,
    so a run with no access to the task - or an analysis written on the fly - still has
    one. A command mixing this in makes its ``task_id`` optional and asks for a pasted
    analysis when none was given.
    """

    if TYPE_CHECKING:
        args: Namespace

        def error(self, message: str, *args: Any, **kwargs: Any) -> CommandError: ...

    analysis_factory: type[AnalysisFactory]
    """Set by the command; a pasted analysis is built through its ``from_pasted``."""

    paste = args.Flag(
        aliases=["--paste"],
        description="Read the task analysis from the clipboard (its HTML, with images and links, kept) "
        "instead of fetching it from the tracker.",
        default=False,
    )
    paste_file = args.Path(
        aliases=["--paste-file"],
        description="Read the task analysis from a file (plain text or HTML) instead of the tracker.",
    )

    @property
    def is_pasted(self) -> bool:
        """Whether the analysis is handed in rather than read off the tracker."""
        return bool(self.args.paste or self.args.paste_file)

    def _pasted_analysis(self) -> Analysis:
        """Return the analysis built from the content pasted on the command line.

        The Excalidraw links the pasted content carries are exported like a task's,
        unless ``--no-excalidraw`` turns that off - both commands that paste define it.
        """
        return self.analysis_factory.from_pasted(
            self._read_pasted_analysis(),
            load_excalidraw=not self.args.no_excalidraw,
        )

    def _read_pasted_analysis(self) -> str:
        """Return the analysis handed in, from a file or the clipboard.

        ``--paste-file`` reads it straight off disk. ``--paste`` reads the clipboard,
        preferring its HTML flavour - where the ``<img>`` links of a description copied
        from the browser survive, a plain-text paste having already lost them - and
        falling back to plain text when there is no HTML. Either way the content is the
        whole analysis, parsed the same as a task description would be.
        """
        if self.args.paste_file:
            if not self.args.paste_file.is_file():
                raise self.error(f"No analysis file at {self.args.paste_file}")

            content = self.args.paste_file.read_text()
        else:
            content = self._read_clipboard()

        if not content.strip():
            raise self.error("The pasted analysis is empty.")

        return content

    def _read_clipboard(self) -> str:
        """Return what the clipboard holds, read through whatever tool is on the box.

        The HTML flavour is asked for first, across ``wl-paste`` (Wayland) then ``xclip``
        (X11) - it is what keeps the image links a plain-text paste drops - and plain
        text is taken only when no tool returns HTML, so a description with no markup
        still goes through. A missing tool or an empty answer just hands over to the
        next. Nothing readable at all is an error pointing at the fix, the clipboard
        being a graphical thing a bare SSH session does not have.
        """
        for command in (
            ["wl-paste", "--type", "text/html"],
            ["xclip", "-selection", "clipboard", "-o", "-t", "text/html"],
            ["wl-paste"],
            ["xclip", "-selection", "clipboard", "-o"],
        ):
            try:
                result = subprocess.run(command, capture_output=True, check=True)
            except FileNotFoundError:
                # The tool is not installed: try the next one.
                continue
            except subprocess.CalledProcessError:
                # The tool ran but gave nothing back - that flavour is not on the
                # clipboard, or no display it can reach (wl-paste on an X11 session):
                # try the next one, which may be the one that fits this box.
                continue

            if result.stdout.strip():
                return result.stdout.decode("utf-8", errors="replace")

        raise self.error(
            "Could not read the clipboard. Copy the task description first, and make sure wl-clipboard "
            "(Wayland) or xclip (X11) is installed; over SSH there is no clipboard, so use --paste-file instead."
        )
