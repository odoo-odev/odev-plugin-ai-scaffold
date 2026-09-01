"""Arguments and analysis loading shared by ``scaffold`` and ``quickstart``."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from odev.common import args, progress
from odev.common.logging import logging
from odev.common.version import OdooVersion

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin
from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis, AnalysisFactory
from odev.plugins.odev_plugin_ai_scaffold.common.repository import ClientRepositoryMixin


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from argparse import Namespace

    from odev.common.errors import CommandError


class Scaffold(AICommandMixin, ClientRepositoryMixin):
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
    scaffold_database_name: str | None = None
    """The database the module is scaffolded for, when the task points at one."""

    def _resolve_scaffold_database(self) -> None:
        """Work out which database the module is written for, and where its code lives.

        A module belongs in the repository of the client it is written for. Finding
        that repository means reading the databases of the task's subscription, and
        asking a hosted database which branch it is built from - neither of which this
        plugin can do: the tracker is reached through Ps-Tools, and only
        ``odev-plugin-hosted`` knows a database's repository. Both are private, so a
        plugin that has them overrides this and sets :attr:`sandbox_repository`; here
        the version and the hosting are asked for instead, and the module is written
        wherever the command was called from.

        An explicit ``--path`` says the developer already knows where the code is, and
        settles it for every plugin: nothing is resolved, and the directory that was
        asked for is the one used.
        """
        if self.args.path.resolve() != Path.cwd().resolve():
            return

        self.scaffold_database_name = self.args.database
        self.sandbox_repository = self._resolve_client_repository(self.analysis_obj)

    def _default_module_name(self, working_dir: Path) -> str:
        """Return the name of the scaffolded module when nothing else names it.

        The working directory names it when it was made for one module, which is what
        an explicit ``--path`` is. A client repository holds many, so its name names
        none of them - and is not a valid module name to begin with, repositories being
        dash-separated. The task is then what identifies the module.
        """
        if self.sandbox_repository is not None and self.args.task_id:
            module_name = f"task_{self.args.task_id}"
            logger.info(f"No module name given: scaffolding into {module_name!r}.")
            return module_name

        return working_dir.name

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
