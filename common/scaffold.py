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
        console: Any

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
    comment = args.String(
        aliases=["--comment"],
        description="Instructions for this run, read before anything else: they take precedence over the "
        "task and its analysis wherever they disagree",
        default="",
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

        self._set_support_reason(self.args.task_id, "scaffolding")
        self.scaffold_database_name = self.args.database
        self.sandbox_repository = self._resolve_client_repository(self.analysis_obj)

    def _named_module(self, working_dir: Path) -> str | None:
        """Return the module the working directory *is*, or None when it holds modules.

        A manifest is what makes the difference, and the only reliable sign: a directory
        with one in it is a module, and the dev goes in it. A repository, an addons
        directory or the playground holds modules rather than being one - its name is
        not a module name, and being dash-separated, could not be one - so the name it
        happens to have says nothing about where the code goes. ``quickstart`` passes
        exactly such a path, the addons directory of the database it just set up.

        None is an answer, not a failure: which module a dev belongs in is a decision
        made from the modules already in the repository, and it is the agent standing in
        front of them that can make it. See ``ScaffoldCommand._placement_prompt``.
        """
        return working_dir.name if (working_dir / "__manifest__.py").is_file() else None

    def _resolve_version(self) -> OdooVersion:
        """Return the Odoo version to scaffold for, asking for it when nothing knows it.

        Every part of the run needs it: the manifest version of the module, the branch
        of the standard source it is written against, the server it is test-installed
        on. So it is asked for rather than defaulted - a module scaffolded against a
        guessed version is a module written for a framework the client does not run.

        Taken from ``-V``, then from the task. Both can parse to an empty version -
        ``OdooVersion("0")``, which is what a task whose subscription names no database
        resolves to, and which is falsy and printed as "0.0" - so the *value* is tested
        rather than whether one was given.
        """
        for candidate in (self.args.version, self.analysis_obj.version if self.analysis_obj else None):
            if not candidate:
                continue

            try:
                if version := OdooVersion(str(candidate)):
                    return version
            except Exception as e:  # noqa: BLE001 - anything unreadable is a version we do not have
                logger.warning(f"Ignoring the unreadable Odoo version {candidate!r}: {e}")

            logger.warning(f"The Odoo version {str(candidate)!r} says nothing about which version to build for.")

        answer = self.console.text("Odoo version to scaffold for (e.g. 19.0):")

        try:
            if version := OdooVersion(answer):
                return version
        except Exception as e:
            raise self.error(f"Could not read {answer!r} as an Odoo version: {e}") from e

        raise self.error("No Odoo version to scaffold for: pass one with -V, e.g. `-V 19.0`.")

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
        self.target_version = self._resolve_version()

        if self.override_name:
            self.depends_list.append(self.override_name)

        self.depends_list = sorted(set(self.depends_list))
