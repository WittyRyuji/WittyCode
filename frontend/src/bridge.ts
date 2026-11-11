// The accounts, providers and window part of the contract between this page and the Python side (the whole protocol is
// in protocol.ts). The page keeps no secrets and starts no processes. In the real app `bridge` is `liveBridge`
// (liveBridge.ts: calls through QWebChannel); in the design page it is the mock in `mock/bridge.ts`.
//
// Python side, in short:
//   providers.*  read and write `customModels` in ~/.factory/settings.json (Droid's BYOK list) and the provider names that
//                WittyCode keeps; `test` sends one minimal request through the same base URL / key / headers the session would use.
//                Keys are never written: `apiKey` is `${NAME}` (an environment variable) or absent, `apiKeyHelper` is a
//                command. An existing plaintext `apiKey` is left alone until the user switches that provider to a variable
//                (then it is removed). `envStatus` reads os.environ and, for "pending", HKCU\Environment.
//   window.*     the frameless window's controls: the title bar is drawn by the page. Python: Qt.FramelessWindowHint,
//                `QWindow.startSystemMove()` / `startSystemResize(edges)` so Windows snapping keeps working, and a push of
//                the maximized state back to the page.
//   codex.*      talk to `codex app-server` (JSON-RPC over stdio): account/read, account/login/start (opens the browser),
//                account/logout, account/rateLimits/read. The login tokens stay in Codex's own ~/.codex/auth.json.
//   devin.*      the Devin CLI (`devin auth status|login|logout`, `devin acp` for the sessions). Its sign-in is shared with the
//                Devin desktop app; the tokens stay in Devin's credentials.toml.
//   chatgpt.*    the ChatGPT plan as an engine run by WittyCode's own agent loop (desktop/core/chatgpt): OpenAI's "Sign in with
//                ChatGPT" for open-source apps (browser, PKCE, loopback callback). The tokens stay in WittyCode's own folder.
//   factory.*    Droid's own sign-in state (droid-sdk), read only: signing in is `droid` in a terminal.
//   memory.*     the shared memory (desktop/core/memory): notes that Droid, Codex, Devin and the user read and write. The page lists,
//                edits, forgets and restores them; every change is journalled and `undo` takes one back.

import type { ApiProvider, ChatGPTAccount, CodexAccount, DevinAccount, EnvState, FactoryAccount, ProviderDraft, RemoteModel } from "./types";
import type { Calls } from "./protocol";

export interface TestResult {
  ok: boolean;
  /** one line for the user: "连接成功 · 420 ms · 14 个模型" / "401：密钥无效" */
  text: string;
  /** null when the request did not get through */
  ms?: number | null;
  /** model ids the server lists (`GET {baseUrl}/models`), when it has that endpoint; null when it does not */
  models?: string[] | null;
}

export interface Bridge {
  factory: {
    account(): Promise<FactoryAccount>;
  };
  codex: {
    account(): Promise<CodexAccount>;
    /** opens the browser for the ChatGPT sign-in; resolves with the account once the user has signed in (or cancelled) */
    login(): Promise<CodexAccount>;
    cancelLogin(): Promise<CodexAccount>;
    logout(): Promise<CodexAccount>;
    setEnabled(on: boolean): Promise<CodexAccount>;
  };
  devin: {
    account(): Promise<DevinAccount>;
    /** `devin auth login` opens the browser; resolves with the account once the user has signed in (or cancelled) */
    login(): Promise<DevinAccount>;
    cancelLogin(): Promise<DevinAccount>;
    logout(): Promise<DevinAccount>;
    setEnabled(on: boolean): Promise<DevinAccount>;
  };
  chatgpt: {
    account(): Promise<ChatGPTAccount>;
    /** "Continue with ChatGPT": opens the browser; resolves with the account once the user has finished there (or cancelled) */
    login(options?: { add?: boolean; forceConsent?: boolean }): Promise<ChatGPTAccount>;
    cancelLogin(): Promise<ChatGPTAccount>;
    logout(clientId?: string): Promise<ChatGPTAccount>;
    use(clientId: string): Promise<ChatGPTAccount>;
    setEnabled(on: boolean): Promise<ChatGPTAccount>;
    welcomed(): Promise<ChatGPTAccount>;
  };
  providers: {
    save(draft: ProviderDraft): Promise<ApiProvider>;
    remove(id: string): Promise<void>;
    test(draft: ProviderDraft): Promise<TestResult>;
    listModels(draft: ProviderDraft): Promise<RemoteModel[]>;
    /** for each variable name: is it there for the droid that WittyCode starts */
    envStatus(names: string[]): Promise<Record<string, EnvState>>;
  };
  memory: {
    list(filter?: Calls["memory.list"]["params"]): Promise<Calls["memory.list"]["result"]>;
    changes(since?: number): Promise<Calls["memory.changes"]["result"]>;
    create(note: Calls["memory.create"]["params"]): Promise<Calls["memory.create"]["result"]>;
    update(edit: Calls["memory.update"]["params"]): Promise<Calls["memory.update"]["result"]>;
    forget(id: string): Promise<Calls["memory.forget"]["result"]>;
    restore(id: string): Promise<Calls["memory.restore"]["result"]>;
    undo(seq: number): Promise<Calls["memory.undo"]["result"]>;
    markSeen(seq?: number): Promise<Calls["memory.markSeen"]["result"]>;
    /** the notes the application wrote by itself, for the weekly review */
    review(): Promise<Calls["memory.review"]["result"]>;
    reviewApply(verdict: Calls["memory.reviewApply"]["params"]): Promise<Calls["memory.reviewApply"]["result"]>;
  };
  /** bringing in what other applications keep: memory files and conversations (desktop/core/importers) */
  importer: {
    scan(): Promise<Calls["import.scan"]["result"]>;
    memories(apps: Calls["import.memories"]["params"]["apps"]): Promise<Calls["import.memories"]["result"]>;
    sessions(apps: Calls["import.sessions"]["params"]["apps"]): Promise<Calls["import.sessions"]["result"]>;
    commit(memories: string[], sessions: string[]): Promise<Calls["import.commit"]["result"]>;
    undo(ids: string[]): Promise<Calls["import.undo"]["result"]>;
  };
  window: {
    minimize(): Promise<void>;
    /** resolves with the new state */
    toggleMaximize(): Promise<boolean>;
    close(): Promise<void>;
    /** the mouse went down on the title bar: let the system move the window (also gives snapping) */
    startMove(): Promise<void>;
    /** the mouse went down on a window edge or corner */
    startResize(edge: "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw"): Promise<void>;
    isMaximized(): Promise<boolean>;
  };
}
