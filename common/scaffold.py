"""Arguments and analysis loading shared by ``scaffold`` and ``quickstart``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from odev.common import args, progress
from odev.common.version import OdooVersion

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin
from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis, AnalysisFactory


if TYPE_CHECKING:
    from argparse import Namespace

    from odev.common.errors import CommandError


class Scaffold(AICommandMixin):
    """Command line arguments of the scaffolding commands, and the analysis behind them."""

    if TYPE_CHECKING:
        args: Namespace
        config: Any

        def error(self, message: str, *args: Any, **kwargs: Any) -> CommandError: ...

    format = args.String(
        aliases=["--format"],
        description="The output format (importable or python module).",
        choices=["xml", "py"],
        default="",
    )
    no_migrate_code = args.Flag(
        aliases=["-M"],
        description="Do not migrate the manual / studio fields into python fields.",
        default=False,
    )
    path = args.Path(
        aliases=["--path"],
        description="Path to the export template.",
        default=Path(".").resolve(),
    )
    version = args.String(
        aliases=["-V", "--version"],
        description="Target version of the export template.",
        default="",
    )
    depends = args.List(
        aliases=["--depends"],
        description="Comma separated value for the modules files used as context",
        default=[],
    )
    module = args.String(
        aliases=["-m", "--module"],
        description="Existing module name to override",
    )
    demo = args.Flag(
        aliases=["-D", "--demo"],
        description="Include the generation of demo data files (CSV/XML) for the module.",
        default=False,
    )
    tests = args.Flag(
        aliases=["-t", "--tests"],
        description="Generate standard Odoo unit tests based on the task requirements.",
        default=False,
    )
    no_excalidraw = args.Flag(
        aliases=["-e", "--no-excalidraw"],
        description="Do not export the Excalidraw diagrams the task links to",
        default=False,
    )
    task_id = args.String(description="Id of the task to scaffold from", nargs="?")

    analysis_factory: type[AnalysisFactory] = AnalysisFactory
    """Where analyses are read from. A plugin with another source swaps this out."""

    analysis_obj: Analysis | None = None
    depends_list: list[str] | None = None
    is_importable: bool = False
    override_name: str | None = None
    target_version: OdooVersion | None = None

    def _load_analysis(self) -> None:
        """Load the analysis of the task, unless one was handed over already.

        ``quickstart`` reads it before the database exists, to know which database the
        task points at, and passes it on rather than paying for it twice.
        """
        if not self.analysis_obj:
            with progress.spinner(f"Loading the analysis of task {self.args.task_id}"):
                analysis_obj = self.analysis_factory.get_analysis(
                    self.args.task_id,
                    task_url=self.config.ai_scaffold.task_url,
                    task_database=self.config.ai_scaffold.task_database,
                    load_excalidraw=not self.args.no_excalidraw,
                )

            if not analysis_obj:
                raise self.error(f"Could not find an analysis for task {self.args.task_id}")

            self.analysis_obj = analysis_obj

        self.depends_list = self.args.depends or list(self.analysis_obj.depends)
        self.is_importable = self.args.format == "xml" or self.analysis_obj.importable_module
        self.override_name = self.args.module or self.analysis_obj.existing_module_name
        self.target_version = OdooVersion(self.args.version) if self.args.version else self.analysis_obj.version

        if self.override_name:
            self.depends_list.append(self.override_name)

        self.depends_list = sorted(set(self.depends_list))
