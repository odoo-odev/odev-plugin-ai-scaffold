"""Handing the task analysis in by hand instead of reading it off the tracker."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from odev.common import args

from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis, AnalysisFactory


if TYPE_CHECKING:
    from argparse import Namespace

    from odev.common.errors import CommandError


class PasteAnalysisMixin:
    """Build the analysis from content handed in, bypassing the tracker and its task id.

    ``--paste`` opens an editor, ``--paste-file`` reads a file; either holds the task as
    text or HTML. Shared by the commands that otherwise fetch the analysis by task id,
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
        description="Paste the task analysis into an editor instead of fetching it from the tracker.",
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
        """Return the analysis pasted by hand, from a file or typed into an editor.

        ``--paste-file`` reads it straight off disk. ``--paste`` opens an editor on an
        empty temporary file - ``$EDITOR``, then ``$VISUAL``, then ``nano`` - and reads
        back whatever was saved, the temp file cleaned up either way. The content is the
        whole analysis, text or HTML, parsed the same as a task description would be.
        """
        if self.args.paste_file:
            if not self.args.paste_file.is_file():
                raise self.error(f"No analysis file at {self.args.paste_file}")

            content = self.args.paste_file.read_text()
        else:
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
            file_descriptor, temp_name = tempfile.mkstemp(suffix=".txt", prefix="scaffold-analysis-")
            os.close(file_descriptor)
            temp_path = Path(temp_name)

            try:
                subprocess.run([*shlex.split(editor), temp_name], check=True)
                content = temp_path.read_text()
            finally:
                temp_path.unlink(missing_ok=True)

        if not content.strip():
            raise self.error("The pasted analysis is empty.")

        return content
