// The one place that decides which Bridge the page talks to: Python through QWebChannel in the live app, the mock in the
// design review (mode.ts decides before the first render).
import type { Bridge } from "./bridge";
import { liveBridge } from "./liveBridge";
import { mockBridge } from "./mock/bridge";
import { LIVE } from "./mode";

export const bridge: Bridge = LIVE ? liveBridge : mockBridge;
