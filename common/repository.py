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
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis


class ClientRepositoryMixin:
    """Ask for a checkout of the client's code, and get nothing without a plugin for it."""

    def _resolve_client_repository(self, analysis: Analysis | None) -> Path | None:
        """Return a checkout of the code the task is about, or None when there is none.

        Overridden by the plugin that can reach the tracker and the hosting. The
        default is not a failure: it is a run made from the task and the standard
        source, which is what the public plugin has.
        """
        return None
