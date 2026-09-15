"""Analyze an Odoo task using an AI agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from odev.common.commands import DatabaseCommand
from odev.common.commands.base import Namespace
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin
from odev.common.odoobin import OdoobinProcess
from odev.common.version import OdooVersion

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin
from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis, AnalysisFactory
from odev.plugins.odev_plugin_ai_scaffold.common.analyze import Analyze
from odev.plugins.odev_plugin_ai_scaffold.common.prompt_factory import PromptFactory
from odev.plugins.odev_plugin_ai_scaffold.common.prompts.base import METHOD_SKILL, SAAS_SKILL
from odev.plugins.odev_plugin_ai_scaffold.common.repository import ClientRepositoryMixin


logger = logging.getLogger(__name__)

PLATFORM_CHOICES = [
    ("saas", "Odoo Online (SaaS)"),
    ("sh", "Odoo.sh"),
    ("op", "On-Premise"),
]
"""Hosting platforms an analysis can be written against."""

DATABASE_PLATFORMS = {"saas": "saas", "paas": "sh"}
"""odev's database platforms, mapped to the hosting they prove.

Only the hosted ones say anything: a local database is a copy of something, and a
plain remote one is reached by URL alone, neither of which tells where it runs.
"""


class AnalyzeCommand(DatabaseCommand, ListLocalDatabasesMixin, AICommandMixin, ClientRepositoryMixin, Analyze):
    """Analyze an Odoo task with an AI agent.

    The agent reads the task, the diagrams it links to and the standard source of the
    version the client runs, then writes its analysis in the conversation for the
    developer to read and argue with.
    """

    _name = "analyze"
    _database_arg_required = False

    analysis_factory: type[AnalysisFactory] = AnalysisFactory
    """Where analyses are read from. A plugin with another source swaps this out."""

    # Imported from the prompt rather than spelled out again: the prompt tells the
    # agent to work by these skills, and the two halves have to name the same ones.
    required_skills = [*AICommandMixin.required_skills, METHOD_SKILL]

    process: OdoobinProcess | None = None
    initiate_excalidraw: bool = True
    _database_hints: dict[str, Any] | None = None

    @property
    def _database_exists_required(self) -> bool:
        """Return True if a database has to exist for the command to work."""
        return False

    def __init__(self, args: Namespace, **kwargs):
        if args.no_excalidraw:
            self.initiate_excalidraw = False

        # -c is not what makes the client code be looked for - that happens on every
        # run - but the answer to the question the lookup would otherwise ask.
        self.assume_repository_clone = args.context

        # An analysis is about one task, so give it a playground of its own: the
        # diagrams and images written for it are named after their place in that task's
        # description, and two tasks sharing a directory overwrite each other's.
        self.sandbox_scope = str(args.task_id)

        super().__init__(args, **kwargs)

    def run(self) -> None:
        analysis = self._load_analysis()

        if analysis is None:
            return

        self._resolve_codebase(analysis)

        version = self._resolve_version(analysis)
        platform = self._resolve_platform(analysis)
        client = self._resolve_client(analysis)

        logger.info(
            f"Analyzing task {self.args.task_id} for {client or 'an unnamed client'} "
            f"on Odoo {version or 'any version'} "
            f"on {dict(PLATFORM_CHOICES).get(platform, 'an unknown platform')}."
        )

        source_path = self._resolve_odoo_source(version)
        self._resolve_skills(platform)

        compiled_request = PromptFactory.build_analysis_prompt(
            analysis,
            version=version,
            platform=platform,
            artifacts_dir=self._get_artifacts_dir(),
            source_path=source_path,
            loc_per_hour_python=self.config.ai_scaffold.loc_per_hour_python,
            loc_per_hour_xml=self.config.ai_scaffold.loc_per_hour_xml,
            loc_per_hour_js=self.config.ai_scaffold.loc_per_hour_js,
            minimum_dev_hours=self.config.ai_scaffold.minimum_dev_hours,
            saas_logic_hours=self.config.ai_scaffold.saas_logic_hours,
            comment=self.args.comment or None,
            odev=self.odev,
        )

        self.run_ai_agent(
            prompt=compiled_request,
            database=self.database_name,
            ephemeral_pg=True,
            mcp_servers=self._get_mcp_servers(),
            # Read-only: the worktree is shared with the rest of odev, and an analysis
            # has no business editing the standard source it is measured against.
            extra_ro_bind_dirs=[str(source_path)] if source_path else None,
        )

    def _resolve_skills(self, platform: str | None) -> None:
        """Add the skills this run leans on to the ones the command always needs.

        Bound on the instance and never on the class: what a run needs depends on its
        hosting, and appending to the class list would carry the answer over to the
        next analysis of the session.
        """
        skills = list(self.required_skills)

        if platform == "saas":
            # What is deployable there is a data-only module, which the method skill
            # sends the agent here to read the real shape and limits of.
            skills.append(SAAS_SKILL)

        self.required_skills = skills

    def _load_analysis(self) -> Analysis | None:
        """Return the analysis of the task, from wherever this odev knows to look.

        Its own seam, rather than a call inlined in :meth:`run`: a plugin holding
        written analyses of its own swaps :attr:`analysis_factory`, or overrides this,
        without having to reimplement the rest of the command.
        """
        return self.analysis_factory.get_analysis(
            self.args.task_id,
            task_url=self.config.ai_scaffold.task_url,
            task_database=self.config.ai_scaffold.task_database,
            load_excalidraw=self.initiate_excalidraw,
        )

    def _get_mcp_servers(self) -> dict[str, dict] | None:
        """Return the MCP servers handed to the agent, if any.

        None here: this plugin has nowhere to deliver an analysis to, so the agent
        writes it in the conversation and needs no tools to do it. A plugin that does
        have somewhere to deliver to overrides this and returns its server definition.
        """
        return None

    def _get_database_hints(self) -> dict[str, Any]:
        """Return what the analyzed database itself says about the client.

        A database beats a task at describing what the client runs: the task is what
        someone typed when opening it, the database is the thing. Read the version it
        runs, the company it belongs to, and the hosting its platform proves, so the
        three only fall through to the task, and then to a prompt, when there is no
        database to ask - which is the common case, the argument being optional.

        Read once and cached: a hosted database answers over RPC, and three resolvers
        ask for this.
        """
        if self._database_hints is not None:
            return self._database_hints

        self._database_hints = hints = {}
        database = getattr(self, "_database", None)

        if database is None or not getattr(database, "exists", False):
            return hints

        try:
            hints["version"] = database.version
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Could not read the version of {self.database_name}: {e}")

        hints["platform"] = DATABASE_PLATFORMS.get(getattr(database, "_platform", None))
        hints["client"] = self._read_company_name(database)

        if known := {key: value for key, value in hints.items() if value}:
            logger.info(f"Read from the database {self.database_name}: {known}.")

        return hints

    def _read_company_name(self, database) -> str | None:
        """Return the name of the main company of ``database``.

        The oldest company is the one the database was set up for, which is the client
        an analysis is written for. Read over SQL on a local database, and over RPC
        otherwise, where there is no cluster to query.
        """
        try:
            if getattr(database, "_platform", None) == "local":
                with database:
                    result = database.query("SELECT name FROM res_company ORDER BY id LIMIT 1")
                return result[0][0] if result else None

            # Real keyword arguments: odoolib forwards positionals as the method's own,
            # so a dict in second place would be read as the list of fields.
            companies = database.models["res.company"].search_read([], fields=["name"], limit=1, order="id")
            return companies[0]["name"] if companies else None
        except Exception as e:  # noqa: BLE001
            # A database that will not name its company is not a reason to stop: the
            # task may know, and the user certainly does.
            logger.debug(f"Could not read the company name of {self.database_name}: {e}")
            return None

    def _resolve_odoo_source(self, version: OdooVersion | None) -> Path | None:
        """Return the worktree holding the standard source for ``version``.

        An estimation turns on what Odoo already does: whether a requirement is a
        customization at all, and what it costs, cannot be told apart from the source
        of the version the client runs. The worktree is created and pulled when it is
        not there yet, which is a full clone the first time a version is analyzed.

        Returns None when no version was resolved, or when the worktree could not be
        prepared: the analysis is then made without the source rather than not at all.
        """
        if not version:
            logger.warning("No Odoo version resolved: analyzing without the standard source.")
            return None

        # Worktrees are checked out per branch, and saas versions have no branch of
        # their own here: 17.2 is analyzed against the source of 17.0.
        branch = f"{version.major}.0"
        available = self._prepare_odoo_environment([branch])

        if not available.get(branch):
            logger.warning(f"No worktree for Odoo {branch}: analyzing without the standard source.")
            return None

        return (self.odev.worktrees_path / branch).resolve()

    def _resolve_version(self, analysis: Analysis) -> OdooVersion | None:
        """Return the Odoo version to analyze for.

        An estimation depends on the version the client runs: what a requirement
        costs, and whether it is a customization at all, is not the same across
        versions. Take it from the command line, then from the database analyzed
        against, then from the task, and ask for it when none of them knows it.
        """
        candidates = [
            self.args.odoo_version,
            self._get_database_hints().get("version"),
            analysis.odoo_version,
        ]

        for candidate in candidates:
            if not candidate:
                continue
            if isinstance(candidate, OdooVersion):
                return candidate
            try:
                # An OdooVersion parsed out of nothing is falsy, and no version.
                if version := OdooVersion(str(candidate)):
                    return version
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Ignoring the unparsable Odoo version {candidate!r}: {e}")

        answer = self.console.text("Odoo version to analyze for (e.g. 17.0):")

        try:
            return OdooVersion(answer) or None
        except Exception as e:  # noqa: BLE001
            # Not worth failing the run over: the analysis is version-agnostic then.
            logger.warning(f"Could not read {answer!r} as an Odoo version, analyzing without one: {e}")
            return None

    def _resolve_platform(self, analysis: Analysis) -> str | None:
        """Return the hosting of the client database, asking for it when unknown.

        The platform caps what an analysis may propose - SaaS takes no custom code -
        so an analysis made without it is an analysis made against the wrong rules.

        Read off --hosting rather than --platform, which DatabaseCommand owns for
        something else entirely: where odev looks the analyzed database up.
        """
        platforms = dict(PLATFORM_CHOICES)

        for candidate in (self.args.hosting, self._get_database_hints().get("platform"), analysis.platform):
            if not candidate:
                continue
            if candidate in platforms:
                return candidate
            logger.warning(f"Ignoring the unknown platform {candidate!r}, expected one of {', '.join(platforms)}.")

        return self.console.select("Hosting of the client database:", choices=PLATFORM_CHOICES, default="saas")

    def _resolve_client(self, analysis: Analysis) -> str | None:
        """Return the name of the client, asking for it when the task does not say.

        The client is read off the customer of the task, which a task opened without
        one leaves empty. It tells the agent who the estimate is written for, so it is
        asked for here rather than left out. The company of the analyzed database comes
        first, being the client rather than a reference to them.
        """
        for candidate in (self.args.client, self._get_database_hints().get("client"), analysis.client_name):
            if candidate:
                analysis.client_name = candidate
                return candidate

        # An analysis is still worth running for a client that refuses to be named.
        analysis.client_name = self.console.text("Name of the client:") or None
        return analysis.client_name

    def _resolve_codebase(self, analysis: Analysis) -> None:
        """Point the sandbox at the client code the task concerns.

        Which code that is cannot be worked out here: it takes the databases of the
        task's subscription, read through Ps-Tools, and a hosted database willing to
        name the repository its branch is built from. Both are private plugins, and one
        of them overrides this to set :attr:`sandbox_repository`. Without it the
        analysis is made from the task and the standard source alone, which is what the
        version and the hosting are asked for.

        Tried on every run rather than behind a flag: an analysis of a task about a
        client that has custom code is an analysis of that code, and a developer who
        had to remember a flag to get it read the standard source instead. Nothing is
        cloned without asking - see :meth:`_confirm_repository_clone` - so a run that
        wants none of it costs one answer.
        """
        self._set_support_reason(self.args.task_id, "analysis")
        self.sandbox_repository = self._resolve_client_repository(analysis)

        if self.sandbox_repository is None:
            logger.warning("No client repository could be reached: analyzing task and standard source only.")

    def _get_artifacts_dir(self) -> Path | None:
        """Return the agent working directory, used to drop prompt artifacts.

        The images of the description and the exported diagrams are written here
        because it is bound into the sandbox: anywhere else and the agent is told
        about files it cannot open.

        Which directory that is depends on where the run works: a playground of the
        task's own, or the checkout when one was resolved - see :attr:`sandbox_scope`.
        """
        sandbox_dirs = self._get_sandbox_dirs(self.database_name)
        return Path(sandbox_dirs[0]) if sandbox_dirs else None
