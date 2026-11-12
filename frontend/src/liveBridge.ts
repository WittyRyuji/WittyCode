// Calls to Python in the live app: `liveCall` (typed by protocol.ts) and the `Bridge` the components use, built on it.
// Kept apart from live.ts so that bridge-instance.ts can pick it without importing the store.

import type { Bridge } from "./bridge";
import type { Calls, Method } from "./protocol";
import { CallError, type Transport } from "./transport";

let transport: Transport | null = null;

export function connectTransport(t: Transport): void {
  transport = t;
}

type Args<M extends Method> = Record<string, never> extends Calls[M]["params"] ? [params?: Calls[M]["params"]] : [params: Calls[M]["params"]];

export function liveCall<M extends Method>(method: M, ...args: Args<M>): Promise<Calls[M]["result"]> {
  if (!transport) return Promise.reject(new CallError("没有连接到 WittyCode 后台。"));
  return transport.call<Calls[M]["result"]>(method, args[0] ?? {});
}

export const liveBridge: Bridge = {
  devin: {
    account: () => liveCall("devin.account"),
    login: () => liveCall("devin.login"),
    cancelLogin: () => liveCall("devin.cancelLogin"),
    logout: () => liveCall("devin.logout"),
    setEnabled: (on) => liveCall("devin.setEnabled", { on }),
  },
  chatgpt: {
    account: () => liveCall("chatgpt.account"),
    login: (options) => liveCall("chatgpt.login", options ?? {}),
    cancelLogin: () => liveCall("chatgpt.cancelLogin"),
    logout: (clientId) => liveCall("chatgpt.logout", clientId ? { clientId } : {}),
    use: (clientId) => liveCall("chatgpt.use", { clientId }),
    setEnabled: (on) => liveCall("chatgpt.setEnabled", { on }),
    welcomed: () => liveCall("chatgpt.welcomed"),
  },
  providers: {
    save: (draft) => liveCall("providers.save", { draft }),
    remove: async (id) => {
      await liveCall("providers.remove", { id });
    },
    test: (draft) => liveCall("providers.test", { draft }),
    listModels: (draft) => liveCall("providers.listModels", { draft }),
    envStatus: (names) => liveCall("providers.envStatus", { names }),
  },
  memory: {
    list: (filter) => liveCall("memory.list", filter ?? {}),
    changes: (since) => liveCall("memory.changes", since === undefined ? {} : { since }),
    create: (note) => liveCall("memory.create", note),
    update: (edit) => liveCall("memory.update", edit),
    forget: (id) => liveCall("memory.forget", { id }),
    restore: (id) => liveCall("memory.restore", { id }),
    undo: (seq) => liveCall("memory.undo", { seq }),
    markSeen: (seq) => liveCall("memory.markSeen", seq === undefined ? {} : { seq }),
    review: () => liveCall("memory.review"),
    reviewApply: (verdict) => liveCall("memory.reviewApply", verdict),
  },
  importer: {
    scan: () => liveCall("import.scan"),
    memories: (apps) => liveCall("import.memories", { apps }),
    sessions: (apps) => liveCall("import.sessions", { apps }),
    commit: (memories, sessions) => liveCall("import.commit", { memories, sessions }),
    undo: (ids) => liveCall("import.undo", { ids }),
  },
  window: {
    minimize: async () => {
      await liveCall("window.minimize");
    },
    toggleMaximize: () => liveCall("window.toggleMaximize"),
    close: async () => {
      await liveCall("window.close");
    },
    startMove: async () => {
      await liveCall("window.startMove");
    },
    startResize: async (edge) => {
      await liveCall("window.startResize", { edge });
    },
    isMaximized: () => liveCall("window.isMaximized"),
  },
};
