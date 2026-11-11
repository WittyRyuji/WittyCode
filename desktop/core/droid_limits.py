"""Where Droid compacts a conversation with a model it does not know, and the window the page shows for it.

What Droid does (droid 0.251.0; probed without any model call: ``sandbox/probe_custom_window.py``, ``probe_limit_sticky.py``,
``probe_limit_sticky2.py``):

- A custom model (``custom:<name>-<n>``, the API providers) has no window Droid knows of. It reports 250 000 for both the window
  (``Session.context().limit``) and the compaction point (``context_budget``), whatever ``maxContextLimit`` says on the entry
  and whatever ``compactionTokenLimit`` says in settings.json, with or without ``baseModelId``. So a 1M model was shown as
  ``59K/250K`` and compacted at 250K.
- What moves both figures is the *session's* ``compaction_token_limit`` (``Session.update_settings``): set to 900 000 they both
  become 900 000. It cannot be given when the session is created.
- A built-in model keeps its real window and the user's own compaction point (``compactionTokenLimitPerModel``, else
  ``compactionTokenLimit``, else 250 000 or the window if that is smaller). But the value set on the session **stays on the
  session** when the model is changed, and then replaces the user's point for a built-in model that has no entry of its own
  (clamped to its window). So whenever the model changes after the session was given a value, the value is put back to what
  Droid would have used.

So for a custom model whose entry has a window, the session is given ``compaction_point(window, max_output)`` and the page shows
the window itself (``DroidLimits.window``); a custom model without a window stays at Droid's 250 000.
"""

from __future__ import annotations

from typing import Any

CUSTOM = "custom:"
"""The prefix of the id Droid gives a model of an API provider."""

DEFAULT_LIMIT = 250_000
"""Droid's compaction point when nothing says otherwise (it is clamped to the model's window by Droid itself)."""

SHARE = 0.85
"""Compact at this share of the window: the same default as the ChatGPT Plan's own conversations (``chatgpt_compact_at``)."""


def compaction_point(window: int, max_output: int | None = None) -> int:
    """Where to compact a conversation with a model of ``window`` tokens: at 85% of it, and never so late that the next answer
    (``max_output`` tokens, else a tenth of the window; at most a quarter of the window) does not fit after it."""
    reserve = min(max_output or window // 10, window // 4)
    return max(min(int(window * SHARE), window - reserve), 1)


class DroidLimits:
    """The windows and compaction points of the models Droid does not know, from the API providers' entries.

    ``providers`` is the ``ProviderStore`` (``limits_of``, ``factory_compaction``); without it every answer is ``None``, which
    leaves everything to Droid."""

    def __init__(self, providers: Any = None) -> None:
        self.providers = providers

    def window(self, model: str) -> int | None:
        """The window the user gave a custom model: what the page shows. ``None``: Droid's figure stands."""
        if self.providers is None or not model.startswith(CUSTOM):
            return None
        return self.providers.limits_of(model)[0]

    def limit(self, model: str) -> int | None:
        """The ``compaction_token_limit`` the session of this model should have. ``None``: nothing is wired, leave it to Droid."""
        if self.providers is None or not model:
            return None
        if model.startswith(CUSTOM):
            window, max_output = self.providers.limits_of(model)
            return compaction_point(window, max_output) if window else DEFAULT_LIMIT
        return self.providers.factory_compaction(model) or DEFAULT_LIMIT
