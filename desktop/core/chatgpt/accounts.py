"""Saved ChatGPT sign-ins: one credential file per issued client id, this machine's host id, the active account, and
access tokens renewed before they expire.

Layout under ``root`` (``<settings folder>\\chatgpt``)::

    host.json                  {"ext_agent_host_id": "urn:uuid:<uuid4>"}: made once, before the first sign-in, never changed
    accounts\\<client id>.json   one registration: identity, tokens, scopes (the record of OpenAI's documentation)
    accounts\\<client id>.lock   held by whoever renews that registration's tokens (any WittyCode process)
    state.json                 {"active": <client id>}

Two registrations can have the same email (two workspaces, or one account registered twice): the issued client id is
what tells them apart, so it names the file. It comes from a server, so a name that is not plainly safe (only letters,
digits, ``_`` and ``-``, and not a Windows device name) is replaced by a hash of it; the record inside names its client.

Every write is atomic (a temporary file in the same folder, then ``os.replace``): a crash leaves the old file or the new
one. A file that cannot be read or parsed is skipped (logged once, by name), never fatal. Tokens are never logged; client
ids are.

``TokenProvider`` hands out access tokens and renews them shortly before they expire. Refresh tokens rotate, so two
renewals with the same refresh token make the second fail (or make OpenAI end the session as a reuse): renewals of one
registration are serialized, within the process by an ``asyncio.Lock`` and across WittyCode processes by a byte-range
lock on its ``.lock`` file, and the record is read again from disk once the locks are held, so a renewal that another
task or process has just made is used instead of repeated. A renewal also runs to its end when the caller that started
it gives up: OpenAI has rotated the refresh token by then, and only the saved answer holds the new one.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desktop.core.chatgpt.errors import AuthError, ChatGPTError, PlanNotEnabled
from desktop.core.chatgpt.http import HttpClient
from desktop.core.chatgpt.oauth import PLAN_SCOPE, Endpoints, refresh, revoke

try:  # Windows; the other branch is for running the tests elsewhere
    import msvcrt

    def _try_lock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

except ImportError:  # pragma: no cover
    import fcntl

    def _try_lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


log = logging.getLogger("desktop.chatgpt")

REFRESH_MARGIN = 300.0
"""Seconds before expiry at which an access token is renewed."""
LOCK_WAIT = 45.0
"""How long a renewal waits for another process's renewal of the same registration (a token request times out in 30 s)."""

_PLAIN_NAME = re.compile(r"[A-Za-z0-9_-]{1,100}")
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))})
_ATTEMPTS = 20
"""Tries of a read, rename or delete that Windows refuses because another handle has the file open for a moment."""


@dataclass(frozen=True, slots=True)
class AccountRecord:
    """One registration of a ChatGPT account (the credential record of the documentation). ``saved_at`` is when its
    tokens were obtained, so ``expires_at`` is when the access token stops working."""

    email: str
    issuer: str
    subject: str
    client_id: str
    ext_agent_host_id: str
    id_token: str = field(repr=False)
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_type: str
    expires_in: float
    scopes: tuple[str, ...]
    saved_at: float

    @property
    def expires_at(self) -> float:
        return self.saved_at + self.expires_in

    @property
    def plan_enabled(self) -> bool:
        """The ChatGPT plan may pay for requests (``chatgpt.tokens.use.direct`` was granted)."""
        return PLAN_SCOPE in self.scopes

    @property
    def signed_in(self) -> bool:
        """There is a refresh token (signing out or a dead refresh token clears it)."""
        return bool(self.refresh_token)

    @property
    def label(self) -> str:
        """Email plus the end of the client id: two registrations of one email stay apart."""
        name = self.email or self.subject or "ChatGPT"
        tail = self.client_id[-6:]
        return f"{name} · {tail}" if tail else name

    def to_json(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "issuer": self.issuer,
            "subject": self.subject,
            "client_id": self.client_id,
            "ext_agent_host_id": self.ext_agent_host_id,
            "id_token": self.id_token,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": int(self.expires_in) if float(self.expires_in).is_integer() else self.expires_in,
            "scopes": list(self.scopes),
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> AccountRecord:
        """A record from ``to_json``'s shape; missing or mistyped values become empty, unknown keys are ignored."""
        return cls(
            email=_string(data.get("email")),
            issuer=_string(data.get("issuer")),
            subject=_string(data.get("subject")),
            client_id=_string(data.get("client_id")),
            ext_agent_host_id=_string(data.get("ext_agent_host_id")),
            id_token=_string(data.get("id_token")),
            access_token=_string(data.get("access_token")),
            refresh_token=_string(data.get("refresh_token")),
            token_type=_string(data.get("token_type")) or "Bearer",
            expires_in=_seconds(data.get("expires_in")),
            scopes=_scope_list(data.get("scopes")),
            saved_at=_timestamp(data.get("saved_at")),
        )


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _seconds(value: Any) -> float:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return 0.0


def _timestamp(value: Any) -> float:
    """Seconds since the epoch; also an ISO 8601 text (the documentation's example record has a text there)."""
    if isinstance(value, str):
        try:
            return _seconds(float(value))
        except ValueError:
            pass
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return 0.0
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return _seconds(moment.timestamp())
    return _seconds(value)


def _scope_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split()
    elif isinstance(value, list):
        items = [item for item in value if isinstance(item, str)]
    else:
        return ()
    return tuple(dict.fromkeys(item for item in items if item))


def _file_stem(client_id: str) -> str:
    if _PLAIN_NAME.fullmatch(client_id) and client_id.lower() not in _DEVICE_NAMES:
        return client_id
    # ("~" is not in the plain alphabet, so a hashed name never equals a plain one)
    return "~" + hashlib.sha256(client_id.encode("utf-8", "surrogatepass")).hexdigest()[:40]


def _retrying(action: Callable[[], Any]) -> Any:
    for attempt in range(_ATTEMPTS):
        try:
            return action()
        except PermissionError:  # (Windows: another handle has the file open at this moment)
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(0.025)
    return None


def _read_text(path: Path) -> str:
    return _retrying(lambda: path.read_text(encoding="utf-8"))


def _write_temporary(path: Path, value: Any) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return temporary


def _write_json(path: Path, value: Any) -> None:
    temporary = _write_temporary(path, value)
    try:
        _retrying(lambda: os.replace(temporary, path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class AccountStore:
    """The saved sign-ins under ``root`` (the layout is in the module docstring)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.folder = self.root / "accounts"
        self._host_id: str | None = None
        self._reported: set[tuple[str, float]] = set()

    # ------------------------------------------------------------------ this machine

    def host_id(self) -> str:
        """This machine's ``ext_agent_host_id``: made and saved by the first call, the same ever after."""
        if self._host_id is None:
            path = self.root / "host.json"
            self._host_id = self._read_host_id(path) or self._create_host_id(path)
        return self._host_id

    def _read_host_id(self, path: Path) -> str | None:
        try:
            text = _read_text(path)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(text)["ext_agent_host_id"]
            if not isinstance(value, str) or not value.startswith("urn:uuid:"):
                raise ValueError("not a urn:uuid")
            uuid.UUID(value.removeprefix("urn:uuid:"))
        except (ValueError, KeyError, TypeError) as error:
            self._report(path, error)
            return None
        return value

    def _create_host_id(self, path: Path) -> str:
        value = f"urn:uuid:{uuid.uuid4()}"
        temporary = _write_temporary(path, {"ext_agent_host_id": value})
        try:
            try:
                # A hard link creates host.json only where there is none, with all of its content at once: two
                # processes that both find no host id end up with the same one.
                os.link(temporary, path)
            except FileExistsError:
                existing = self._read_host_id(path)
                if existing:
                    return existing
                _retrying(lambda: os.replace(temporary, path))  # (the one there is unreadable)
            except OSError:  # (a file system without hard links)
                _retrying(lambda: os.replace(temporary, path))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        log.info("made this machine's ChatGPT host id")
        return value

    # ------------------------------------------------------------------ accounts

    def path_of(self, client_id: str) -> Path:
        """The credential file of ``client_id`` (always directly in the accounts folder)."""
        return self.folder / f"{_file_stem(client_id)}.json"

    def lock_path(self, client_id: str) -> Path:
        return self.folder / f"{_file_stem(client_id)}.lock"

    def accounts(self) -> list[AccountRecord]:
        """Every readable registration, the most recently saved first."""
        try:
            paths = sorted(self.folder.glob("*.json"))
        except OSError:
            return []
        records: list[AccountRecord] = []
        seen: set[str] = set()
        for path in paths:
            record = self._load(path)
            if record is None:
                continue
            if path.name != self.path_of(record.client_id).name or record.client_id in seen:
                self._report(path, ValueError("the file is not named after its client id"))
                continue
            seen.add(record.client_id)
            records.append(record)
        records.sort(key=lambda record: (-record.saved_at, record.client_id))
        return records

    def get(self, client_id: str | None) -> AccountRecord | None:
        if not client_id:
            return None
        record = self._load(self.path_of(client_id))
        return record if record is not None and record.client_id == client_id else None

    def save(self, record: AccountRecord) -> None:
        if not record.client_id:
            raise ValueError("an account record needs its issued client id")
        _write_json(self.path_of(record.client_id), record.to_json())
        log.debug("saved the ChatGPT account of client %s", record.client_id)

    def remove(self, client_id: str) -> bool:
        """Forget a registration entirely (signing out keeps it: ``clear_tokens``). False when there was none."""
        path = self.path_of(client_id)
        try:
            _retrying(path.unlink)
            removed = True
        except FileNotFoundError:
            removed = False
        with contextlib.suppress(OSError):
            self.lock_path(client_id).unlink()
        if self._read_state().get("active") == client_id:
            self.set_active(None)
        if removed:
            log.info("removed the ChatGPT account of client %s", client_id)
        return removed

    def clear_tokens(self, client_id: str, *, keep_id_token: bool = True) -> AccountRecord | None:
        """Blank the access and refresh tokens (``expires_in`` 0), keeping the registration: client id, email, subject,
        host id, scopes and (unless ``keep_id_token`` is False) the ID token, the ``id_token_hint`` of the next sign-in.
        Signing out passes False: the documentation says to drop the ID token then too."""
        record = self.get(client_id)
        if record is None:
            return None
        id_token = record.id_token if keep_id_token else ""
        cleared = replace(record, access_token="", refresh_token="", expires_in=0.0, id_token=id_token)
        self.save(cleared)
        log.info("cleared the ChatGPT tokens of client %s", client_id)
        return cleared

    def active_id(self) -> str | None:
        """The chosen account; when that one is gone, the most recently saved account that is signed in (else the most
        recently saved one)."""
        saved = self._read_state().get("active")
        if isinstance(saved, str) and self.get(saved) is not None:
            return saved
        records = self.accounts()
        chosen = [record for record in records if record.signed_in] or records
        return chosen[0].client_id if chosen else None

    def set_active(self, client_id: str | None) -> None:
        _write_json(self.root / "state.json", {"active": client_id})

    # ------------------------------------------------------------------ files

    def _load(self, path: Path) -> AccountRecord | None:
        try:
            data = json.loads(_read_text(path))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            record = AccountRecord.from_json(data)
            if not record.client_id:
                raise ValueError("no client id")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            self._report(path, error)
            return None
        return record

    def _read_state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        try:
            data = json.loads(_read_text(path))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            self._report(path, error)
            return {}
        return data if isinstance(data, dict) else {}

    def _report(self, path: Path, error: BaseException) -> None:
        """Log an unusable file once (per version of it); only its name and the kind of problem, never its content."""
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        if (path.name, stamp) in self._reported:
            return
        self._reported.add((path.name, stamp))
        log.warning("skipping the unusable ChatGPT file %s (%s)", path.name, type(error).__name__)


class TokenProvider:
    """Access tokens for requests: the stored one while it has more than ``margin`` seconds left, else a renewed one
    (saved before it is handed out). Meant for one event loop."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        endpoints: Endpoints,
        *,
        clock: Callable[[], float] = time.time,
        margin: float = REFRESH_MARGIN,
        lock_wait: float = LOCK_WAIT,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.store = store
        self.http = http
        self.endpoints = endpoints
        self.clock = clock
        self.margin = margin
        self.lock_wait = lock_wait
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._renewals: dict[str, asyncio.Future[str]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def access_token(self, client_id: str | None = None, *, rejected: str | None = None) -> str:
        """The access token of ``client_id`` (None: the active account). Raises ``PlanNotEnabled`` when the sign-in
        does not allow plan usage, ``AuthError(terminal=True)`` when the account is signed out or its refresh token is
        dead (the tokens are cleared then), and the error of a renewal that failed otherwise (the tokens are kept)."""
        key = client_id or self.store.active_id()
        if not key:
            raise AuthError("no ChatGPT account is signed in", terminal=True, code="signed_out")
        record = self._usable(key, self.store.get(key))
        if self._fresh(record) and record.access_token != rejected:
            return record.access_token
        self._bind_loop()
        renewal = self._renewals.get(key)
        if renewal is None or renewal.done():
            renewal = asyncio.ensure_future(self._renewal(key, rejected))
            renewal.add_done_callback(_retrieved)
            self._renewals[key] = renewal
        # Shielded: a caller that gives up (the user stops a turn) must not cut a renewal short, or the refresh token
        # that OpenAI has just rotated is never saved and the sign-in is lost.
        return await asyncio.shield(renewal)

    async def sign_out(self, client_id: str | None = None) -> bool:
        """``sign_out`` of ``client_id`` (None: the active account), not in the middle of a renewal of it."""
        key = client_id or self.store.active_id()
        if not key:
            return False
        async with self._serialized(key):
            return await sign_out(self.http, self.endpoints, self.store, key, sleep=self._sleep)

    async def _renewal(self, client_id: str, rejected: str | None = None) -> str:
        async with self._serialized(client_id):
            # (another task or process may have renewed it while this one waited)
            record = self._usable(client_id, self.store.get(client_id))
            if self._fresh(record) and record.access_token != rejected:
                return record.access_token
            return await self._renew(client_id, record)

    def _usable(self, client_id: str, record: AccountRecord | None) -> AccountRecord:
        if record is None or not record.signed_in:
            raise AuthError(f"the ChatGPT account of client {client_id} is signed out", terminal=True, code="signed_out")
        if not record.plan_enabled:
            raise PlanNotEnabled(
                f"the sign-in of client {client_id} does not allow using the ChatGPT plan ({PLAN_SCOPE} was not granted)",
                code="plan_not_enabled",
            )
        return record

    def _fresh(self, record: AccountRecord) -> bool:
        return bool(record.access_token) and record.expires_at - self.clock() >= self.margin

    async def _renew(self, client_id: str, record: AccountRecord) -> str:
        try:
            renewed = await refresh(self.http, self.endpoints, record, clock=self.clock)
        except AuthError as error:
            if error.terminal:
                # (only the tokens that failed: a sign-in may have stored new ones in the meantime)
                current = self.store.get(client_id)
                if current is not None and current.refresh_token == record.refresh_token:
                    try:
                        self.store.clear_tokens(client_id)
                    except OSError as failure:
                        log.warning("could not clear the dead ChatGPT tokens of client %s: %s", client_id, failure)
            log.warning("renewing the ChatGPT sign-in of client %s failed: %s", client_id, error)
            raise
        except ChatGPTError as error:
            log.warning("renewing the ChatGPT sign-in of client %s failed (the tokens are kept): %s", client_id, error)
            raise
        current = self.store.get(client_id)
        if current is None or current.refresh_token != record.refresh_token:
            # Replaced while the renewal ran (a new sign-in of this registration, or it was removed): what is stored
            # now wins, and the renewal is not written over it.
            if current is None or not current.signed_in:
                raise AuthError(f"the ChatGPT account of client {client_id} is signed out", terminal=True, code="signed_out")
            return self._usable(client_id, current if self._fresh(current) else renewed).access_token
        try:
            self.store.save(renewed)
        except OSError as error:
            raise ChatGPTError(f"没能保存更新后的 ChatGPT 登录：{error}", code="storage") from error
        log.info("renewed the ChatGPT tokens of client %s", client_id)
        return self._usable(client_id, renewed).access_token

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._locks, self._renewals = loop, {}, {}

    @contextlib.asynccontextmanager
    async def _serialized(self, client_id: str) -> AsyncIterator[None]:
        self._bind_loop()
        async with self._locks.setdefault(client_id, asyncio.Lock()):
            async with _process_lock(self.store.lock_path(client_id), self.lock_wait):
                yield


def _retrieved(task: asyncio.Future[Any]) -> None:
    """Mark a renewal's failure as seen: when every caller has given up, nobody else reads it."""
    if not task.cancelled():
        task.exception()


@contextlib.asynccontextmanager
async def _process_lock(path: Path, wait: float) -> AsyncIterator[None]:
    """Hold ``path``'s byte-range lock (the system drops it if the holder dies). A lock file that cannot be opened at
    all only costs the protection across processes, so it is logged and the work goes on."""
    handle = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError as error:
        log.warning("cannot use the lock file %s (%s); renewing without it", path.name, type(error).__name__)
    if handle is None:
        yield
        return
    with handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise ChatGPTError("另一个 WittyCode 窗口正在更新这个 ChatGPT 登录，稍后再试。", code="busy") from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                _unlock(handle)


async def sign_out(
    http: HttpClient,
    endpoints: Endpoints,
    store: AccountStore,
    client_id: str,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> bool:
    """Revoke the registration's refresh token (best effort), then clear its tokens here whatever happened, keeping the
    registration and the host id. True when OpenAI confirmed the revocation; False means the user should be told that
    it was not confirmed (they can disconnect the app in ChatGPT's settings)."""
    record = store.get(client_id)
    if record is None:
        return False
    confirmed = False
    try:
        confirmed = await revoke(http, endpoints, record, sleep=sleep)
    finally:
        store.clear_tokens(client_id, keep_id_token=False)
        outcome = "confirmed" if confirmed else "not confirmed"
        log.info("signed out of the ChatGPT account of client %s (revocation %s)", client_id, outcome)
    return confirmed
