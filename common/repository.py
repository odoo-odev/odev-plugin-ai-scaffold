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
