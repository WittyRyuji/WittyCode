"""AWS for Bedrock, from the standard library only: signing a request (Signature Version 4) and finding the credentials to sign with.

What the user can configure is what Factory's Droid documents for a custom model's ``bedrock`` block, and it is read the same way:

- ``environment-api-key``: a Bedrock API key in ``AWS_BEARER_TOKEN_BEDROCK``, sent as ``Authorization: Bearer``; nothing is signed.
- ``credentials-command``: ``awsCredentialExport`` prints JSON with a top-level ``Credentials`` object (``AccessKeyId``,
  ``SecretAccessKey``, ``SessionToken``, optionally ``Expiration``); it runs when no valid credentials are at hand.
- ``aws-credentials``: the standard chain. With ``awsProfile``: that profile alone (``~/.aws/credentials``, then ``~/.aws/config``: IAM
  Identity Center / SSO, ``credential_process``). Without: ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``,
  then the profile of ``AWS_PROFILE`` (or ``default``). ``awsAuthRefresh`` (e.g. ``aws sso login``) runs when nothing was found, and
  the chain is tried once more.
- No ``authMethod``: ``AWS_BEARER_TOKEN_BEDROCK``, the chain, ``awsCredentialExport``, ``awsAuthRefresh``, in that order.

SSO: the access token the AWS CLI cached (``~/.aws/sso/cache/<sha1 of the session name or start URL>.json``) is exchanged for the
role's credentials at the SSO portal. An expired token is not renewed here: the CLI's refresh token is single use, and spending it
without writing the new one back would sign the user out of the CLI. The error says to run ``aws sso login``.

Not supported: a profile that assumes a role (``role_arn``): the message points at ``credentials-command``.

The region is ``awsRegion``, else ``AWS_REGION``, else ``AWS_DEFAULT_REGION``, else the ``region`` of the profile; never a default, so
a request does not go to a region the user did not choose.
"""

from __future__ import annotations

import configparser
import datetime as dt
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

log = logging.getLogger("desktop.aws")

ALGORITHM = "AWS4-HMAC-SHA256"
BEARER_VARIABLE = "AWS_BEARER_TOKEN_BEDROCK"
RENEW_BEFORE = 300.0
"""Credentials that expire sooner than this are not used (the request would arrive after they ran out)."""
KEEP_SECONDS = 600.0
"""How long credentials without an expiry (keys from the environment or a file) are remembered; reading them again is cheap."""


class AwsError(Exception):
    """Something the user can act on; the message is one Chinese sentence."""


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key: str
    secret_key: str = field(repr=False)
    token: str = field(default="", repr=False)
    expires: float | None = None
    """Seconds since the epoch; ``None``: does not expire."""

    def usable(self, now: float) -> bool:
        return self.expires is None or self.expires - now > RENEW_BEFORE


@dataclass(frozen=True, slots=True)
class Access:
    """How a request to Bedrock proves who sends it: a bearer token, or credentials to sign with."""

    bearer: str = field(default="", repr=False)
    credentials: Credentials | None = None


@dataclass(frozen=True, slots=True)
class BedrockSettings:
    """A model's ``bedrock`` block with its ``${VAR}`` references already filled in."""

    method: str = ""
    region: str = ""
    profile: str = ""
    base_url: str = ""
    auth_refresh: str = ""
    credential_export: str = ""
    metadata: tuple[tuple[str, str], ...] = ()


# ---------------------------------------------------------------------------------------------------- signing


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def _canonical_query(query: str) -> str:
    pairs = []
    for part in query.split("&") if query else ():
        name, _, value = part.partition("=")
        pairs.append((quote(_unquote(name), safe="-_.~"), quote(_unquote(value), safe="-_.~")))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _unquote(text: str) -> str:
    from urllib.parse import unquote

    return unquote(text)


def sign(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    credentials: Credentials,
    region: str,
    service: str,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """The headers to send: ``headers`` and what the signature needs (``Host`` is left to the HTTP client, which sends the same one).

    Everything in ``headers`` is signed, so give only what must not change on the way (content type, request metadata). The path is
    signed as it is sent, encoded once more (every service but S3 does it that way): a model id with a colon is ``%3A`` in the URL
    and ``%253A`` in the canonical request."""
    stamp = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    date = stamp.strftime("%Y%m%d")
    parts = urlsplit(url)
    signed: dict[str, str] = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    signed["host"] = parts.netloc
    signed["x-amz-date"] = amz_date
    if credentials.token:
        signed["x-amz-security-token"] = credentials.token
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_names = ";".join(names)
    payload = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), quote(parts.path or "/", safe="/~"), _canonical_query(parts.query), canonical_headers, signed_names, payload])
    scope = f"{date}/{region}/{service}/aws4_request"
    to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(credentials.secret_key, date, region, service), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    sent = {name: value for name, value in headers.items()}
    sent["X-Amz-Date"] = amz_date
    if credentials.token:
        sent["X-Amz-Security-Token"] = credentials.token
    sent["Authorization"] = f"{ALGORITHM} Credential={credentials.access_key}/{scope}, SignedHeaders={signed_names}, Signature={signature}"
    return sent


# ---------------------------------------------------------------------------------------------------- credentials


def _expiry(value: Any) -> float | None:
    """An ``Expiration`` as seconds since the epoch: ISO 8601 text, or milliseconds (the SSO portal)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            moment = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.timestamp()
    return None


def credentials_from_json(text: str, where: str) -> Credentials:
    """``{"Credentials": {...}}`` (what ``awsCredentialExport`` prints), or the same fields at the top (``credential_process``)."""
    try:
        data = json.loads(text)
    except ValueError:
        raise AwsError(f"{where} 输出的不是 JSON：它要打印带 Credentials 对象的 JSON（AccessKeyId、SecretAccessKey、SessionToken）。") from None
    inner = data.get("Credentials") if isinstance(data, dict) and isinstance(data.get("Credentials"), dict) else data
    if not isinstance(inner, dict) or not isinstance(inner.get("AccessKeyId"), str) or not isinstance(inner.get("SecretAccessKey"), str):
        raise AwsError(f"{where} 输出的 JSON 里没有 Credentials.AccessKeyId 和 SecretAccessKey。")
    token = inner.get("SessionToken")
    return Credentials(inner["AccessKeyId"], inner["SecretAccessKey"], token if isinstance(token, str) else "", _expiry(inner.get("Expiration")))


def _ini(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  (keys are case sensitive in these files)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return configparser.RawConfigParser()
    return parser


Fetch = Callable[[str, Mapping[str, str]], tuple[int, Any]]


def fetch_json(url: str, headers: Mapping[str, str]) -> tuple[int, Any]:
    """A blocking GET that answers ``(status, parsed JSON or None)``; a connection that cannot be made raises ``AwsError``."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        with error:
            status, raw = error.code, error.read()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise AwsError(f"连不上 AWS（{getattr(error, 'reason', error)}）。") from None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        return status, None


class Resolver:
    """Finds the access for a model's settings. Blocking (files, commands, one HTTP request): call it from a worker thread."""

    def __init__(
        self,
        lookup: Callable[[str], str | None],
        run: Callable[[str], str],
        refresh: Callable[[str], object] | None = None,
        *,
        home: Path | None = None,
        fetch: Fetch = fetch_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._lookup = lookup
        self._run = run
        self._refresh = refresh or run
        """Runs ``awsAuthRefresh`` (a login that waits for the user in a browser takes much longer than a command that prints keys)."""
        self._home = home
        self._fetch = fetch
        self._clock = clock
        self._cache: dict[tuple[str, ...], tuple[float, Credentials]] = {}

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else Path.home()

    # ------------------------------------------------------------------------------------------ region

    def region(self, settings: BedrockSettings) -> str:
        found = settings.region or self._lookup("AWS_REGION") or self._lookup("AWS_DEFAULT_REGION")
        if not found:
            profile = settings.profile or self._lookup("AWS_PROFILE") or "default"
            found = self._profile_section(profile).get("region", "")
        if not found:
            raise AwsError("没有指定 AWS 区域：在模型的 bedrock.awsRegion 里写，或设置环境变量 AWS_REGION，或在 ~/.aws/config 的 profile 里写 region。")
        return found

    # ------------------------------------------------------------------------------------------ the chain

    def access(self, settings: BedrockSettings) -> Access:
        method = settings.method
        if method == "environment-api-key":
            return Access(bearer=self._bearer(required=True))
        if method == "credentials-command":
            return Access(credentials=self._exported(settings))
        if not method:
            bearer = self._bearer(required=False)
            if bearer:
                return Access(bearer=bearer)
        found = self._chain(settings)
        if found is None and not method and settings.credential_export:
            found = self._exported(settings)
        if found is None and settings.auth_refresh:
            log.info("no AWS credentials found: running awsAuthRefresh")
            self._refresh(settings.auth_refresh)
            found = self._chain(settings)
        if found is None:
            raise AwsError(self._missing(settings))
        return Access(credentials=found)

    def _bearer(self, *, required: bool) -> str:
        token = self._lookup(BEARER_VARIABLE) or ""
        if required and not token:
            raise AwsError(f"环境变量 {BEARER_VARIABLE} 没有设置：把 Bedrock API Key 放进它（不需要重启 WittyCode）。")
        return token

    def _missing(self, settings: BedrockSettings) -> str:
        profile = settings.profile
        where = f"profile「{profile}」" if profile else "环境变量（AWS_ACCESS_KEY_ID …）和默认 profile"
        return f"找不到 AWS 凭据（查过 {where}）。设置凭据，或在 bedrock 里加 awsAuthRefresh（例如 aws sso login）/ awsCredentialExport，或改用 Bedrock API Key。"

    def _remember(self, key: tuple[str, ...], credentials: Credentials) -> Credentials:
        now = self._clock()
        until = credentials.expires - RENEW_BEFORE if credentials.expires is not None else now + KEEP_SECONDS
        self._cache[key] = (until, credentials)
        return credentials

    def _cached(self, key: tuple[str, ...]) -> Credentials | None:
        found = self._cache.get(key)
        if found is not None and found[0] > self._clock() and found[1].usable(self._clock()):
            return found[1]
        return None

    def _chain(self, settings: BedrockSettings) -> Credentials | None:
        profile = settings.profile
        if not profile:
            keys = self._environment_keys()
            if keys is not None:
                return keys
            profile = self._lookup("AWS_PROFILE") or "default"
        key = ("profile", profile)
        cached = self._cached(key)
        if cached is not None:
            return cached
        found = self._from_profile(profile, explicit=bool(settings.profile))
        return self._remember(key, found) if found is not None else None

    def _environment_keys(self) -> Credentials | None:
        access, secret = self._lookup("AWS_ACCESS_KEY_ID"), self._lookup("AWS_SECRET_ACCESS_KEY")
        if access and secret:
            return Credentials(access, secret, self._lookup("AWS_SESSION_TOKEN") or "")
        return None

    def _exported(self, settings: BedrockSettings) -> Credentials:
        if not settings.credential_export:
            raise AwsError("bedrock.authMethod 是 credentials-command，但没有 awsCredentialExport 命令。")
        key = ("export", settings.credential_export)
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            output = self._run(settings.credential_export)
        except Exception as error:
            raise AwsError(f"awsCredentialExport 命令没有成功：{error}") from None
        return self._remember(key, credentials_from_json(output, "awsCredentialExport"))

    # ------------------------------------------------------------------------------------------ profiles

    def _files(self) -> tuple[Path, Path]:
        folder = Path(self._lookup("AWS_SHARED_CREDENTIALS_FILE") or self.home / ".aws" / "credentials")
        config = Path(self._lookup("AWS_CONFIG_FILE") or self.home / ".aws" / "config")
        return folder, config

    def _profile_section(self, profile: str) -> dict[str, str]:
        """What ``~/.aws/config`` and ``~/.aws/credentials`` say about a profile (credentials win)."""
        credentials_file, config_file = self._files()
        merged: dict[str, str] = {}
        config = _ini(config_file)
        for section in ([profile] if profile == "default" else []) + [f"profile {profile}"]:
            if config.has_section(section):
                merged.update(dict(config.items(section)))
        shared = _ini(credentials_file)
        if shared.has_section(profile):
            merged.update(dict(shared.items(profile)))
        return merged

    def _from_profile(self, profile: str, *, explicit: bool) -> Credentials | None:
        values = self._profile_section(profile)
        if not values:
            if explicit:
                raise AwsError(f"~/.aws 里没有 profile「{profile}」。")
            return None
        if values.get("aws_access_key_id") and values.get("aws_secret_access_key"):
            return Credentials(values["aws_access_key_id"], values["aws_secret_access_key"], values.get("aws_session_token", ""))
        if values.get("credential_process"):
            try:
                output = self._run(values["credential_process"])
            except Exception as error:
                raise AwsError(f"profile「{profile}」的 credential_process 没有成功：{error}") from None
            return credentials_from_json(output, "credential_process")
        if values.get("sso_session") or values.get("sso_start_url"):
            return self._from_sso(profile, values)
        if values.get("role_arn"):
            raise AwsError(f"profile「{profile}」是靠 role_arn 换角色的，这里还不支持：用 aws configure export-credentials 之类的命令配 credentials-command。")
        return None

    def _sso_session(self, values: Mapping[str, str]) -> tuple[str, Mapping[str, str]]:
        """``(cache key text, the settings of the SSO session)``: a named ``[sso-session x]`` section, or the profile itself."""
        name = values.get("sso_session", "")
        if not name:
            return values.get("sso_start_url", ""), values
        _credentials_file, config_file = self._files()
        config = _ini(config_file)
        section = f"sso-session {name}"
        if not config.has_section(section):
            raise AwsError(f"~/.aws/config 里没有 [sso-session {name}]。")
        return name, dict(config.items(section))

    def _from_sso(self, profile: str, values: Mapping[str, str]) -> Credentials:
        cache_key, session = self._sso_session(values)
        region = session.get("sso_region") or values.get("sso_region", "")
        account, role = values.get("sso_account_id", ""), values.get("sso_role_name", "")
        if not (cache_key and region and account and role):
            raise AwsError(f"profile「{profile}」的 SSO 设置不完整（需要 sso_session 或 sso_start_url，以及 sso_region、sso_account_id、sso_role_name）。")
        login = f"aws sso login --profile {profile}"
        cache_file = self.home / ".aws" / "sso" / "cache" / (hashlib.sha1(cache_key.encode("utf-8")).hexdigest() + ".json")
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AwsError(f"还没有登录 AWS SSO：运行 {login}。") from None
        token = cached.get("accessToken") if isinstance(cached, dict) else None
        expires = _expiry(cached.get("expiresAt")) if isinstance(cached, dict) else None
        if not isinstance(token, str) or not token or (expires is not None and expires <= self._clock()):
            raise AwsError(f"AWS SSO 的登录过期了：运行 {login}。")
        url = f"https://portal.sso.{region}.amazonaws.com/federation/credentials?role_name={quote(role, safe='')}&account_id={quote(account, safe='')}"
        status, answer = self._fetch(url, {"x-amz-sso_bearer_token": token, "Accept": "application/json"})
        if status in (401, 403):
            raise AwsError(f"AWS SSO 不认这个登录了：运行 {login}。")
        role_credentials = answer.get("roleCredentials") if isinstance(answer, dict) else None
        if status != 200 or not isinstance(role_credentials, dict) or not role_credentials.get("accessKeyId"):
            raise AwsError(f"AWS SSO 没有给出角色凭据（HTTP {status}）：检查 profile「{profile}」的账号和角色。")
        return Credentials(
            str(role_credentials["accessKeyId"]),
            str(role_credentials.get("secretAccessKey") or ""),
            str(role_credentials.get("sessionToken") or ""),
            _expiry(role_credentials.get("expiration")),
        )
