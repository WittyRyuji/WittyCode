import type { CSSProperties } from "react";
import { AGENT_STATE } from "../lib/agentTeam";
import { DRAGON_FPS, DRAGON_SIZE, dragonColours, dragonFrames, type DragonIdentity, type DragonPalette, type DragonStatus } from "../lib/dragonSprites";

const sheets = new Map<string, { url: string; count: number }>();

function sheetOf(identity: DragonIdentity, palette: DragonPalette, status: DragonStatus) {
  const key = `${identity}:${status === "stopped" ? "grey" : palette}:${status}`;
  const cached = sheets.get(key);
  if (cached) return cached;
  const frames = dragonFrames(identity, status);
  const canvas = document.createElement("canvas");
  canvas.width = DRAGON_SIZE * frames.length;
  canvas.height = DRAGON_SIZE;
  const context = canvas.getContext("2d");
  if (!context) return null;
  const colours = dragonColours(palette, status);
  frames.forEach((frame, i) => frame.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      const pixel = row[x];
      if (pixel === ".") continue;
      context.fillStyle = colours[pixel as keyof typeof colours];
      context.fillRect(i * DRAGON_SIZE + x, y, 1, 1);
    }
  }));
  const sheet = { url: canvas.toDataURL(), count: frames.length };
  sheets.set(key, sheet);
  return sheet;
}

export function DragonSprite({ identity, palette, status, scale }: { identity: DragonIdentity; palette: DragonPalette; status: DragonStatus; scale: 2 | 3 }) {
  const sheet = sheetOf(identity, palette, status);
  const count = sheet?.count ?? 1;
  const size = DRAGON_SIZE * scale;
  const style = {
    "--dragon-size": `${size}px`,
    "--dragon-strip": `${size * count}px`,
    "--dragon-frames": count,
    "--dragon-time": `${count / DRAGON_FPS}s`,
    backgroundImage: sheet ? `url(${sheet.url})` : undefined,
  } as CSSProperties;
  return <span className={`dragon-sprite${count > 1 ? " playing" : ""}`} style={style} role="img" aria-label={AGENT_STATE[status].text} />;
}
