"""Scaffold an Odoo module from a task, using an AI agent."""

from __future__ import annotations

from pathlib import Path

from odev.common.commands import DatabaseCommand
from odev.common.commands.base import Namespace
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin

from odev.plugins.odev_plugin_ai_scaffold.common.scaffold import Scaffold


logger = logging.getLogger(__name__)

MODULE_STRUCTURE = """
- <module_name>/
  ├── __manifest__.py          # name, version (e.g. 17.0.1.0.0), depends, data
  ├── __init__.py              # from . import models (and controllers if any)
  ├── models/
  │   ├── __init__.py         # from . import <each model file>
  │   └── <model>.py
  ├── views/
  │   └── <model>_views.xml
  └── security/
      └── ir.model.access.csv
"""


class ScaffoldCommand(DatabaseCommand, ListLocalDatabasesMixin, Scaffold):
    """Scaffold an Odoo module from a task, using an AI agent."""

    _name = "scaffold"
    _database_arg_required = False
    exclusive_arguments = [("database", "task_id", "prompt")]

    @property
    def _database_exists_required(self) -> bool:
        return False

    def __init__(self, args: Namespace, **kwargs):
        if args.database and not args.task_id and args.database not in self.list_databases():
            args.task_id = args.database
            args.database = None

        self.analysis_obj = kwargs.pop("analysis_obj", None)

        super().__init__(args, **kwargs)

    def run(self) -> None:
        """Execute the scaffold command."""
        self._load_analysis()

        logger.info(
            f"Scaffolding a {'importable' if self.is_importable else 'python'} module for Odoo {self.target_version}..."
        )

        version_available: dict[str, bool] = {}

        if self.target_version:
            version_available = self._prepare_odoo_environment([str(self.target_version)])

        agent = self.get_ai_agent()
        sandbox_dirs = self._get_sandbox_dirs(cwd=self.args.path)
        artifacts_dir = Path(sandbox_dirs[0]) if sandbox_dirs else self.args.path.resolve()

        prompt_str = self._build_prompt(version_available, artifacts_dir)

        database = self.args.database
        if database and not self._ensure_database_safety(database):
            database = None

        prompt_str += self._verification_prompt(database)

        logger.info(f"Delegating scaffolding to {self.args.cli}...")

        success = agent.run(
            prompt_str,
            sandbox_dirs,
            database=database,
            version=str(self.target_version) if self.target_version else None,
        )

        if not success:
            logger.error("Scaffolding failed or was interrupted.")

    def _build_prompt(self, version_available: dict[str, bool], artifacts_dir: Path) -> str:
        """Return the prompt describing what to build, and everything the task carries.

        The whole task goes in: its description, the images embedded in it and the
        diagrams it links to. What used to be handed over was the text following a
        "Technical analysis" heading, images stripped - which silently dropped the
        functional half of a task and every screenshot in it. The agent is told which
        part to weight instead of the parser deciding by truncation.
        """
        analysis = self.analysis_obj

        prompt_str = "You are an expert Odoo Developer. Your task is to scaffold a module.\n"
        prompt_str += f"\nHere is the task to implement:\n{analysis.description}\n"
        prompt_str += (
            "\nWhere the task carries a technical analysis, that is what to build from; "
            "the rest is the context it was written in and tells you why.\n"
        )
        prompt_str += f"\nThe module should be created in {self.args.path.resolve()}.\n"

        if image_paths := analysis.save_embedded_images(artifacts_dir):
            prompt_str += (
                f"\nThe task description embeds {len(image_paths)} image(s), exported for you to: "
                f"{', '.join(str(path) for path in image_paths)}. Read them: the description refers "
                "to each by that same file name, where the picture belonged.\n"
            )

        if diagram_paths := analysis.export_excalidraw_diagrams(artifacts_dir):
            prompt_str += (
                f"\n{len(diagram_paths)} Excalidraw architecture diagram(s) were exported for you to: "
                f"{', '.join(str(path) for path in diagram_paths)}. Read those images and make sure "
                "the scaffolded module matches the structure they show.\n"
            )

        if self.depends_list:
            prompt_str += f"Available dependencies for context are: {', '.join(self.depends_list)}.\n"

        if self.target_version:
            version_str = str(self.target_version)
            prompt_str += f"\nTarget Odoo version: {version_str}.\n"

            if version_available.get(version_str):
                prompt_str += (
                    f"Odoo reference source code is available at /worktrees/{version_str}/ "
                    "(subdirs: odoo/addons for Community, enterprise for Enterprise). "
                    "Use it to check correct model APIs, field types, and inheritance patterns.\n"
                )

        return prompt_str + f"\nRequired Module Structure:\n{MODULE_STRUCTURE}"

    def _verification_prompt(self, database: str | None) -> str:
        """Return the instructions telling the agent to check what it just wrote."""
        test_db = f"{database}_test" if database else "scaffold_test_db"
        module_name = self.override_name or self.args.path.name
        version_flag = f"-V {self.target_version}" if self.target_version else ""
        template_flag = f"-t {database}" if database else ""

        return (
            "\nAfter generating all module files, verify the module is correct:\n"
            "1. Check Python syntax and imports are consistent.\n"
            f"   IMPORTANT: The module MUST be created in a subfolder named `{module_name}` "
            "within the current directory (which is already in the addons-path).\n"
            "2. Test module installation with: "
            f"odev run {version_flag} {template_flag} {test_db} "
            f"-i {module_name} --stop-after-init --log-level=warn\n"
            "3. If errors occur, read the traceback, fix the offending file, and re-run.\n"
        )
