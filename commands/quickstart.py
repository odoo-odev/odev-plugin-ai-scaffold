"""Quickstart a database and scaffold a module from a task into it."""

from __future__ import annotations

import re
from pathlib import Path

from odev.commands.database.quickstart import QuickStartCommand
from odev.common.commands.base import Namespace
from odev.common.logging import logging
from odev.common.mixins import ListLocalDatabasesMixin
from odev.common.odoobin import OdoobinProcess

from odev.plugins.odev_plugin_ai_scaffold.common.scaffold import Scaffold
from odev.plugins.odev_plugin_project.commands.pre_commit import COPIER_ANSWERS_FILE


logger = logging.getLogger(__name__)


class QuickStartScaffoldCommand(ListLocalDatabasesMixin, QuickStartCommand, Scaffold):
    """Quickstart a database and then scaffold a module into it with an AI agent.

    Extends the standard quickstart: given a task rather than a database, the database
    the task points at is the one set up, and the module is scaffolded into it.
    """

    _name = "quickstart"
    _database_arg_required = False

    @property
    def _database_exists_required(self) -> bool:
        return False

    def __init__(self, args: Namespace, **kwargs):
        if (
            args.database
            and not args.task_id
            and re.match(r"^#?\d+$", args.database)
            and args.database not in self.list_databases()
        ):
            args.task_id = args.database
            args.database = None

        # Read before anything else: which database to quickstart is what the task
        # says, so the analysis has to be in hand before the command is set up.
        if (
            args.task_id
            and not args.database
            and (
                analysis_obj := self.analysis_factory.get_analysis(
                    args.task_id,
                    task_url=self.config.ai_scaffold.task_url,
                    task_database=self.config.ai_scaffold.task_database,
                    load_excalidraw=not args.no_excalidraw,
                )
            )
        ):
            args.database = analysis_obj.database_url
            self.analysis_obj = analysis_obj

        super().__init__(args, **kwargs)

        if self.args.task_id:
            self._load_analysis()

    def run(self):
        """Execute the quickstart command followed by scaffolding."""
        super().run()

        if not self.args.task_id or not (scaffold_cls := self.odev.commands.get("scaffold")):
            return

        self.args.prompt = False

        if odoo_bin := OdoobinProcess(self._database):
            self.args.path = odoo_bin.additional_addons_paths[0]

        copier_answer_file = Path(odoo_bin.additional_addons_paths[0]) / COPIER_ANSWERS_FILE
        action = "update" if copier_answer_file.exists() else "install"

        if odoo_bin.check_addons_path(self.args.path) and self.console.confirm(
            f"Do you want to {action} pre-commit ?", default=True
        ):
            self.odev.run_command("pre-commit", self._database.name)

        # The analysis is handed over rather than read again: it cost a round trip to
        # the tracker, and the diagrams in it a browser.
        scaffold_cls(self.args, analysis_obj=self.analysis_obj).run()
