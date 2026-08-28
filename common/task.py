"""Read the task an analysis is made from, straight off the tracker that holds it.

This used to be somebody else's job: a Ps-Tools model exposed ``get_remote_analysis_data``
over RPC and odev asked it for the task, because Ps-Tools was where the odoo.com
credentials lived. Nothing about the work is Ps-Tools' though - it is ``project.task``
and the subscription behind it, on the tracker itself - so it is done here now, with the
credentials odev already keeps for that host, and the plugin no longer needs a proxy to
read a task.

What the tracker cannot tell in one query is resolved on top of the raw fields: who the
task is assigned to, which databases the customer's subscription carries, and from those
the Odoo version and the hosting platform. An analysis cannot be made without the last
two, and reading them here saves asking for what the tracker already knows.
"""

from __future__ import annotations

import re
from typing import Any

from odev.common import progress
from odev.common.databases import RemoteDatabase
from odev.common.logging import logging


logger = logging.getLogger(__name__)

TASK_MODEL = "project.task"

TASK_FIELDS = [
    "name",
    "description",
    "create_date",
    "x_has_sh",
    "mnt_subscription_id",
    "user_ids",
    "create_uid",
    "partner_id",
]
"""What is read off the task itself. Everything else is resolved from these."""

PLATFORMS = {"saas": "saas", "paas": "sh", "premise": "op"}
"""``openerp.enterprise.database.hosting`` -> the platform names the prompts use."""

# The hosting of an enterprise database is versioned as "17.0", but also as
# "saas~17.2" or "16.0-something": only the numeric head is a usable version.
DATABASE_VERSION = re.compile(r"\d+(?:\.\d+){1,3}")

# Images embedded in an HTML field point at the attachment they were uploaded as,
# in one of the several shapes the editor produces:
#   /web/image/1234                         /web/image/1234-a1b2c3/name.png
#   /web/image/ir.attachment/1234/datas     /web/image/1234?access_token=...
# Only the attachment id matters here; the rest is decoration.
WEB_IMAGE_SRC = re.compile(r"/web/image/(?:ir\.attachment/)?(\d+)")

MIMETYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
}


def _extension(mimetype: str) -> str:
    """Return the file extension an image of ``mimetype`` is saved under."""
    return MIMETYPE_EXTENSIONS.get(mimetype, f".{mimetype.removeprefix('image/')}")


class TaskReader:
    """Read a task and everything an analysis needs to know about it."""

    database: RemoteDatabase

    def __init__(self, url: str, database_name: str | None = None):
        self.database = RemoteDatabase(url, database_name or None)

    def read(self, task_id: str) -> dict[str, Any] | None:
        """Return the task ``task_id``, resolved, or None when there is no such task."""
        fields = self._readable_fields()

        with progress.spinner(f"Fetching task {task_id}"):
            tasks: list[dict[str, Any]] = self.database.models[TASK_MODEL].search_read(
                [("id", "=", int(task_id))],
                fields=fields,
                order="create_date DESC",
            )

        if not tasks:
            logger.error(f"No task found for ID {task_id} on {self.database.url}.")
            return None

        task = tasks[0]

        self._add_assignees(task)
        self._add_hosting(task)
        self._add_description_images(task)

        # Many2one fields are read as an (id, display_name) pair.
        partner = task.get("partner_id")
        task["client"] = partner[1] if partner else None
        task["attachments"] = self._attachments(task["id"])

        return task

    def _readable_fields(self) -> list[str]:
        """Return the :data:`TASK_FIELDS` that actually exist on the remote task.

        The tracker carries studio fields that a staging copy of it does not, and
        search_read raises on the whole query as soon as one field is unknown: losing
        the task over an optional field is worse than reading it without that field.
        """
        try:
            remote_fields = self.database.models[TASK_MODEL].fields_get(TASK_FIELDS)
        except Exception:  # noqa: BLE001
            logger.debug("Could not read the fields of the remote task, asking for all of them.", exc_info=True)
            return TASK_FIELDS

        fields = [field for field in TASK_FIELDS if field in remote_fields]

        if missing := set(TASK_FIELDS) - set(fields):
            logger.warning(f"Ignoring fields absent from the remote task: {', '.join(sorted(missing))}")

        return fields

    def _add_assignees(self, task: dict[str, Any]) -> None:
        """Resolve ``user_ids`` into an ``assignee_names`` list on the task.

        user_ids is a many2many, so search_read only hands back its ids: the names take
        a second query on res.users.
        """
        task["assignee_names"] = []
        assignee_ids = task.get("user_ids") or []

        if not assignee_ids:
            return

        try:
            users = self.database.models["res.users"].search_read(
                [("id", "in", sorted(assignee_ids))],
                fields=["name"],
                # Assignees may have been archived since the task was written.
                context={"active_test": False},
            )
        except Exception:  # noqa: BLE001 - unknown assignees must not cost us the task
            logger.debug(f"Could not read the assignees {sorted(assignee_ids)}.", exc_info=True)
            return

        names_by_id = {user["id"]: user["name"] for user in users}
        task["assignee_names"] = [names_by_id[user_id] for user_id in assignee_ids if user_id in names_by_id]

    def _add_hosting(self, task: dict[str, Any]) -> None:
        """Resolve ``databases``, ``odoo_version`` and ``platform`` on the task.

        A task carries no direct link to a database: what it has is a subscription,
        whose sale.order in turn carries database_ids. Read the first database that
        carries a version - a task pointing at several is pointing at one subscription,
        so they share a platform, and picking one beats reporting none.

        The databases are kept, url included: a caller that has to work on the
        customer's database needs to reach it, and the ids alone leave it with a second
        query.
        """
        task["odoo_version"] = None
        task["databases"] = []

        subscription = task.get("mnt_subscription_id")
        subscription_id = subscription[0] if subscription else None

        # A subscription means SaaS unless a database's own hosting says otherwise.
        # Without one, the task's own flag is all there is to go on.
        task["platform"] = "saas" if subscription_id else ("sh" if task.get("x_has_sh") else None)

        if not subscription_id:
            return

        try:
            subscriptions = self.database.models["sale.order"].read([subscription_id], fields=["database_ids"])
            database_ids = subscriptions[0]["database_ids"] if subscriptions else []
        except Exception:  # noqa: BLE001 - an unreadable subscription must not cost us the task
            logger.debug(f"Could not read the subscription {subscription_id}.", exc_info=True)
            return

        if not database_ids:
            return

        try:
            task["databases"] = self.database.models["openerp.enterprise.database"].read(
                sorted(database_ids),
                fields=["version", "hosting", "url"],
            )
        except Exception:  # noqa: BLE001 - an unreadable database must not cost us the task
            logger.debug(f"Could not read the databases {sorted(database_ids)}.", exc_info=True)
            return

        for database in task["databases"]:
            if platform := PLATFORMS.get(database["hosting"]):
                task["platform"] = platform

            if version := DATABASE_VERSION.search(database["version"] or ""):
                task["odoo_version"] = version.group()
                break

    def _attachments(self, task_id: int) -> list[dict[str, Any]]:
        """Return the files attached to the task, description images excluded.

        Attachments live in ir.attachment, so reading the task never includes them.
        ``res_field`` is False for a file added to the chatter, and set to the field
        name for an image embedded in one, which :meth:`_add_description_images` handles.
        """
        try:
            return self.database.models["ir.attachment"].search_read(
                [
                    ("res_model", "=", TASK_MODEL),
                    ("res_id", "=", task_id),
                    ("res_field", "=", False),
                ],
                fields=["name", "mimetype", "datas"],
            )
        except Exception:  # noqa: BLE001 - an unreadable attachment must not cost us the task
            logger.debug(f"Could not read the attachments of task {task_id}.", exc_info=True)
            return []

    def _add_description_images(self, task: dict[str, Any]) -> None:
        """Pull the images out of the description, leaving a name where each one was.

        The images of a description are attachments on the tracker, reachable neither
        by a relative url nor without credentials, so they have to be fetched. They are
        kept beside the description rather than inlined into it as data URIs: an agent
        cannot read a data URI as an image anyway, and a couple of screenshots in
        base64 are enough to blow past the 128kB the kernel allows the prompt, which
        reaches the agent CLI as a single command line argument.

        What the description keeps is the name of the file each image became, so the
        text still says where the picture belonged. :class:`~.analysis.Analysis` writes
        those files out next to the prompt.
        """
        task["description_images"] = []
        description = task.get("description")

        if not description:
            return

        attachment_ids = {int(match) for match in WEB_IMAGE_SRC.findall(description)}

        if not attachment_ids:
            return

        try:
            attachments = self.database.models["ir.attachment"].read(
                sorted(attachment_ids),
                fields=["mimetype", "datas"],
            )
        except Exception:  # noqa: BLE001 - a missing image must not cost us the description
            logger.debug(f"Could not read the images {sorted(attachment_ids)}.", exc_info=True)
            return

        images = {attachment["id"]: attachment for attachment in attachments if attachment.get("datas")}

        if not images:
            return

        # Numbered in the order they appear, and once per attachment: the same image
        # used twice in a description is one file referred to twice, not two files.
        names: dict[int, str] = {}

        def replace(match: re.Match[str]) -> str:
            attachment_id = int(match.group(1))

            # Leave an unknown id untouched rather than name a file that is not there.
            if attachment_id not in images:
                return match.group(0)

            if attachment_id not in names:
                attachment = images[attachment_id]
                mimetype = attachment.get("mimetype") or "image/png"
                names[attachment_id] = f"embedded-image-{len(names) + 1}{_extension(mimetype)}"
                task["description_images"].append(
                    {
                        "name": names[attachment_id],
                        "mimetype": mimetype,
                        "datas": attachment["datas"],
                    }
                )

            return names[attachment_id]

        # The regex matches the /web/image/<id> prefix only, so drop whatever trailed
        # it (/name.png, /datas, ?access_token=...) up to the quote.
        task["description"] = re.sub(rf"{WEB_IMAGE_SRC.pattern}[^\"'\s>]*", replace, description)

        if task["description_images"]:
            logger.info(f"Extracted {len(task['description_images'])} image(s) embedded in the description.")
