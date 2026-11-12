// How the live page talks to Python. Python registers one QObject named `backend` on a QWebChannel and injects Qt's
// qwebchannel.js before the page runs (so `QWebChannel` is a global):
//   slot   call(method, paramsJson, callId)       page -> Python, no return value
//   signal reply(callId, ok, payloadJson)         the answer to a call; ok=false: payloadJson is the user-facing message
//   signal event(name, payloadJson)               Python -> page, whenever something changes
// Everything is JSON. The design mock does not use a transport at all (mock/bridge.ts, mock/simulator.ts).

export interface Transport {
  call<T>(method: string, params?: object): Promise<T>;
  onEvent(cb: (name: string, payload: unknown) => void): void;
}

/** A call Python refused or that never came back: `message` is the sentence to show. */
export class CallError extends Error {
  override name = "CallError";
}

export const CALL_TIMEOUT_MS = 120_000;

/** Calls that wait for the user (the browser sign-in, the native file dialogs) have no safety timeout. */
const WAITS_FOR_USER = new Set(["devin.login", "chatgpt.login", "project.add", "attachments.pick"]);

interface Signal<A extends unknown[]> {
  connect(callback: (...args: A) => void): void;
}

/** The proxy qwebchannel.js builds for Python's `backend` object. A trailing function argument is qwebchannel's own
 *  completion callback: passing one keeps it from creating a promise that would reject for a slot without a result. */
interface BackendObject {
  call(method: string, paramsJson: string, callId: string, done: () => void): void;
  reply: Signal<[callId: string, ok: boolean, payloadJson: string]>;
  event: Signal<[name: string, payloadJson: string]>;
}

type ChannelConstructor = new (transport: unknown, ready: (channel: { objects: Record<string, unknown> }) => void) => unknown;

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
  timer?: ReturnType<typeof setTimeout>;
}

const parse = (json: string): unknown => {
  if (!json) return null;
  try {
    return JSON.parse(json);
  } catch {
    return json;
  }
};

export class QWebChannelTransport implements Transport {
  private readonly backend: Promise<BackendObject>;
  private readonly pending = new Map<string, Pending>();
  private readonly listeners: ((name: string, payload: unknown) => void)[] = [];
  private sequence = 0;

  constructor(
    transport: unknown = window.qt?.webChannelTransport,
    Channel: ChannelConstructor | undefined = (globalThis as { QWebChannel?: ChannelConstructor }).QWebChannel,
  ) {
    if (!transport) throw new CallError("没有连接到 WittyCode 后台：页面里没有 qt.webChannelTransport。");
    if (typeof Channel !== "function") throw new CallError("没有连接到 WittyCode 后台：页面里没有 QWebChannel（qwebchannel.js 没有注入）。");
    this.backend = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new CallError("连接 WittyCode 后台超时。")), CALL_TIMEOUT_MS);
      new Channel(transport, (channel) => {
        clearTimeout(timer);
        const backend = channel.objects.backend as BackendObject | undefined;
        if (!backend) return reject(new CallError("没有连接到 WittyCode 后台：QWebChannel 里没有 backend 对象。"));
        backend.reply.connect((callId, ok, payloadJson) => this.settle(callId, ok, payloadJson));
        backend.event.connect((name, payloadJson) => this.dispatch(name, payloadJson));
        resolve(backend);
      });
    });
  }

  async call<T>(method: string, params: object = {}): Promise<T> {
    const backend = await this.backend;
    const callId = String(++this.sequence);
    return new Promise<T>((resolve, reject) => {
      const entry: Pending = { resolve: resolve as (value: unknown) => void, reject };
      if (!WAITS_FOR_USER.has(method)) {
        entry.timer = setTimeout(() => {
          this.pending.delete(callId);
          reject(new CallError(`WittyCode 后台 ${CALL_TIMEOUT_MS / 1000} 秒没有回应（${method}）。`));
        }, CALL_TIMEOUT_MS);
      }
      this.pending.set(callId, entry);
      try {
        backend.call(method, JSON.stringify(params), callId, () => {});
      } catch (error) {
        clearTimeout(entry.timer);
        this.pending.delete(callId);
        reject(error instanceof Error ? error : new CallError(String(error)));
      }
    });
  }

  onEvent(cb: (name: string, payload: unknown) => void): void {
    this.listeners.push(cb);
  }

  private settle(callId: string, ok: boolean, payloadJson: string): void {
    const entry = this.pending.get(callId);
    if (!entry) return; // timed out already
    this.pending.delete(callId);
    clearTimeout(entry.timer);
    const payload = parse(payloadJson);
    if (ok) entry.resolve(payload);
    else entry.reject(new CallError(typeof payload === "string" && payload ? payload : "操作失败。"));
  }

  private dispatch(name: string, payloadJson: string): void {
    const payload = parse(payloadJson);
    for (const listener of this.listeners) {
      try {
        listener(name, payload);
      } catch (error) {
        // One event the page cannot handle must not stop the ones after it.
        console.error(`handling the "${name}" event failed`, error);
      }
    }
  }
}
