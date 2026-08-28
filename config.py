"""Configuration for the AI scaffold plugin, read from odev's config file."""

from __future__ import annotations

from odev.common.config import Section


class AiScaffoldSection(Section):
    """Settings for ``odev analyze`` and ``odev scaffold``, under ``[ai_scaffold]``."""

    _name = "ai_scaffold"

    @property
    def task_url(self) -> str:
        """URL of the Odoo instance the analysed tasks live on.

        The task is read over RPC with the credentials odev already keeps for that
        host, so pointing this elsewhere - a staging build, a local copy - is all it
        takes to work against another tracker. Defaults to odoo.com.
        """
        return self.get("task_url", "https://www.odoo.com")

    @task_url.setter
    def task_url(self, value: str):
        self.set("task_url", value)

    @property
    def task_database(self) -> str:
        """Database name behind :attr:`task_url`.

        Empty by default: a monodb host resolves its own name, and only a host serving
        several databases needs to be told which one.
        """
        return self.get("task_database", "")

    @task_database.setter
    def task_database(self, value: str):
        self.set("task_database", value)

    @property
    def loc_per_hour_python(self) -> float:
        """Python lines of code the agent should assume get written per hour of work.

        Most of an analysis carries a free ``estimated_time`` the agent fills in by
        hand, with nothing to ground it: this is what turns that guess into a line
        count divided by a throughput instead. Defaults to 20.
        """
        return float(self.get("loc_per_hour_python", "20"))

    @loc_per_hour_python.setter
    def loc_per_hour_python(self, value: float):
        self.set("loc_per_hour_python", str(value))

    @property
    def loc_per_hour_xml(self) -> float:
        """XML lines of code the agent should assume get written per hour of work.

        Defaults to 50.
        """
        return float(self.get("loc_per_hour_xml", "50"))

    @loc_per_hour_xml.setter
    def loc_per_hour_xml(self, value: float):
        self.set("loc_per_hour_xml", str(value))

    @property
    def loc_per_hour_js(self) -> float:
        """JavaScript lines of code the agent should assume get written per hour of work.

        Defaults to 20.
        """
        return float(self.get("loc_per_hour_js", "20"))

    @loc_per_hour_js.setter
    def loc_per_hour_js(self, value: float):
        self.set("loc_per_hour_js", str(value))

    @property
    def minimum_dev_hours(self) -> float:
        """Floor put on the total estimated development time of an analysis.

        Even a small change carries setup, testing and review overhead that a
        per-line estimate alone tends to undercut. Defaults to 4.
        """
        return float(self.get("minimum_dev_hours", "4"))

    @minimum_dev_hours.setter
    def minimum_dev_hours(self, value: float):
        self.set("minimum_dev_hours", str(value))
