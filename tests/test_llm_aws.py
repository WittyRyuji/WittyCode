"""AWS for Bedrock (``core/llm/aws``): the signature of a request, and where the credentials come from."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from desktop.core.llm import aws
from desktop.core.llm.aws import Access, AwsError, BedrockSettings, Credentials, Resolver

SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
NOW = dt.datetime(2026, 11, 11, 12, 30, 45, tzinfo=dt.timezone.utc)
EPOCH = NOW.timestamp()
HOST = "https://bedrock-runtime.us-east-1.amazonaws.com"


def signature_of(headers: dict[str, str]) -> str:
    return headers["Authorization"].split("Signature=")[1]


# ---------------------------------------------------------------------------------------------- signing


def test_the_signing_key_is_the_one_aws_documents():
    assert aws.signing_key(SECRET, "20120215", "us-east-1", "iam").hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_the_get_vanilla_example_of_the_signature_test_suite_comes_out_right():
    credentials = Credentials("AKIDEXAMPLE", SECRET)
    sent = aws.sign("GET", "https://example.amazonaws.com/", {}, b"", credentials, "us-east-1", "service", dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc))
    assert sent["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )
    assert sent["X-Amz-Date"] == "20150830T123600Z"


@pytest.mark.parametrize(
    ("url", "body", "headers", "token", "expected"),
    [
        (  # a model id with a colon: %3A in the URL, %253A in the canonical request; a body that is not ASCII
            f"{HOST}/model/anthropic.claude-sonnet-4-5-20250929-v1%3A0/converse-stream",
            '{"messages":[{"role":"user","content":[{"text":"hi \u4f60\u597d"}]}]}'.encode(),
            {"Content-Type": "application/json"},
            "",
            "12146a625838a258a66649ffab94fdd595e13755be1b036791d0b0225ee52553",
        ),
        (  # an ARN (colons and a slash), a session token, a header value with a double space
            f"{HOST}/model/arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Ainference-profile%2Fus.anthropic.claude-3-7/converse-stream",
            b"{}",
            {"Content-Type": "application/json", "X-Amzn-Bedrock-Request-Metadata": '{"team":"a  b"}'},
            "SESSIONTOKEN/abc+def==",
            "f881ef0378c46be15ef694864787289161d06ca588a18f210a9238794962270e",
        ),
        (f"{HOST}/model/x/invoke-with-response-stream", b"", {"Content-Type": "application/json"}, "tok", "ce09f16e764ad7842886e678f850f4759d1b386dd09f9e5a352faa070403182f"),
        (  # a server on a port (a VPC endpoint, a proxy): the port is part of the signed host
            "http://127.0.0.1:8080/model/x%3Ay/converse-stream",
            b"{}",
            {"Content-Type": "application/json"},
            "",
            "c43471509e87ed4ada62ef076eae0bcd0184a5d6425774e8c419d29d81416d14",
        ),
    ],
)
def test_a_bedrock_request_is_signed_as_botocore_signs_it(url, body, headers, token, expected):
    """The expected values were made by botocore's ``SigV4Auth`` for the same request, key and time."""
    sent = aws.sign("POST", url, headers, body, Credentials("AKIDEXAMPLE", SECRET, token), "us-east-1", "bedrock", NOW)
    assert signature_of(sent) == expected
    assert sent["X-Amz-Date"] == "20261111T123045Z" and ("X-Amz-Security-Token" in sent) == bool(token)
    assert "x-amz-security-token" in sent["Authorization"] or not token


def test_the_method_is_signed_in_capitals_whatever_the_caller_wrote():
    credentials = Credentials("AKIDEXAMPLE", SECRET)
    big = aws.sign("POST", f"{HOST}/model/x/converse-stream", {}, b"{}", credentials, "us-east-1", "bedrock", NOW)
    small = aws.sign("post", f"{HOST}/model/x/converse-stream", {}, b"{}", credentials, "us-east-1", "bedrock", NOW)
    assert signature_of(big) == signature_of(small)


def test_the_query_is_sorted_and_encoded_in_the_signature():
    credentials = Credentials("AKIDEXAMPLE", SECRET)
    one = aws.sign("GET", "https://iam.amazonaws.com/?Version=2010-05-08&Action=ListUsers", {}, b"", credentials, "us-east-1", "iam", NOW)
    two = aws.sign("GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08", {}, b"", credentials, "us-east-1", "iam", NOW)
    assert signature_of(one) == signature_of(two)
    other = aws.sign("GET", "https://iam.amazonaws.com/?Action=ListRoles&Version=2010-05-08", {}, b"", credentials, "us-east-1", "iam", NOW)
    assert signature_of(other) != signature_of(one)


def test_the_headers_given_are_kept_and_the_secret_never_shows():
    credentials = Credentials("AKIDEXAMPLE", SECRET, "TOKEN")
    sent = aws.sign("POST", f"{HOST}/model/x/converse-stream", {"Content-Type": "application/json"}, b"{}", credentials, "us-east-1", "bedrock", NOW)
    assert sent["Content-Type"] == "application/json" and sent["X-Amz-Security-Token"] == "TOKEN"
    assert SECRET not in repr(credentials) and "TOKEN" not in repr(credentials)


# ---------------------------------------------------------------------------------------------- credentials in JSON


def test_credentials_are_read_from_the_credentials_object_or_from_the_top_level():
    nested = json.dumps({"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST", "Expiration": "2026-11-11T13:00:00Z"}})
    found = aws.credentials_from_json(nested, "x")
    assert (found.access_key, found.secret_key, found.token) == ("AK", "SK", "ST")
    assert found.expires == dt.datetime(2026, 11, 11, 13, 0, tzinfo=dt.timezone.utc).timestamp()
    flat = aws.credentials_from_json(json.dumps({"Version": 1, "AccessKeyId": "AK", "SecretAccessKey": "SK"}), "x")
    assert (flat.token, flat.expires) == ("", None)
    for bad in ("not json", "[]", json.dumps({"Credentials": {"AccessKeyId": "AK"}})):
        with pytest.raises(AwsError):
            aws.credentials_from_json(bad, "awsCredentialExport")


def test_an_expiry_may_be_text_seconds_or_milliseconds():
    assert aws._expiry("2026-11-11T13:00:00+00:00") == aws._expiry("2026-11-11T13:00:00Z")
    assert aws._expiry(1_700_000_000_000) == 1_700_000_000.0 and aws._expiry(1_700_000_000) == 1_700_000_000.0
    assert aws._expiry("tomorrow") is None and aws._expiry(None) is None and aws._expiry(True) is None


def test_credentials_that_are_about_to_expire_are_not_used():
    assert Credentials("a", "b").usable(EPOCH)
    assert Credentials("a", "b", "", EPOCH + 3600).usable(EPOCH)
    assert not Credentials("a", "b", "", EPOCH + 60).usable(EPOCH)


# ---------------------------------------------------------------------------------------------- the chain


class Setup:
    """A made-up environment, home folder and command runner for a ``Resolver``."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        (self.home / ".aws" / "sso" / "cache").mkdir(parents=True)
        self.env: dict[str, str] = {}
        self.ran: list[str] = []
        self.outputs: dict[str, str] = {}
        self.fetched: list[tuple[str, dict]] = []
        self.answer: tuple[int, object] = (200, None)
        self.now = EPOCH

    def run(self, command: str) -> str:
        self.ran.append(command)
        if command not in self.outputs:
            raise RuntimeError("the command failed")
        return self.outputs[command]

    def fetch(self, url: str, headers) -> tuple[int, object]:
        self.fetched.append((url, dict(headers)))
        return self.answer

    def resolver(self) -> Resolver:
        return Resolver(self.env.get, self.run, home=self.home, fetch=self.fetch, clock=lambda: self.now)

    def write(self, name: str, text: str) -> None:
        (self.home / ".aws" / name).write_text(text, encoding="utf-8")

    def sso_token(self, key: str, token: str = "ACCESS", expires: str = "2026-11-11T20:00:00Z") -> None:
        (self.home / ".aws" / "sso" / "cache" / (hashlib.sha1(key.encode()).hexdigest() + ".json")).write_text(json.dumps({"accessToken": token, "expiresAt": expires}), encoding="utf-8")


@pytest.fixture
def setup(tmp_path) -> Setup:
    return Setup(tmp_path)


def test_the_region_is_the_models_then_the_environments_then_the_profiles_and_never_a_default(setup):
    resolver = setup.resolver()
    with pytest.raises(AwsError, match="区域"):
        resolver.region(BedrockSettings())
    setup.write("config", "[default]\nregion = eu-west-1\n\n[profile work]\nregion = ap-southeast-2\n")
    assert resolver.region(BedrockSettings()) == "eu-west-1"
    assert resolver.region(BedrockSettings(profile="work")) == "ap-southeast-2"
    setup.env["AWS_PROFILE"] = "work"
    assert resolver.region(BedrockSettings()) == "ap-southeast-2"
    setup.env["AWS_DEFAULT_REGION"] = "us-east-2"
    assert resolver.region(BedrockSettings()) == "us-east-2"
    setup.env["AWS_REGION"] = "us-west-1"
    assert resolver.region(BedrockSettings()) == "us-west-1"
    assert resolver.region(BedrockSettings(region="ca-central-1")) == "ca-central-1"


def test_a_bedrock_api_key_in_the_environment_is_sent_as_a_bearer_token(setup):
    resolver = setup.resolver()
    with pytest.raises(AwsError, match="AWS_BEARER_TOKEN_BEDROCK"):
        resolver.access(BedrockSettings(method="environment-api-key"))
    setup.env["AWS_BEARER_TOKEN_BEDROCK"] = "bedrock-api-key-123"
    assert resolver.access(BedrockSettings(method="environment-api-key")) == Access(bearer="bedrock-api-key-123")


def test_without_a_method_the_bearer_token_comes_first_and_aws_credentials_follow(setup):
    setup.env.update(AWS_ACCESS_KEY_ID="AK", AWS_SECRET_ACCESS_KEY="SK", AWS_SESSION_TOKEN="ST")
    resolver = setup.resolver()
    found = resolver.access(BedrockSettings())
    assert found.bearer == "" and (found.credentials.access_key, found.credentials.secret_key, found.credentials.token) == ("AK", "SK", "ST")
    setup.env["AWS_BEARER_TOKEN_BEDROCK"] = "key"
    assert resolver.access(BedrockSettings()).bearer == "key"
    assert resolver.access(BedrockSettings(method="aws-credentials")).bearer == "", "a method that names the chain does not look for the key"


def test_the_environment_keys_need_both_halves(setup):
    setup.env["AWS_ACCESS_KEY_ID"] = "AK"
    with pytest.raises(AwsError, match="找不到 AWS 凭据"):
        setup.resolver().access(BedrockSettings(method="aws-credentials"))


def test_a_named_profile_is_read_from_the_credentials_file_and_ignores_the_environment(setup):
    setup.env.update(AWS_ACCESS_KEY_ID="ENV", AWS_SECRET_ACCESS_KEY="ENVSECRET")
    setup.write("credentials", "[default]\naws_access_key_id = DEFAULTKEY\naws_secret_access_key = DEFAULTSECRET\n\n[work]\naws_access_key_id = WORKKEY\naws_secret_access_key = WORKSECRET\naws_session_token = WORKTOKEN\n")
    resolver = setup.resolver()
    named = resolver.access(BedrockSettings(method="aws-credentials", profile="work")).credentials
    assert (named.access_key, named.secret_key, named.token) == ("WORKKEY", "WORKSECRET", "WORKTOKEN")
    assert resolver.access(BedrockSettings(method="aws-credentials")).credentials.access_key == "ENV", "no profile named: the environment first"
    setup.env.clear()
    assert resolver.access(BedrockSettings(method="aws-credentials")).credentials.access_key == "DEFAULTKEY"
    setup.env["AWS_PROFILE"] = "work"
    assert resolver.access(BedrockSettings()).credentials.access_key == "WORKKEY", "AWS_PROFILE picks the profile when the model names none"


def test_a_profile_that_is_not_there_is_an_error_when_the_model_names_it(setup):
    with pytest.raises(AwsError, match="没有 profile「ghost」"):
        setup.resolver().access(BedrockSettings(method="aws-credentials", profile="ghost"))


def test_a_profile_may_get_its_credentials_from_a_process(setup):
    setup.write("config", "[profile proc]\ncredential_process = fetch-keys --role x\n")
    setup.outputs["fetch-keys --role x"] = json.dumps({"Version": 1, "AccessKeyId": "PK", "SecretAccessKey": "PS", "SessionToken": "PT", "Expiration": "2026-11-11T14:00:00Z"})
    found = setup.resolver().access(BedrockSettings(profile="proc")).credentials
    assert (found.access_key, found.token) == ("PK", "PT") and setup.ran == ["fetch-keys --role x"]
    setup.outputs["fetch-keys --role x"] = "garbage"
    setup.now += 7200
    with pytest.raises(AwsError, match="JSON"):
        setup.resolver().access(BedrockSettings(profile="proc"))


def test_credentials_are_remembered_until_they_near_their_end(setup):
    setup.write("config", "[profile proc]\ncredential_process = fetch\n")
    setup.outputs["fetch"] = json.dumps({"AccessKeyId": "K1", "SecretAccessKey": "S", "Expiration": "2026-11-11T13:30:00Z"})
    resolver = setup.resolver()
    assert resolver.access(BedrockSettings(profile="proc")).credentials.access_key == "K1"
    setup.outputs["fetch"] = json.dumps({"AccessKeyId": "K2", "SecretAccessKey": "S", "Expiration": "2026-11-11T18:00:00Z"})
    assert resolver.access(BedrockSettings(profile="proc")).credentials.access_key == "K1" and len(setup.ran) == 1
    setup.now += 3300  # (the first ones end in 59 minutes; four are left: within the five minutes before the end)
    assert resolver.access(BedrockSettings(profile="proc")).credentials.access_key == "K2" and len(setup.ran) == 2


def test_sso_trades_the_cached_login_for_the_roles_credentials(setup):
    setup.write("config", "[profile corp]\nsso_session = corp-sso\nsso_account_id = 123456789012\nsso_role_name = Dev Role\nregion = us-west-2\n\n[sso-session corp-sso]\nsso_start_url = https://corp.awsapps.com/start\nsso_region = us-east-1\n")
    setup.sso_token("corp-sso", "THE-ACCESS-TOKEN")
    setup.answer = (200, {"roleCredentials": {"accessKeyId": "RK", "secretAccessKey": "RS", "sessionToken": "RT", "expiration": int((EPOCH + 3600) * 1000)}})
    found = setup.resolver().access(BedrockSettings(profile="corp")).credentials
    assert (found.access_key, found.secret_key, found.token) == ("RK", "RS", "RT") and found.expires == pytest.approx(EPOCH + 3600)
    ((url, headers),) = setup.fetched
    assert url == "https://portal.sso.us-east-1.amazonaws.com/federation/credentials?role_name=Dev%20Role&account_id=123456789012"
    assert headers["x-amz-sso_bearer_token"] == "THE-ACCESS-TOKEN"


def test_the_older_sso_profile_without_a_session_is_found_by_its_start_url(setup):
    setup.write("config", "[default]\nsso_start_url = https://old.awsapps.com/start\nsso_region = eu-west-1\nsso_account_id = 111\nsso_role_name = R\n")
    setup.sso_token("https://old.awsapps.com/start")
    setup.answer = (200, {"roleCredentials": {"accessKeyId": "OK", "secretAccessKey": "OS", "sessionToken": "OT", "expiration": 0}})
    assert setup.resolver().access(BedrockSettings()).credentials.access_key == "OK"
    assert setup.fetched[0][0].startswith("https://portal.sso.eu-west-1.amazonaws.com/")


def test_an_sso_login_that_is_missing_expired_or_refused_says_to_log_in_again(setup):
    setup.write("config", "[profile corp]\nsso_session = s\nsso_account_id = 1\nsso_role_name = R\n\n[sso-session s]\nsso_region = us-east-1\nsso_start_url = https://x/start\n")
    with pytest.raises(AwsError, match="aws sso login --profile corp"):
        setup.resolver().access(BedrockSettings(profile="corp"))
    setup.sso_token("s", expires="2026-11-11T10:00:00Z")
    with pytest.raises(AwsError, match="过期.*aws sso login --profile corp"):
        setup.resolver().access(BedrockSettings(profile="corp"))
    assert setup.fetched == []
    setup.sso_token("s", expires="2026-11-11T20:00:00Z")
    setup.answer = (401, {"message": "no"})
    with pytest.raises(AwsError, match="不认这个登录"):
        setup.resolver().access(BedrockSettings(profile="corp"))
    setup.answer = (200, {"unexpected": True})
    with pytest.raises(AwsError, match="没有给出角色凭据"):
        setup.resolver().access(BedrockSettings(profile="corp"))


def test_an_incomplete_sso_profile_and_a_missing_session_section_are_named(setup):
    setup.write("config", "[profile bad]\nsso_session = nowhere\nsso_account_id = 1\nsso_role_name = R\n\n[profile half]\nsso_start_url = https://x/start\n")
    with pytest.raises(AwsError, match=r"\[sso-session nowhere\]"):
        setup.resolver().access(BedrockSettings(profile="bad"))
    with pytest.raises(AwsError, match="SSO 设置不完整"):
        setup.resolver().access(BedrockSettings(profile="half"))


def test_a_profile_that_assumes_a_role_is_said_to_be_unsupported(setup):
    setup.write("config", "[profile assume]\nrole_arn = arn:aws:iam::1:role/x\nsource_profile = default\n")
    with pytest.raises(AwsError, match="role_arn.*credentials-command"):
        setup.resolver().access(BedrockSettings(profile="assume"))


def test_the_credential_export_command_prints_credentials_and_is_remembered(setup):
    setup.outputs["export-keys"] = json.dumps({"Credentials": {"AccessKeyId": "XK", "SecretAccessKey": "XS", "SessionToken": "XT", "Expiration": "2026-11-11T16:00:00Z"}})
    settings = BedrockSettings(method="credentials-command", credential_export="export-keys")
    resolver = setup.resolver()
    assert resolver.access(settings).credentials.access_key == "XK"
    resolver.access(settings)
    assert setup.ran == ["export-keys"]
    with pytest.raises(AwsError, match="awsCredentialExport"):
        setup.resolver().access(BedrockSettings(method="credentials-command"))
    setup.outputs.clear()
    with pytest.raises(AwsError, match="命令没有成功"):
        setup.resolver().access(BedrockSettings(method="credentials-command", credential_export="other"))


def test_without_a_method_the_export_command_is_tried_after_the_chain(setup):
    setup.outputs["export-keys"] = json.dumps({"Credentials": {"AccessKeyId": "XK", "SecretAccessKey": "XS"}})
    settings = BedrockSettings(credential_export="export-keys")
    assert setup.resolver().access(settings).credentials.access_key == "XK"
    setup.env.update(AWS_ACCESS_KEY_ID="AK", AWS_SECRET_ACCESS_KEY="SK")
    setup.ran.clear()
    assert setup.resolver().access(settings).credentials.access_key == "AK" and setup.ran == [], "the chain came first"


def test_the_login_command_runs_when_nothing_was_found_and_the_chain_is_tried_again(setup):
    resolver = setup.resolver()
    settings = BedrockSettings(method="aws-credentials", auth_refresh="aws sso login")

    def login(command: str) -> str:
        setup.ran.append(command)
        setup.env.update(AWS_ACCESS_KEY_ID="AFTER", AWS_SECRET_ACCESS_KEY="LOGIN")
        return ""

    resolver = Resolver(setup.env.get, setup.run, login, home=setup.home, fetch=setup.fetch, clock=lambda: setup.now)
    assert resolver.access(settings).credentials.access_key == "AFTER" and setup.ran == ["aws sso login"]
    with pytest.raises(AwsError, match="找不到 AWS 凭据"):
        setup.env.clear()
        Resolver(setup.env.get, setup.run, lambda command: "", home=setup.home).access(settings)


def test_a_missing_credential_says_where_it_looked_and_what_to_do(setup):
    with pytest.raises(AwsError) as plain:
        setup.resolver().access(BedrockSettings(method="aws-credentials"))
    assert "环境变量" in str(plain.value) and "awsAuthRefresh" in str(plain.value)
    setup.write("credentials", "[named]\naws_access_key_id = A\n")
    with pytest.raises(AwsError, match="profile「named」"):
        setup.resolver().access(BedrockSettings(method="aws-credentials", profile="named"))
