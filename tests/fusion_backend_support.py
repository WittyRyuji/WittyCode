"""Explicit offline profiles; a newly registered backend without a peer is a contract failure, not a skip."""
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import struct
import sys
import urllib.request
import zipfile

from pip._vendor import distlib

from desktop.core import factory_guard
from desktop.core.chatgpt.http import HttpClient
from desktop.core.factory import FactoryEngine
from desktop.core.fusion.backends import BACKENDS
from desktop.core.llm.backend import ApiBackend
from desktop.core.providers import ProviderStore
from tests.app_support import ready_core
from tests.chatgpt_support import make_engine
from tests.fake_responses import DEFAULT_MODELS, FakeResponses
from tests.fakes import until
from tests.fusion_acp_support import make_guarded_engine
from tests.llm_support import FakeApi
from tests.test_app_api import chunk
from tests.test_factory_guard import site_policy

RUNNERS = tuple(b.name for b in BACKENDS)
PROFILES = {"plan": ("gpt-6.1-terra", "low"), "api": ("deepseek-v4.1-flash", "low"),
            "devin": ("swe-2-high", "medium"), "factory": ("demo-wire", "low")}


def api_call(arguments, name="delegate_tasks", call_id="matrix_delegate"):
    return [chunk({"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(arguments)}}]},
                  finish="tool_calls"), "[DONE]"]


def factory_peer(tmp_path, monkeypatch):
    """Model a machine with its own policy, CLI and ancestors, without replacing the guard checks."""
    work = Path(os.environ["WITTYCODE_TEST_WORK"]).resolve(strict=True)
    assert tmp_path.resolve(strict=True).is_relative_to(work), "keep the fake peer in the managed run"
    ancestors = factory_guard._scratch_ancestors

    def in_test_machine(base):
        assert base.is_relative_to(work), "the offline seat must use this run's scratch space"
        return tuple(path for path in ancestors(base) if path.is_relative_to(work))

    # Only model the absence of config above the test machine's root. All marker checks inside it are real.
    monkeypatch.setattr(factory_guard, "_scratch_ancestors", in_test_machine)
    root = tmp_path / "factory-peer"
    root.mkdir()
    policy = root / "site.json"
    policy.write_text(json.dumps(site_policy()))
    monkeypatch.setattr(factory_guard, "_SYSTEM_POLICY", policy)
    monkeypatch.setenv("FACTORY_API_KEY", "test-" + "fixture")
    # A standard Python console launcher is an actual .exe, including for the subprocess version check.
    exe = root / "peer.exe"
    zipdata = io.BytesIO()
    with zipfile.ZipFile(zipdata, "w") as archive:
        archive.writestr("__main__.py", f"import sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
                         "from tests.fake_fusion_factory import serve\nserve()\n")
    launcher = Path(distlib.__file__).with_name(f"t{struct.calcsize('P') * 8}.exe")
    exe.write_bytes(launcher.read_bytes() + f'#!"{sys.executable}"\n'.encode() + zipdata.getvalue())
    return FactoryEngine(None, tmp_path / "factory-index", locate=lambda _: str(exe))


@asynccontextmanager
async def peers(tmp_path, monkeypatch):
    terra = {**DEFAULT_MODELS[0], "slug": PROFILES["plan"][0], "display_name": "Terra 6.1"}
    with FakeResponses(models=[*DEFAULT_MODELS, terra]) as plan, FakeApi() as api:
        profile = tmp_path / "models.json"
        profile.write_text(json.dumps({"customModels": [{"model": PROFILES["api"][0], "displayName": "DS 4.1 Flash",
            "baseUrl": api.base_url, "apiKey": "${MATRIX_TEST_KEY}", "provider": "generic-chat-completion-api",
            "reasoningEffort": "medium", "maxContextLimit": 64000}]}), encoding="utf-8")
        providers = ProviderStore(models_file=profile, sidecar=tmp_path / "providers.json",
                                  environ={"MATRIX_TEST_KEY": "test-fixture"}, registry_env=lambda: {})
        http = HttpClient(urllib.request.build_opener(urllib.request.ProxyHandler({})))
        engine = make_engine(tmp_path, plan, api=ApiBackend(providers, http=http))
        devin = make_guarded_engine(tmp_path, FAKE_FUSION_BRIDGE_NOTICE="id-only", FAKE_FUSION_MCP_WRAPPERS="1")
        core, *_ = await ready_core(tmp_path, chatgpt_engine=engine, devin_engine=devin.engine,
                                    factory_engine=factory_peer(tmp_path, monkeypatch), providers=providers)
        try:
            await until(lambda: core.catalog.find("devin:" + PROFILES["devin"][0]), timeout=20)
            models = {}
            for backend in BACKENDS:
                native, _ = PROFILES[backend.name]
                (row,) = [r for r in core.catalog.models if r.get("fusionBackend") == backend.name and
                          (r["id"] == backend.prefix + native or backend.name == "api" and r["label"] == "DS 4.1 Flash")]
                models[backend.name] = row["id"]
            yield core, models, plan, api
        finally:
            await core.shutdown()


async def configure(core, models, main, worker, light):
    seats = {"main": main, "worker": worker, "light": light}
    levels = {f"{role}:{models[name]}": PROFILES[name][1] for role, name in seats.items()}
    for key, value in {"fusionMain": models[main], "fusionWorkers": [models[worker]], "fusionLight": models[light],
                       "fusionReasoning": levels}.items():
        await core.call("settings.set", {"key": key, "value": value})


def factory_turns(tmp_path):
    return [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "factory-peer").glob("*.turn")]
