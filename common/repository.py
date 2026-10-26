"""The seam between a task and a checkout of the client's code.

An agent asked about a client should work in that client's repository, and nothing
here can find it: it takes the databases of the task's subscription, which are read
through Ps-Tools, and a hosted database willing to name the repository its branch is
built from, which only ``odev-plugin-hosted`` knows how to ask. Both are private.

So this is where the question is asked, and a private plugin answers it. Unanswered,
the commands fall back on what they can be told instead - the version and the hosting,
which they ask for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from git import Repo

from odev.common.odoobin import OdoobinProcess
from odev.common.version import OdooVersion


if TYPE_CHECKING:
    from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis


class ClientRepositoryMixin:
    """Ask for a checkout of the client's code, and get nothing without a plugin for it."""

    if TYPE_CHECKING:
        console: Any
        odev: Any

    assume_repository_clone: bool = False
    """Clone the client repository without asking first.

    Set by a command told outright to work in the client code, which has then already
    been asked - on the command line, which is where the answer came from.
    """

    # The Python a module has by extension but not by logic: the manifest and the
    # package files. A module whose only Python is these carries nothing but data.
    _NON_CODE_PY = frozenset({"__manifest__.py", "__openerp__.py", "__init__.py"})

    def _version_from_cwd(self) -> OdooVersion | None:
        """Return the Odoo version of the checkout the developer is standing in.

        A developer working in a client's checkout builds for the version it is on,
        which the task's subscription in Ps-Tools can name wrongly - so the folder is
        asked first. The branch is the surest word on it: a client repository checked
        out on ``17.0`` or ``saas-17.2`` says the version in its branch name. When the
        branch is a working name rather than a version - ``client-dev``, a detached
        HEAD, no repository at all - the module manifests answer instead, carrying the
        version in their own ``version`` key.

        None when neither knows: the version then falls through to the task, and to a
        prompt.
        """
        cwd = Path.cwd()

        if branch := self._cwd_branch(cwd):
            try:
                if version := OdooVersion(branch):
                    return version
            except Exception:  # noqa: BLE001 - a branch that is not a version is simply not one
                pass

        if (cwd / "__manifest__.py").is_file():
            return OdoobinProcess.version_from_manifest(cwd)

        return OdoobinProcess.version_from_addons(cwd)

    def _platform_from_cwd(self) -> str | None:
        """Return the hosting the checkout the developer is standing in proves, if any.

        Only SaaS leaves a mark in the code: it takes data-only modules, so a checkout
        whose every module is data - no Python beyond the manifest - is a SaaS client's,
        and nothing else is. Custom Python rules SaaS out but cannot tell Odoo.sh from
        on-premise apart: None then, and the hosting is read off the task or asked for.
        """
        modules = self._modules_in_cwd(Path.cwd())

        if modules and all(self._is_data_only(module) for module in modules):
            return "saas"

        return None

    def _cwd_branch(self, path: Path) -> str | None:
        """Return the branch name of the git repository ``path`` sits in, or None.

        None covers everything that leaves no branch to read: no repository above the
        directory, and a detached HEAD, which ``active_branch`` raises over.
        """
        try:
            return Repo(path, search_parent_directories=True).active_branch.name
        except Exception:  # noqa: BLE001 - no repository, or a detached HEAD with no branch
            return None

    def _modules_in_cwd(self, path: Path) -> list[Path]:
        """Return the modules the working directory is, or the ones it holds.

        A manifest is what marks a module: the directory is one when it has a manifest
        of its own, and otherwise holds its modules as the children that do - which is
        how an addons directory or a repository root looks from here.
        """
        if (path / "__manifest__.py").is_file():
            return [path]

        try:
            return [child for child in path.iterdir() if (child / "__manifest__.py").is_file()]
        except OSError:
            return []

    def _is_data_only(self, module: Path) -> bool:
        """Return whether ``module`` carries no Python logic, only data to import.

        Everything Python in the module is weighed but the package files: a model, a
        wizard, a controller is logic and makes the module code, while the manifest and
        an ``__init__`` that only wires submodules together are not - and a submodule
        that holds logic is itself a ``.py`` this still finds.
        """
        return not any(py.name not in self._NON_CODE_PY for py in module.rglob("*.py"))

    def _resolve_client_repository(self, analysis: Analysis | None) -> Path | None:
        """Return a checkout of the code the task is about, or None when there is none.

        Overridden by the plugin that can reach the tracker and the hosting. The
        default is not a failure: it is a run made from the task and the standard
        source, which is what the public plugin has.
        """
        return None

    def _set_support_reason(self, task_id: str | None, purpose: str) -> None:
        """Tell odev what the support logins of this run are for.

        Reaching a client's database logs into the support backend of its hosting,
        which asks what the login is for - and asks it with a blank prompt, though the
        run knows the answer: the task it was called about, and what it is doing with
        it. Both are said, a task number alone leaving whoever reads the log to guess
        why the database was opened. Recorded on the framework rather than handed
        down, the login happening several layers below this, in the connector of a
        database the command never sees.
        """
        if task_id:
            self.odev.support_reason = f"{purpose} #{task_id}"

    def _confirm_repository_clone(self, full_name: str, path: Path) -> bool:
        """Return whether the client repository may be cloned to work in it.

        The clone is the one step of the lookup the developer pays for: a client
        repository is large, it lands on their disk for good, and the run happens
        either way - from the task and the standard source, only without the code.
        So it is asked for rather than taken, and only ever once, when there is a
        repository to clone: everything before it is a question put to Ps-Tools and
        the hosting, and a repository already checked out costs nothing to work in.
        """
        if self.assume_repository_clone:
            return True

        return self.console.confirm(f"Clone {full_name} into {path} and work in it?", default=True)
