"""Application settings, stored as JSON in ``%APPDATA%\\WittyCode\\settings.json``.

``WITTYCODE_SETTINGS`` overrides the file location (used by tests and the
live check so they never touch the real profile).

The application used to be called droid-desk and kept this file in ``%APPDATA%\\droid-desk``;
``migrate_legacy_settings`` copies it to the new place once and leaves the old one alone.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("desktop.settings")

SETTINGS_ENV = "WITTYCODE_SETTINGS"
APP_DIR = "WittyCode"
LEGACY_APP_DIR = "droid-desk"
SETTINGS_FILE = "settings.json"
SETTINGS_VERSION = 2
"""2: the ChatGPT Plan engine joined the shared memory (``MEMORY_ENGINES``); a file written before that, with every engine
then known switched on, gets it switched on too (``settings_from_dict``)."""
DEVIN_PERMISSIONS = ("ask", "plan", "code", "smart", "full")
"""The permission scale of Devin sessions: its modes (Ask / Plan / Code / Smart / Bypass), see ``core/devin/policy``."""
CHATGPT_PERMISSIONS = ("ask", "code", "full")
"""The permission scale of ChatGPT Plan sessions (the application's own harness): see ``core/harness/permissions``."""
MODES = ("auto", "spec")
THEMES = ("system", "light", "dark")
FONT_SCALES = ("sm", "md", "lg")
FOLLOW_UPS = ("queue", "steer")
"""What Enter does with a message typed while a turn runs: ``queue`` holds it until the turn is over, ``steer`` gives it
to the running turn now. Ctrl+Enter does the other one."""
SIDEBAR_WIDTH_RANGE = (220, 440)
MAX_OPEN_SESSIONS_RANGE = (1, 16)
DEFAULT_MAX_OPEN_SESSIONS = 4
DEFAULT_MAX_STEPS = 80
MAX_STEPS_RANGE = (1, 2**53 - 1)  # positive integers that survive the JSON/JavaScript bridge exactly
MAX_REMEMBERED_SESSIONS = 5000
"""Archived and renamed conversations kept in the settings file; a file with more is cut here."""
MAX_SESSION_TITLE = 120
COMPACT_AT_RANGE = (50, 95)
DEFAULT_COMPACT_AT = 85
"""Percent of a model's context window at which a ChatGPT Plan conversation is summarised (``harness/compact``)."""
MEMORY_ENGINES = ("devin", "chatgpt", "factory")


@dataclass
class AppSettings:
    factory_path: str = ""
    factory_enabled: bool = False
    factory_autonomy: str = "off"
    devin_path: str = ""
    """Explicit Devin CLI path; empty means auto-detect."""
    devin_enabled: bool = True
    """Show the Devin account's models in the model menu."""
    hidden_models: list[str] = field(default_factory=list)
    """Page ids of models (ChatGPT Plan, Devin or API) the user does not want in the conversation's model menu. A model in
    use is shown anyway; ids that no longer exist stay, so a model that comes back is still hidden."""
    memory_enabled: bool = True
    """The shared memory (``core/memory``): notes common to all engines, given to each conversation as an MCP server."""
    memory_recall: bool = True
    """Give each message the notes that bear on it (the application's own hook, ``core/hooks``); needs ``memory_enabled``."""
    memory_capture: bool = True
    """Write down what a message asks to be remembered in so many words ("记住 …", ``core/memory/capture``); needs ``memory_enabled``."""
    memory_curate: bool = True
    """After a turn, ask the engine for what is worth keeping and put it on the memory page as suggestions to confirm
    (``core/memory/curator``); costs one small request per turn; needs ``memory_enabled``."""
    memory_engines: list[str] = field(default_factory=lambda: list(MEMORY_ENGINES))
    """Which engines get the shared memory (a subset of ``MEMORY_ENGINES``)."""
    theme: str = "system"
    font_scale: str = "md"
    follow_up: str = "queue"
    """One of ``FOLLOW_UPS``: what Enter does while a turn is running."""
    sidebar_width: int = 288
    default_model: str = ""
    """Model id for new sessions; empty: the first model of the menu."""
    reasoning_effort: str = ""
    """Reasoning effort for new sessions; empty uses the model default."""
    devin_autonomy: str = "code"
    """Permission level for new Devin sessions (one of ``DEVIN_PERMISSIONS``)."""
    chatgpt_enabled: bool = True
    """Show the ChatGPT Plan models (the user's ChatGPT plan through the application's own harness) in the model menu."""
    chatgpt_autonomy: str = "code"
    """Permission level for new ChatGPT Plan sessions (one of ``CHATGPT_PERMISSIONS``)."""
    chatgpt_welcomed: bool = False
    """The user has seen the one-time "You're using your ChatGPT plan" note (OpenAI's UI guidelines ask for it once)."""
    chatgpt_compact: bool = True
    """Summarise the early part of a ChatGPT Plan conversation before the next request would not fit the model's window.
    Off: only when the server has refused a request as too big (a conversation must not get stuck)."""
    chatgpt_compact_at: int = DEFAULT_COMPACT_AT
    """The percent of the window at which that happens (``COMPACT_AT_RANGE``)."""
    max_steps: int = DEFAULT_MAX_STEPS
    """Model decision steps allowed in one turn of the own loop; read again between steps."""
    think_chinese: bool = True
    """Ask every model to write its thinking in Chinese (``core/language``)."""
    mode: str = "auto"
    projects: list[str] = field(default_factory=list)
    max_open_sessions: int = DEFAULT_MAX_OPEN_SESSIONS
    window_geometry: str = ""
    """Opaque base64 blob from ``QWidget.saveGeometry()``."""
    splitter_sizes: list[int] = field(default_factory=list)
    last_project: str = ""
    """The folder of the conversation started last: the first message box of a fresh window starts there."""
    archived_sessions: dict[str, int] = field(default_factory=dict)
    """Sidebar row ids of the conversations the user archived -> when, in ms. Archiving only hides: nothing of the engine's
    own files is touched, and the conversation can be brought back."""
    session_titles: dict[str, str] = field(default_factory=dict)
    """The names the user gave conversations here (row id -> name). The engines keep their own titles."""
    session_auto_titles: dict[str, str] = field(default_factory=dict)
    """LLM summaries of the first question; manual session_titles always take precedence."""
    session_links: dict[str, str] = field(default_factory=dict)
    """Earlier native row ids -> the current native segment of the same WittyCode conversation."""
    session_order: list[str] = field(default_factory=list)
    """Sidebar row ids in the order the user dragged them; newly discovered sessions precede these."""
    session_created: dict[str, float] = field(default_factory=dict)
    """Stable creation times, including old rows without one and conversations that changed engine."""

    def add_project(self, folder: str | Path) -> bool:
        normalized = normalize_folder(folder)
        if any(same_folder(normalized, existing) for existing in self.projects):
            return False
        self.projects.append(normalized)
        return True

    def remove_project(self, folder: str | Path) -> bool:
        before = len(self.projects)
        self.projects = [item for item in self.projects if not same_folder(item, folder)]
        return len(self.projects) != before


def normalize_folder(folder: str | Path) -> str:
    return str(Path(folder).expanduser().resolve(strict=False))


def same_folder(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(normalize_folder(a)) == os.path.normcase(normalize_folder(b))


def _roaming_folder(env: Mapping[str, str]) -> Path:
    appdata = env.get("APPDATA", "").strip()
    return Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"


def default_settings_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(SETTINGS_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _roaming_folder(env) / APP_DIR / SETTINGS_FILE


def legacy_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the application kept its settings before it was renamed."""
    env = os.environ if env is None else env
    return _roaming_folder(env) / LEGACY_APP_DIR / SETTINGS_FILE


def migrate_legacy_settings(env: Mapping[str, str] | None = None) -> Path | None:
    """Copy the pre-rename settings file to the new folder when that has none yet; return the new path if copied.

    Only a copy: the old file stays, so an older build keeps working and nothing can be lost. An explicit settings
    file (``--settings`` / ``WITTYCODE_SETTINGS``) is never filled from the old one.
    """
    env = os.environ if env is None else env
    if env.get(SETTINGS_ENV, "").strip():
        return None
    target = default_settings_path(env)
    legacy = legacy_settings_path(env)
    staging = target.with_name(target.name + ".migrating")
    try:
        if target.exists() or not legacy.is_file():
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        # Staged, so a copy that dies half way never leaves a truncated settings.json that would count as "present".
        shutil.copyfile(legacy, staging)
        if target.exists():  # another instance got there first
            staging.unlink(missing_ok=True)
            return None
        os.replace(staging, target)
    except OSError as error:
        log.warning("could not copy the settings from %s to %s: %s", legacy, target, error)
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return target


def settings_from_dict(raw: object) -> AppSettings:
    """Build settings from parsed JSON, falling back to defaults field by field."""
    defaults = AppSettings()
    if not isinstance(raw, Mapping):
        return defaults

    def text(name: str, allowed: tuple[str, ...] | None = None) -> str:
        value = raw.get(name)
        if isinstance(value, str) and (allowed is None or value in allowed):
            return value
        return getattr(defaults, name)

    projects: list[str] = []
    raw_projects = raw.get("projects")
    if isinstance(raw_projects, list):
        for item in raw_projects:
            if isinstance(item, str) and item.strip() and not any(same_folder(item, p) for p in projects):
                projects.append(item)

    max_open = raw.get("max_open_sessions")
    low, high = MAX_OPEN_SESSIONS_RANGE
    if not isinstance(max_open, int) or isinstance(max_open, bool) or not low <= max_open <= high:
        max_open = defaults.max_open_sessions

    sidebar = raw.get("sidebar_width")
    low_w, high_w = SIDEBAR_WIDTH_RANGE
    if not isinstance(sidebar, int) or isinstance(sidebar, bool) or not low_w <= sidebar <= high_w:
        sidebar = defaults.sidebar_width
    devin_enabled = raw.get("devin_enabled")
    if not isinstance(devin_enabled, bool):
        devin_enabled = defaults.devin_enabled

    hidden: list[str] = []
    raw_hidden = raw.get("hidden_models")
    if isinstance(raw_hidden, list):
        for item in raw_hidden:
            if isinstance(item, str) and item.strip() and item not in hidden:
                hidden.append(item)

    memory_enabled = raw.get("memory_enabled")
    if not isinstance(memory_enabled, bool):
        memory_enabled = defaults.memory_enabled
    memory_recall = raw.get("memory_recall")
    if not isinstance(memory_recall, bool):
        memory_recall = defaults.memory_recall
    memory_capture = raw.get("memory_capture")
    if not isinstance(memory_capture, bool):
        memory_capture = defaults.memory_capture
    memory_curate = raw.get("memory_curate")
    if not isinstance(memory_curate, bool):
        memory_curate = defaults.memory_curate
    memory_engines = raw.get("memory_engines")
    if isinstance(memory_engines, list):
        chosen = [name for name in MEMORY_ENGINES if name in memory_engines]
        version = raw.get("version")
        if (not isinstance(version, int) or version < 2) and "chatgpt" not in chosen and "devin" in chosen:
            chosen.append("chatgpt")  # (a file from before the own loop joined the memory, with every engine then known on)
        memory_engines = chosen
    else:
        memory_engines = list(defaults.memory_engines)
    chatgpt_enabled = raw.get("chatgpt_enabled")
    if not isinstance(chatgpt_enabled, bool):
        chatgpt_enabled = defaults.chatgpt_enabled
    chatgpt_welcomed = raw.get("chatgpt_welcomed")
    if not isinstance(chatgpt_welcomed, bool):
        chatgpt_welcomed = defaults.chatgpt_welcomed
    chatgpt_compact = raw.get("chatgpt_compact")
    if not isinstance(chatgpt_compact, bool):
        chatgpt_compact = defaults.chatgpt_compact
    compact_at = raw.get("chatgpt_compact_at")
    low_at, high_at = COMPACT_AT_RANGE
    if not isinstance(compact_at, int) or isinstance(compact_at, bool) or not low_at <= compact_at <= high_at:
        compact_at = defaults.chatgpt_compact_at
    max_steps = raw.get("max_steps")
    if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not MAX_STEPS_RANGE[0] <= max_steps <= MAX_STEPS_RANGE[1]:
        max_steps = defaults.max_steps
    think_chinese = raw.get("think_chinese")
    if not isinstance(think_chinese, bool):
        think_chinese = defaults.think_chinese

    splitter = raw.get("splitter_sizes")
    if not (
        isinstance(splitter, list)
        and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in splitter)
    ):
        splitter = []

    archived: dict[str, int] = {}
    raw_archived = raw.get("archived_sessions")
    if isinstance(raw_archived, dict):
        for key, stamp in raw_archived.items():
            if isinstance(key, str) and key and isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0 and len(archived) < MAX_REMEMBERED_SESSIONS:
                archived[key] = stamp
    titles: dict[str, str] = {}
    raw_titles = raw.get("session_titles")
    if isinstance(raw_titles, dict):
        for key, name in raw_titles.items():
            if isinstance(key, str) and key and isinstance(name, str) and name.strip() and len(titles) < MAX_REMEMBERED_SESSIONS:
                titles[key] = " ".join(name.split())[:MAX_SESSION_TITLE]

    auto_titles: dict[str, str] = {}
    if isinstance(raw.get("session_auto_titles"), dict):
        auto_titles = {k: " ".join(v.split())[:MAX_SESSION_TITLE] for k, v in raw["session_auto_titles"].items() if isinstance(k, str) and k and isinstance(v, str) and v.strip()}
        auto_titles = dict(list(auto_titles.items())[:MAX_REMEMBERED_SESSIONS])
    links: dict[str, str] = {}
    if isinstance(raw.get("session_links"), dict):
        links = {k: v for k, v in raw["session_links"].items() if isinstance(k, str) and isinstance(v, str) and k != v and k.startswith(("cg:", "dv:", "fy:")) and v.startswith(("cg:", "dv:", "fy:"))}
        links = dict(list(links.items())[:MAX_REMEMBERED_SESSIONS])
        for key in list(links):
            if key not in links:
                continue
            seen, current = {key}, links[key]
            while current in links and current not in seen:
                seen.add(current)
                current = links[current]
            if current in seen:
                for item in seen:
                    links.pop(item, None)

    order: list[str] = []
    raw_order = raw.get("session_order")
    if isinstance(raw_order, list):
        order = list(dict.fromkeys(item for item in raw_order if isinstance(item, str) and item))[:MAX_REMEMBERED_SESSIONS]
    created: dict[str, float] = {}
    raw_created = raw.get("session_created")
    if isinstance(raw_created, dict):
        for key, stamp in raw_created.items():
            if isinstance(key, str) and key and isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 < stamp < 253402300800 and len(created) < MAX_REMEMBERED_SESSIONS:
                created[key] = float(stamp)

    return AppSettings(
        factory_path=text("factory_path"),
        factory_enabled=raw.get("factory_enabled") if isinstance(raw.get("factory_enabled"), bool) else defaults.factory_enabled,
        factory_autonomy=text("factory_autonomy", ("off", "low", "medium", "high")),
        devin_path=text("devin_path"),
        devin_enabled=devin_enabled,
        hidden_models=hidden,
        memory_enabled=memory_enabled,
        memory_recall=memory_recall,
        memory_capture=memory_capture,
        memory_curate=memory_curate,
        memory_engines=memory_engines,
        theme=text("theme", THEMES),
        font_scale=text("font_scale", FONT_SCALES),
        follow_up=text("follow_up", FOLLOW_UPS),
        sidebar_width=sidebar,
        default_model=text("default_model"),
        reasoning_effort=text("reasoning_effort"),
        devin_autonomy=text("devin_autonomy", DEVIN_PERMISSIONS),
        chatgpt_enabled=chatgpt_enabled,
        chatgpt_autonomy=text("chatgpt_autonomy", CHATGPT_PERMISSIONS),
        chatgpt_welcomed=chatgpt_welcomed,
        chatgpt_compact=chatgpt_compact,
        chatgpt_compact_at=compact_at,
        max_steps=max_steps,
        think_chinese=think_chinese,
        mode=text("mode", MODES),
        projects=projects,
        max_open_sessions=max_open,
        window_geometry=text("window_geometry"),
        splitter_sizes=list(splitter),
        last_project=text("last_project"),
        archived_sessions=archived,
        session_titles=titles,
        session_auto_titles=auto_titles,
        session_links=links,
        session_order=order,
        session_created=created,
    )


def settings_to_dict(settings: AppSettings) -> dict[str, Any]:
    return {"version": SETTINGS_VERSION, **asdict(settings)}


def load_settings(path: Path | None = None) -> AppSettings:
    target = default_settings_path() if path is None else path
    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return AppSettings()
    try:
        raw = json.loads(content)
    except ValueError:
        _set_aside(target)
        return AppSettings()
    return settings_from_dict(raw)


def _set_aside(path: Path) -> None:
    # Keep an unreadable file instead of letting the next save overwrite it.
    try:
        os.replace(path, path.with_name(path.name + ".bad"))
    except OSError:
        pass


def save_settings(settings: AppSettings, path: Path | None = None) -> Path:
    target = default_settings_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings_to_dict(settings), ensure_ascii=False, indent=2) + "\n"
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temporary, target)
    return target