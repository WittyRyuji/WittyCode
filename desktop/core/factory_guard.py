"""One-turn Factory JSON-RPC transport for a fresh Fusion tool bridge.

    bridge = factory_bridge(host, tools, watcher, history, instructions, steps_used=spent)
    async with engine.guarded_client(project, model=native_id, reasoning=effort, bridge=bridge,
                                     on_notification=observe, scratch_base=private_temp_base) as seat:
        terminal = await seat.prompt(prompt_with_context)

The synchronous observer receives native notifications; terminal is agent_turn_completed,
not the send acknowledgement. Use bridge.boundary for host-requested stops. Tool-free seats
pass tool_free=True to factory_bridge. Do not retry a refusal via the standalone client.
There is no native resume; the caller owns history, attachments, budgets and usage.

1.18.0 ignores --only-tools in stream-jsonrpc. Use a private personal mcp.json (subject
 to org MCP policy), NOT session-injected mcpServers. ToolSearch may still be advertised;
a blocking PreToolUse hook rejects it and every other non-bridge invocation before use.
See docs.factory.com/harness/{mcp,hooks} and /enterprise/hierarchical-settings-and-org-control.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

if __name__ == "__main__":  # The pinned, isolated Python interpreter executes only this application hook.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from desktop.core.fusion import acp_server

_VERSION = "1.18.0"
_STARTUP_SECONDS = 15
# Only consume existing IT-managed policy. Never install a file here to supersede an organization's remote policy.
# Factory's authoritative Windows path is hardcoded, not relative to the child's private profile.
_SYSTEM_POLICY = Path(r"C:\Program Files\Factory\settings.json")
_MATCHER = "desktop.core.fusion.acp_server"
_SERVER = acp_server.SERVER


class FactoryTurnDisconnected(RuntimeError):
    """The checked seat disappeared after accepting work, without a terminal event."""


class FactoryGuardUnavailable(RuntimeError):
    """The requested seat could not establish its execution boundary; do not submit a prompt."""


def _check_launch_environment(env):
    # Do not resolve a different path or contact a URL. Only forward the very pathname we checked.
    local = env.get("FACTORY_ORG_MANAGED_SETTINGS_LOCAL_PATH")
    if env.get("FACTORY_ORG_MANAGED_SETTINGS_URL") or (local and local != str(_SYSTEM_POLICY)):
        raise FactoryGuardUnavailable("Factory 组织策略来源与已验证的机器策略不一致；不允许启动或继续席位。")
    root = env.get("SYSTEMROOT")
    if not root or not Path(root).is_absolute():
        raise FactoryGuardUnavailable("Factory 执行前钩子缺少有效的 SYSTEMROOT；不允许启动或继续席位。")
    if Path(env.get("COMSPEC", "")) != Path(root) / "System32/cmd.exe":
        raise FactoryGuardUnavailable("Factory 执行前钩子需要系统命令处理器；不允许启动或继续席位。")


def _check_site():
    # Never replace/disable org policy or assume a clean personal home disables remote policy.
    # Support only this narrow, authoritative deployment; other org features need a separate audit.
    # In particular, an org SessionStart hook can execute *before* an initialize reply.
    try:
        if sys.platform != "win32" or _SYSTEM_POLICY.resolve(strict=True) != _SYSTEM_POLICY:
            raise ValueError
        site = json.loads(_SYSTEM_POLICY.read_text(encoding="utf-8-sig"))
        if (not isinstance(site, dict) or set(site) != {"mcpPolicy", "strictEnabledPlugins", "strictKnownMarketplaces"}
                or site["strictEnabledPlugins"] is not True or site["strictKnownMarketplaces"] != []
                or site["mcpPolicy"] != {"enabled": True, "allowlist": [_MATCHER]}
                or site["mcpPolicy"]["enabled"] is not True):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise FactoryGuardUnavailable("无法确认机器托管策略仅允许 Fusion 工具桥且无启动钩子；不允许启动或继续 Factory 席位。") from None
    _check_launch_environment(os.environ)


def _target(name):
    if isinstance(name, str):
        for prefix in (_SERVER + "___", acp_server.PREFIX):
            if name.startswith(prefix) and re.fullmatch(r"[a-zA-Z0-9_-]+", name[len(prefix):]):
                return acp_server.PREFIX + name[len(prefix):]
    return ""


def _hook(payload):
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        acp_server.request("guard", {"event": event})
        return 0
    target = _target(payload.get("tool_name"))
    if event == "PreToolUse" and target:
        _check_site()  # A previously eligible seat cannot keep using tools after a machine-policy change.
        if acp_server.request("guard", {"event": event, "name": target, "input": payload.get("tool_input")}).get("allowed") is True:
            return 0
    return 2


def _environment(home):
    # Do not strip a configured enterprise/airgap endpoint to make a seat work. Reject unsupported modes.
    if any(k.upper().startswith(("DROID_", "FACTORY_")) and k.upper() not in {
            "FACTORY_API_KEY", "FACTORY_ORG_MANAGED_SETTINGS_URL", "FACTORY_ORG_MANAGED_SETTINGS_LOCAL_PATH"}
           for k in os.environ):
        raise FactoryGuardUnavailable("Factory 环境带有未验证的配置；未启动 CLI。")
    keep = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
            "ALLUSERSPROFILE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
            "FACTORY_API_KEY", "FACTORY_ORG_MANAGED_SETTINGS_URL", "FACTORY_ORG_MANAGED_SETTINGS_LOCAL_PATH")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    _check_launch_environment(env)  # Validate the child snapshot as well as the earlier seat preflight.
    env["FACTORY_DROID_AUTO_UPDATE_ENABLED"] = "false"  # Use the inspected version for this process.
    tmp, roaming, local = home / "tmp", home / "AppData/Roaming", home / "AppData/Local"
    for path in (tmp, roaming, local):
        path.mkdir(parents=True)
    env.update(HOME=str(home), FACTORY_HOME_OVERRIDE=str(home), USERPROFILE=str(home), APPDATA=str(roaming),
               LOCALAPPDATA=str(local), XDG_CONFIG_HOME=str(home / "config"), TMP=str(tmp), TEMP=str(tmp),
               PATH=str(Path(env["SYSTEMROOT"]) / "System32"), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env


def _scratch_ancestors(base: Path) -> tuple[Path, ...]:
    return (base, *base.parents)  # Include both the scratch base itself and the volume root.


def _check_scratch(base, project):
    if (not base.is_absolute() or base.drive.startswith("\\\\") or base.resolve(strict=True) != base or base.is_relative_to(project)
            or any((p / name).exists() for p in _scratch_ancestors(base) for name in (".git", ".factory", ".agents", ".claude"))):
        raise FactoryGuardUnavailable("Factory 私有工作区不能位于工程或带有代理配置的上级目录内；未启动 CLI。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    volume, flags = ctypes.create_unicode_buffer(32768), wintypes.DWORD()
    if (not kernel.GetVolumePathNameW(str(base), volume, len(volume)) or kernel.GetDriveTypeW(volume) != 3
            or not kernel.GetVolumeInformationW(volume, None, 0, None, None, ctypes.byref(flags), None, 0)
            or not flags.value & 0x8):  # FILE_PERSISTENT_ACLS: CreateDirectory must actually enforce its DACL.
        raise FactoryGuardUnavailable("Factory 私有工作区需要支持访问控制的本地磁盘；未启动 CLI。")


@contextlib.contextmanager
def _private_root(base):
    # Python 3.11.9 ignores mkdir(0700) on Windows. Set an owner/SYSTEM/admin-only inheritable DACL
    # in CreateDirectory itself, not by adjusting a possibly shared directory after the fact.
    class Attributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = ctypes.WinDLL("advapi32", use_last_error=True).ConvertStringSecurityDescriptorToSecurityDescriptorW
    descriptor = ctypes.c_void_p()
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    root = base / ("fusion-factory-" + uuid.uuid4().hex)
    try:
        attrs = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        if not kernel.CreateDirectoryW(str(root), ctypes.byref(attrs)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)
    owned = root.lstat()
    try:
        yield root
    finally:
        now = root.lstat()
        if (not owned.st_ino or not os.path.samestat(owned, now) or now.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or root.resolve(strict=True) != root):
            raise OSError("Factory scratch ownership changed; refusing cleanup")
        shutil.rmtree(root)


class _EmptyManifest:
    @staticmethod
    def declarations():
        return []

    @staticmethod
    def known(name):
        return False


def factory_bridge(host, tools, watcher, history, instructions, *, steps_used=0, tool_free=False):
    """Prepare the same host permission, budget, evidence and tool dispatch used by Fusion ACP."""
    from desktop.core.fusion.acp import AcpRun
    from desktop.core.harness.loop import AgentLoop

    bridge = AcpRun(host)
    bridge.tools, bridge.watcher, bridge.history = tools, watcher, history
    bridge.steps, bridge.tool_free = steps_used, tool_free
    bridge.loop = AgentLoop(None, tools, watcher, model=host.model, reasoning=None, instructions=instructions)
    return bridge


def guarded_factory_client(engine, project, *, model="", reasoning="", bridge=None, on_notification=None, scratch_base=None):
    _check_site()
    if not os.environ.get("FACTORY_API_KEY"):
        raise FactoryGuardUnavailable("Factory 隔离席位需要通过 FACTORY_API_KEY 鉴权；不复制桌面登录凭据，未启动 CLI。")
    from desktop.core.fusion.acp import AcpRun
    if not engine.settings.factory_enabled or not model or not isinstance(bridge, AcpRun):
        raise FactoryGuardUnavailable("需要已启用的 Factory、明确的模型和 Fusion 工具桥；未启动 CLI。")
    return _Seat(engine, Path(project).resolve(), model, reasoning, bridge, on_notification,
                 Path(scratch_base) if scratch_base else Path(tempfile.gettempdir()))


class _Seat:
    def __init__(self, engine, project, model, reasoning, bridge, callback, base):
        self.engine, self.project, self.model, self.reasoning = engine, project, model, reasoning
        self.bridge, self.callback, self.base = bridge, callback, base
        self._client = self.server = self.temp = self.end = None
        self.session = ""
        self.names = ()
        self.attempted = self.closed = self.owns_bridge = self.ready = False
        self.opening = self.closing = None

    async def __aenter__(self):
        if self.opening is not None or self.closed or self.owns_bridge:
            raise RuntimeError("Factory 席位只能打开一次。")
        self.opening = asyncio.create_task(self._open())
        try:
            await self.opening
        except BaseException:
            await self.close()
            raise
        finally:
            self.opening = None
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _open(self):
        from desktop.core.factory import EXEC_ARGS, FactoryClient
        from desktop.core.memory.launch import console_python

        _check_site()
        if self.bridge.active or self.bridge.server or self.bridge.client or getattr(self.bridge, "_factory_bound", False):
            raise FactoryGuardUnavailable("Factory 需要独占的新工具桥。")
        self.owns_bridge = self.bridge._factory_bound = True
        _check_scratch(self.base, self.project)
        path = await asyncio.to_thread(self.engine.binary)
        if not path or Path(path).suffix.lower() != ".exe" or Path(path).resolve().is_relative_to(self.project):
            raise FactoryGuardUnavailable("未找到工程外受支持的 Factory 可执行文件。")
        path = str(Path(path).resolve(strict=True))
        if self.bridge.tool_free:
            self.bridge.tools = _EmptyManifest()  # The host tools/call boundary must also reject every invocation.
        self.names = tuple(t["name"] for t in self.bridge.tools.declarations())
        if len(set(self.names)) != len(self.names) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in self.names):
            raise FactoryGuardUnavailable("工具清单无法无歧义地映射到 Factory。")
        scratch = _private_root(self.base)
        root = scratch.__enter__()
        self.temp = scratch
        home, cwd = root / "home", root / "workspace"
        cwd.mkdir()
        env = _environment(home)
        version_task = asyncio.create_task(asyncio.to_thread(subprocess.run, [path, "--version"], cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW))
        try:
            version = await asyncio.shield(version_task)
        except asyncio.CancelledError:
            await asyncio.gather(version_task, return_exceptions=True)  # The version process still owns its scratch cwd.
            raise
        if version.stdout.strip() != _VERSION.encode("ascii"):
            raise FactoryGuardUnavailable("此 Factory 版本尚未验证执行前保护；未发送请求。")
        python = console_python()
        hook = str(Path(__file__).resolve())
        if any(Path(p).resolve().is_relative_to(self.project) for p in (python, hook, acp_server.__file__)):
            raise FactoryGuardUnavailable("不能从任务可修改的工程加载权限钩子或工具桥。")
        if any(re.search(r'[\r\n%&|<>^!()"]', part) for part in (python, hook)):
            raise FactoryGuardUnavailable("Factory 执行前钩子路径不能安全传给 Windows 命令处理器。")
        config = home / ".factory"
        config.mkdir()
        if self.names:
            peer = {"type": "stdio", "command": python, "args": ["-I", str(Path(acp_server.__file__).resolve())]}
            (config / "mcp.json").write_text(json.dumps({"mcpServers": {_SERVER: peer}}), encoding="utf-8")
        command = subprocess.list2cmdline([python, "-I", hook])
        group = [{"hooks": [{"type": "command", "command": command, "onFailure": "block", "timeout": 8}]}]
        runtime = root / "runtime.json"
        runtime.write_text(json.dumps({"hooks": {k: group for k in ("SessionStart", "PreToolUse")}}), encoding="utf-8")
        self.bridge.guard_seen.clear()
        self.bridge.bridge_seen.clear()
        self.server = await asyncio.start_server(self.bridge._http, "127.0.0.1", 0, limit=16_384)
        port = self.server.sockets[0].getsockname()[1]
        env.update({acp_server.AUTH: self.bridge.credential, acp_server.ADDRESS: f"http://127.0.0.1:{port}/"})
        self._client = FactoryClient([path, *EXEC_ARGS, "--settings", str(runtime)], env=env, cwd=cwd,
                                    name="Fusion Factory", on_request=self._request, on_notification=self._notify, on_exit=self._lost)
        _check_site()
        try:
            await self._client.start()
        finally:
            for key in (acp_server.AUTH, "FACTORY_API_KEY"):
                self._client.env.pop(key, None)  # One-shot process; the parent transport need not retain a launch credential.
        # An empty restrictToolIds means *defaults*, not no tools. Intersect a singleton with its own denial instead.
        only = [f"MCP:{_SERVER}/{n}" for n in self.names] or ["Read"]
        denied = ["ToolSearch", "Script", "WaitForScript"] + ([] if self.names else ["Read"])
        params = {"cwd": str(cwd), "machineId": "fusion", "interactionMode": "auto", "autonomyLevel": "off",
                  "disableBuiltinSkills": True, "toolExecutionMode": "direct_only", "restrictToolIds": only,
                  "disabledToolIds": denied, "modelId": self.model}
        if self.reasoning:
            params["reasoningEffort"] = self.reasoning
        result = await self._client.request("droid.initialize_session", params, timeout=40)
        settings = result["settings"]
        if (not result.get("sessionId") or any(settings.get(k) != params[k] for k in (
                "toolExecutionMode", "modelId", "restrictToolIds", "disabledToolIds", "autonomyLevel", "interactionMode"))
                or self.reasoning and settings.get("reasoningEffort") != self.reasoning):
            raise FactoryGuardUnavailable("Factory 未确认所选模型或受控设置；未发送请求。")
        self.session = result["sessionId"]
        try:
            async with asyncio.timeout(_STARTUP_SECONDS):
                await self.bridge.guard_seen.wait()
                if self.names:
                    await self.bridge.bridge_seen.wait()
                while not await self._manifest_ready():
                    await asyncio.sleep(0.1)
        except TimeoutError:
            raise FactoryGuardUnavailable("未确认执行前钩子、私有工具桥或实际工具清单；未发送请求。") from None
        self.bridge.client, self.bridge.session = self, self.session  # AcpRun._stop sends one session/cancel.
        self.ready = True

    async def notify(self, method, params):
        if method != "session/cancel" or params != {"sessionId": self.session} or not (self.bridge.boundary or self.bridge.violation):
            raise RuntimeError("Unsupported Fusion bridge operation")
        try:
            await self._client.request("droid.interrupt_session", {}, timeout=5)
            await asyncio.wait_for(asyncio.shield(self.end), 5)
        except Exception:
            self._lost(None)

    async def _manifest_ready(self):
        servers = (await self._client.request("droid.list_mcp_servers", {}, timeout=5))["servers"]
        for server in servers:
            if (not self.names or server["name"] != _SERVER or server.get("source") != "user" or server.get("blockedByPolicy")
                    or server.get("status") not in ("connected", "connecting")):
                raise FactoryGuardUnavailable("Factory 注册了其他 MCP 或托管策略未放行工具桥。")
        tools = (await self._client.request("droid.list_tools", {}, timeout=5))["tools"]
        if not tools or any(type(t.get("currentlyAllowed")) is not bool for t in tools):
            raise FactoryGuardUnavailable("Factory 未返回可核实的工具状态。")
        allowed = {(t["id"], t["llmId"]) for t in tools if t["currentlyAllowed"]}
        expected = {("mcp_" + _SERVER + "_" + n, _SERVER + "___" + n) for n in self.names}
        if allowed - expected - ({("tool-search-cli", "ToolSearch")} if expected else set()):
            raise FactoryGuardUnavailable("Factory 仍开放原生或其他工具。")
        return expected <= allowed and (not expected or len(servers) == 1 and servers[0].get("status") == "connected")

    async def _request(self, method, params, request_id):
        if method == "droid.request_permission":
            uses = params.get("toolUses") or []
            options = [o.get("value") if isinstance(o, dict) else o for o in params.get("options", [])]
            targets = {acp_server.PREFIX + n for n in self.names}
            ok = (self.bridge.active and not self.bridge.violation and not self.bridge.boundary
                  and params.get("sessionId", self.session) == self.session and isinstance(uses, list) and uses
                  and "proceed_once" in options and all(isinstance(u, dict) and isinstance(u.get("toolUse"), dict)
                      and _target(u["toolUse"].get("name")) in targets for u in uses))
            return {"selectedOption": "proceed_once" if ok else "cancel"}
        if method == "droid.ask_user":
            return {"cancelled": True, "answers": []}
        raise RuntimeError("Unsupported Factory peer request")

    def _lost(self, error):
        self.bridge.active = False
        if self.end is not None and not self.end.done():
            self.end.set_result({"type": "disconnected"})

    def _notify(self, method, params):
        if method != "droid.session_notification" or params.get("sessionId") != self.session or not self.bridge.active:
            return
        event = params.get("notification", {})
        if event.get("type") in ("error", "agent_turn_completed") and not self.end.done():
            self.bridge.active = False
            self.end.set_result(event)
        if self.callback:
            self.callback(method, params)

    async def prompt(self, text, *, before_send=None):
        if self.closed or self.attempted or not self.ready or not text or not isinstance(text, str):
            raise RuntimeError("Factory 受控席位未就绪或已使用。")
        self.attempted = True
        try:
            _check_site()
            if not await self._manifest_ready() or self.closed:
                raise FactoryGuardUnavailable("Factory 工具清单或席位状态已改变；未发送请求。")
            if self.names and (self.bridge.loop is None or self.bridge.watcher is None or not self.bridge.watcher.current):
                raise FactoryGuardUnavailable("Fusion 席位的工具权限与证据观察器未就绪。")
            if self.bridge.watcher:
                self.bridge.watcher.begin_step()
            self.end = asyncio.get_running_loop().create_future()
            if before_send:
                before_send()  # Durable host checkpoint, after all local guard checks.
            self.bridge.active = True
            await self._client.request("droid.add_user_message", {"messageId": str(uuid.uuid4()), "text": "Fusion request:\n" + text}, timeout=30)
            result = await asyncio.shield(self.end)
            if result.get("type") == "disconnected" and not self.bridge.violation:
                raise FactoryTurnDisconnected("Factory 连接在回合完成前关闭。")
            finished = result.get("reason") == "completed" or (self.bridge.boundary and result.get("reason") in ("interrupted", "cancelled"))
            if result.get("type") != "agent_turn_completed" or not finished or self.bridge.violation:
                raise RuntimeError("Factory 回合未正常完成。")
            return result
        except asyncio.CancelledError:
            await self.cancel()
            raise
        finally:
            await self.close()

    async def cancel(self):
        if self.owns_bridge:
            self.bridge.active = False
        try:
            if self.end is not None and not self.end.done() and self._client.running:
                with contextlib.suppress(Exception):
                    await self._client.request("droid.interrupt_session", {}, timeout=3)
        finally:
            await self.close()

    async def close(self):
        if self.closing is None:
            self.closed = True
            self.closing = asyncio.create_task(self._shutdown())
        await asyncio.shield(self.closing)

    async def _shutdown(self):
        if self.opening is not None and not self.opening.done():
            self.opening.cancel()
            await asyncio.gather(self.opening, return_exceptions=True)
        if not self.owns_bridge:
            return
        self.bridge.active = False
        if self.end is not None and not self.end.done():
            self.end.cancel()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        self.bridge.client = None
        pending = list(self.bridge._connections)
        if self.bridge._cancel:
            pending.append(self.bridge._cancel)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._client is not None:
            await self._client.close()
        if self.temp is not None:
            self.temp.__exit__(None, None, None)
            self.temp = None


if __name__ == "__main__":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        code = _hook(json.load(sys.stdin))
    except Exception:
        code = 2
    if code:
        print("Fusion refused a non-bridge tool or the guard is unavailable.", file=sys.stderr)
    raise SystemExit(code)
