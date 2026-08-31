"""Scaffold an Odoo module from a task, using an AI agent."""

from __future__ import annotations

import re
from pathlib import Path

from odev.common.commands import DatabaseCommand
from odev.common.commands.base import Namespace
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin

from odev.plugins.odev_plugin_ai_scaffold.common.scaffold import Scaffold


logger = logging.getLogger(__name__)

GUIDELINES_SKILL = "odoo_coding_guidelines"
"""Skill carrying how Odoo code is written, module layout included.

The prompt used to draw an ASCII tree of a module - manifest, models, views, security -
which is method rather than a fact of the run, is wrong for half the runs (a data-only
module has no ``models/``, a dev in an existing module adds no tree at all), and stops
at whichever four directories somebody happened to type. The skill has the whole
convention, per file type, and the command installs it before the agent starts.
"""

SAAS_SKILL = "odoo_saas_development"
"""Skill carrying what a data-only module can express, which is what SaaS deploys."""

BASE_SKILLS = ["odev", GUIDELINES_SKILL]
"""Skills every scaffolding run needs, whatever it builds."""


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
        self._resolve_scaffold_database()

        logger.info(
            f"Scaffolding a {'importable' if self.is_importable else 'python'} module for Odoo {self.target_version}..."
        )

        # Worktrees are checked out per branch, and saas versions have no branch of
        # their own here: a module for 17.2 is written against the source of 17.0.
        source_branch = "master" if self.target_version.master else f"{self.target_version.major}.0"
        version_available = self._prepare_odoo_environment([source_branch])

        agent = self.get_ai_agent()
        # Installed here and not by run_ai_agent: this command drives the agent itself,
        # to hand it the version the module is written for, and used to start it with
        # none of the skills its prompt sends it to.
        self._ensure_skills(agent, self._resolve_skills())

        sandbox_dirs = self._get_sandbox_dirs(self.scaffold_database_name, cwd=self.args.path)
        working_dir = Path(sandbox_dirs[0]) if sandbox_dirs else self.args.path.resolve()

        prompt_str = self._build_prompt(version_available, working_dir, source_branch)

        database = self.args.database
        if database and not self._ensure_database_safety(database):
            database = None

        prompt_str += self._verification_prompt(database, working_dir)

        logger.info(f"Delegating scaffolding to {self.args.cli}...")

        success = agent.run(
            prompt_str,
            sandbox_dirs,
            database=database,
            version=str(self.target_version),
        )

        if not success:
            logger.error("Scaffolding failed or was interrupted.")

    def _build_prompt(self, version_available: dict[str, bool], artifacts_dir: Path, source_branch: str) -> str:
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
        if image_paths := analysis.save_embedded_images(artifacts_dir):
            prompt_str += (
                f"\nThe task description embeds {len(image_paths)} image(s), exported for you to: "
                f"{', '.join(str(path) for path in image_paths)}. Read them: the description refers "
                "to each by that same file name, where the picture belonged.\n"
            )

        if diagram_paths := analysis.export_excalidraw_diagrams(artifacts_dir, self.odev):
            prompt_str += (
                f"\n{len(diagram_paths)} Excalidraw architecture diagram(s) were exported for you to: "
                f"{', '.join(str(path) for path in diagram_paths)}. Read those images and make sure "
                "the scaffolded module matches the structure they show.\n"
            )

        if self.depends_list:
            prompt_str += f"Available dependencies for context are: {', '.join(self.depends_list)}.\n"

        prompt_str += f"\nTarget Odoo version: {self.target_version}.\n"

        if version_available.get(source_branch):
            prompt_str += (
                f"Odoo reference source code is available at /worktrees/{source_branch}/ "
                "(subdirs: odoo/addons for Community, enterprise for Enterprise). "
                "Use it to check correct model APIs, field types, and inheritance patterns.\n"
            )

        return prompt_str + self._placement_prompt(artifacts_dir) + self._conventions_prompt()

    def _placement_prompt(self, working_dir: Path) -> str:
        """Return where the code goes: which module, and what it is called.

        A dev is not a module. Most of them belong in the custom module the client
        already has for that area - the models, the views and the manifest are there,
        and a second module beside it means two places to look and a dependency to
        declare between them for nothing. So the agent is told to read what is in the
        repository before deciding, and a new module is what it falls back on.

        When it does need one, the name is the convention rather than the agent's
        invention: ``<client>_<module it extends>``, which says at a glance whose
        customization it is and what it customizes.
        """
        if self.override_name:
            return (
                f"\nWrite this dev in the existing `{self.override_name}` module, in {working_dir}: "
                "extend the models, views and manifest that are already there rather than adding a "
                "module beside it.\n"
            )

        if named_module := self._named_module(working_dir):
            return f"\nWrite this dev in the `{named_module}` module, which is what {working_dir} is.\n"

        return (
            f"\nWhere this dev goes, in {working_dir}:\n"
            "1. Read the modules already there before writing anything. When one of them is the client's "
            "customization of the area this task is about, the dev belongs in it: extend its models and "
            "views, add to its manifest, bump its version. A new module beside it is a second place to "
            "look for the same customization, and a dependency between them for nothing.\n"
            "2. Only create a module when no existing one covers the requirement - a new functional area, "
            "or a repository with nothing in it yet.\n"
            "3. Name a new module `<client>_<main module it depends on>`: the client it is written for, "
            "or a short abbreviation of it, then the module it customizes. Lowercase `[a-z0-9_]` only, "
            f"and never the task number.{self._naming_example()}\n"
            "4. Say which of the two you did, and why, before you start writing.\n"
        )

    def _naming_example(self) -> str:
        """Return the naming convention spelled out for this client, when one is named."""
        client = (self.analysis_obj.client_name or "").strip() if self.analysis_obj else ""

        if not client:
            return ""

        # First word only, lowercased: the convention takes the client's name or an
        # abbreviation of it, and a legal name carries "SA", "GmbH", "& Sons".
        abbreviation = re.sub(r"[^a-z0-9]", "", client.split()[0].lower())

        if not abbreviation:
            return ""

        main_module = next((module for module in self.depends_list or [] if module != "base"), "<module>")
        return f" For {client}, that reads `{abbreviation}_{main_module}`."

    def _conventions_prompt(self) -> str:
        """Return the pointer to how Odoo code is written, rather than a copy of it.

        See :data:`GUIDELINES_SKILL`: the layout of a module, the naming of its files and
        the conventions of each language are the skill's, so the prompt says to work by
        it instead of restating a fraction of it that is wrong for half the runs.
        """
        skills = [GUIDELINES_SKILL, *([SAAS_SKILL] if self.is_importable else [])]

        prompt_str = f"\nWork by the {' and '.join(f'`{skill}`' for skill in skills)} skill"
        prompt_str += "s" if len(skills) > 1 else ""
        prompt_str += ": module layout, file naming and the conventions of each language are stated there.\n"

        if self.is_importable:
            prompt_str += (
                "This one has to be importable data rather than installable code: no Python, "
                "everything expressed as XML records.\n"
            )

        return prompt_str

    def _resolve_skills(self) -> list[str]:
        """Return the skills this run leans on, on top of the ones every run needs.

        Built per run rather than declared on the class: what a scaffold needs depends
        on what it builds, and a class attribute would carry the answer over to the next
        scaffold of the session.
        """
        # SaaS takes no custom code, so what is built for it is data to import - and
        # what a data-only module can express is what that skill states.
        return [*BASE_SKILLS, *([SAAS_SKILL] if self.is_importable else [])]

    def _verification_prompt(self, database: str | None, working_dir: Path) -> str:
        """Return the instructions telling the agent to check what it just wrote."""
        module_name = self.override_name or self._named_module(working_dir)
        # Assembled by join rather than by interpolating flags that may be empty: this
        # is a command line the agent copies, and a run without a template database
        # printed a double space in the middle of it.
        run_command = " ".join(
            [
                "odev run",
                f"-V {self.target_version}",
                *([f"-t {database}"] if database else []),
                f"{database}_test" if database else "scaffold_test_db",
                f"-i {module_name or '<the module you wrote in>'}",
                "--stop-after-init --log-level=warn",
            ]
        )
        # Only said when the module has still to be created: the directory it goes in is
        # the one in the addons-path, and a working directory that is itself a module
        # sits *under* that path rather than being it.
        location = (
            ""
            if module_name
            else f"   The module directory goes directly in {working_dir}, which is already in the addons-path.\n"
        )

        return (
            "\nWhen every file is written, check what you wrote:\n"
            "1. Check Python syntax and imports are consistent.\n"
            f"{location}"
            f"2. Install it on a throwaway database:\n   {run_command}\n"
            "3. If errors occur, read the traceback, fix the offending file, and re-run.\n"
        )
