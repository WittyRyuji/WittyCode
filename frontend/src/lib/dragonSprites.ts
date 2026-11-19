import type { AgentInfo } from "../types";

export type DragonIdentity = "main" | "worker" | "light";
export type DragonStatus = AgentInfo["status"];
export const DRAGON_SIZE = 16;
export const DRAGON_FPS = 6;

type Pose = "stand" | "bob";
function faceLeft({ stand, bob }: Record<Pose, readonly string[]>) {
  const flip = (rows: readonly string[]) => rows.map((row) => [...row].reverse().join(""));
  return { stand: flip(stand), bob: flip(bob) };
}

const workerStand = [
  "...KK...........",
  "...KLK..KKKKK...",
  "....KLKKBBBBBK..",
  ".....KLBBBBBBBK.",
  ".....KBBBLLKBBK.",
  ".....KBBBLLKBBBK",
  ".....KBBBBBBBBBK",
  ".....KBBBBKKKKK.",
  ".....KKBBBBBBK..",
  "....KBBBLLLKK...",
  ".K.KBBBBLLLKK...",
  "KBKKBBBBLLLBK...",
  "KBBKBBBBLLLKK...",
  ".KBBBBBBLLLK....",
  "..KKKBBKKBBK....",
  "....KKKK.KKKK...",
];
const workerBob = [
  "................",
  "...KK...........",
  "...KLK..KKKKK...",
  "....KLKKBBBBBK..",
  ".....KLBBBBBBBK.",
  ".....KBBBLLKBBK.",
  ".....KBBBLLKBBBK",
  ".....KBBBBBBBBBK",
  ".....KBBBBKKKKK.",
  "....KKKBBBBBBK..",
  ".K.KBBBBLLLKK...",
  "KBKKBBBBLLLBK...",
  "KBBKBBBBLLLKK...",
  ".KBBBBBBLLLK....",
  "..KKKBBKKBBK....",
  "....KKKK.KKKK...",
];
export const DRAGON_SHAPES = {
  worker: faceLeft({ stand: workerStand, bob: workerBob }),
  main: faceLeft({
    stand: ["...KK...L.L.L...", "...KLK..LLLLL...", ...workerStand.slice(2)],
    bob: [workerBob[0], "...KK...L.L.L...", "...KLK..LLLLL...", ...workerBob.slice(3)],
  }),
  light: faceLeft({
    stand: [
      "................",
      "........KKKKK...",
      "......KKBBBBBK..",
      "......KBBBBBBBK.",
      ".....KBBBLLKBBK.",
      ".....KBBBLLKBBBK",
      ".....KBBBBBBBBBK",
      ".....KBBBBKKKKK.",
      ".....KKBBBBBBK..",
      "....KBBBLLLKK...",
      "..K.KBBBLLLK.K..",
      ".KLKKBBBLLLKKLK.",
      ".KLLLKKKKKKLLLK.",
      ".KLLLLLLLLLLLLK.",
      "..KLLLLLLLLLLK..",
      "...KKKKKKKKKK...",
    ],
    bob: [
      "................",
      "................",
      "........KKKKK...",
      "......KKBBBBBK..",
      "......KBBBBBBBK.",
      ".....KBBBLLKBBK.",
      ".....KBBBLLKBBBK",
      ".....KBBBBBBBBBK",
      ".....KBBBBKKKKK.",
      ".....KKBBBBBBK..",
      "..K.KBBBLLLK.K..",
      ".KLKKBBBLLLKKLK.",
      ".KLLLKKKKKKLLLK.",
      ".KLLLLLLLLLLLLK.",
      "..KLLLLLLLLLLK..",
      "...KKKKKKKKKK...",
    ],
  }),
};

const DRAGON_PALETTES = {
  main: { B: "#D82800", L: "#F8B800" },
  green: { B: "#00A844", L: "#F8D878" },
  purple: { B: "#6844FC", L: "#F8B8F8" },
  orange: { B: "#E45C10", L: "#F8D878" },
  teal: { B: "#008888", L: "#B8F8D8" },
  blue: { B: "#3CBCFC", L: "#FCFCFC" },
  pink: { B: "#F878F8", L: "#FCFCFC" },
  grey: { B: "#7C7C7C", L: "#BCBCBC" },
} as const;
export type DragonPalette = keyof typeof DRAGON_PALETTES;
const ROLE_PALETTES = { main: ["main"], worker: ["green", "purple", "orange", "teal"], light: ["blue", "pink"] } as const;

export function dragonPalette(identity: DragonIdentity, id: string): DragonPalette {
  // FNV-1a keeps an expert's colour independent of its position or state.
  let hash = 0x811c9dc5;
  for (let i = 0; i < id.length; i++) hash = Math.imul(hash ^ id.charCodeAt(i), 0x01000193);
  const palettes = ROLE_PALETTES[identity];
  return palettes[(hash >>> 0) % palettes.length];
}

export function dragonIdentity(role: AgentInfo["fusionRole"]): DragonIdentity {
  return role === "light" ? "light" : "worker";
}

export function dragonColours(palette: DragonPalette, status: DragonStatus) {
  return {
    K: "#101018",
    ...DRAGON_PALETTES[status === "stopped" ? "grey" : palette],
    A: status === "done" ? "#3CBCFC" : "#F83800",
  } as const;
}

const EYES = { closed: ["BBB", "KKK"], hurt: ["KBK", "BKB"] } as const;
const GLYPHS = {
  alert: { top: 2, rows: ["AA", "AA", "AA", "AA", "..", "AA"] },
  sleepLow: { top: 5, rows: ["AAA", ".A.", "AAA"] },
  sleepHigh: { top: 2, rows: ["AAA", ".A.", "AAA"] },
  fail: { top: 4, rows: ["A.A", ".A.", "A.A"] },
} as const;

type Shot = [pose: Pose, eye: "open" | keyof typeof EYES, ticks?: number, glyph?: keyof typeof GLYPHS];
const ANIMATIONS: Record<DragonStatus, Shot[]> = {
  pending: [["stand", "open", 5], ["stand", "closed"]],
  working: [["stand", "open"], ["bob", "open"], ["stand", "open"], ["bob", "open"], ["stand", "open"], ["bob", "closed"]],
  waiting: [["stand", "open", 3, "alert"], ["bob", "open", 3]],
  done: [["stand", "closed", 4, "sleepLow"], ["bob", "closed", 4, "sleepHigh"]],
  stopped: [["stand", "closed"]],
  failed: [["stand", "hurt", 1, "fail"]],
};

/** Composed pixel rows in playback order, with repeated frames already expanded to individual ticks. */
export function dragonFrames(identity: DragonIdentity, status: DragonStatus): readonly (readonly string[])[] {
  return ANIMATIONS[status].flatMap(([pose, eye, ticks = 1, glyph]) => {
    const frame: string[] = [...DRAGON_SHAPES[identity][pose]];
    if (eye !== "open") EYES[eye].forEach((pixels, row) => {
      const y = (pose === "stand" ? 4 : 5) + row;
      frame[y] = frame[y].slice(0, 4) + pixels + frame[y].slice(7);
    });
    if (glyph) {
      const { top, rows } = GLYPHS[glyph];
      rows.forEach((pixels, row) => {
        const y = top + row;
        // The glyph keeps its own orientation; only its position moves to the right.
        const right = DRAGON_SIZE - pixels.length;
        const rowPixels = [...frame[y]];
        [...pixels].forEach((pixel, x) => { if (pixel === "A") rowPixels[right + x] = pixel; });
        frame[y] = rowPixels.join("");
      });
    }
    return Array.from({ length: ticks }, () => frame);
  });
}
