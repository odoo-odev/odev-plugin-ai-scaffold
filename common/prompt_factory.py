"""Assemble the prompt class a run uses, from the version and whatever extends it."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from odev.common.logging import logging
from odev.common.version import OdooVersion

from odev.plugins.odev_plugin_ai_scaffold.common.analysis import Analysis
from odev.plugins.odev_plugin_ai_scaffold.common.prompts.base import BasePrompt


logger = logging.getLogger(__name__)


class PromptFactory:
    """Build the prompt generator for a run."""

    extensions: list[type] = []
    """Mixins layered ahead of the prompt class, for plugins to add to.

    A plugin changing part of the prompt - most often what to do with the finished
    analysis - appends a mixin here rather than rewriting the class or patching a method
    onto it. They come first in the MRO, so a mixin overrides what it means to and
    reaches the rest through super(), and they compose with the version-specific prompt
    below instead of replacing it.
    """

    @classmethod
    def get_prompt_generator(cls, version: OdooVersion | None = None, odev=None) -> BasePrompt:
        """Return the prompt generator for ``version``, extensions included."""
        prompt_class = cls._version_prompt_class(version)

        if cls.extensions:
            prompt_class = type("Prompt", (*cls.extensions, prompt_class), {})

        return prompt_class(version, odev)

    @classmethod
    def _version_prompt_class(cls, version: OdooVersion | None = None) -> type[BasePrompt]:
        """Return the prompt class of ``version``, or the base one.

        A version whose prompt needs to differ gets a ``prompts/v<major>.py`` declaring
        a ``Prompt`` class; every other version falls back on :class:`BasePrompt`.
        """
        if version is None:
            return BasePrompt

        version_file = Path(__file__).parent / "prompts" / f"v{version.major}.py"

        if not version_file.exists():
            return BasePrompt

        try:
            spec = importlib.util.spec_from_file_location(
                f"odev.plugins.odev_plugin_ai_scaffold.common.prompts.v{version.major}",
                version_file,
            )

            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return getattr(module, "Prompt", BasePrompt)
        except Exception as e:  # noqa: BLE001 - a broken version prompt is not worth the run
            logger.warning(f"Could not load the prompt of version {version.major}: {e}")

        return BasePrompt

    @classmethod
    def build_analysis_prompt(  # noqa: PLR0913 - mirrors BasePrompt.build_prompt
        cls,
        analysis: Analysis,
        version: OdooVersion | None = None,
        platform: str | None = None,
        artifacts_dir: Path | None = None,
        source_path: Path | None = None,
        loc_per_hour_python: float | None = None,
        loc_per_hour_xml: float | None = None,
        loc_per_hour_js: float | None = None,
        minimum_dev_hours: float | None = None,
        comment: str | None = None,
        odev=None,
    ) -> str:
        """Build the agent prompt for a given task analysis."""
        return cls.get_prompt_generator(version, odev).build_prompt(
            analysis,
            artifacts_dir,
            platform,
            source_path,
            loc_per_hour_python,
            loc_per_hour_xml,
            loc_per_hour_js,
            minimum_dev_hours,
            comment,
        )
