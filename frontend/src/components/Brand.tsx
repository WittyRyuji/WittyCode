import iconUrl from "../assets/brand/icon-256.png";
import { DRAGON_PATH, DRAGON_VIEWBOX, WORD_BOTTOM_PATH, WORD_TOP_PATH } from "../assets/brand/paths";

/** The square mark (LOGO_ICON.png): the app icon. Always black with a white frame, on both themes. */
export function AppIcon({ size = 24, className }: { size?: number; className?: string }) {
  return <img className={`app-icon${className ? ` ${className}` : ""}`} src={iconUrl} width={size} height={size} alt="WittyCode" draggable={false} />;
}

/** The dragon alone; takes the colour of the text around it. */
export function DragonMark({ size = 24, className }: { size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox={DRAGON_VIEWBOX} fill="currentColor" role="img" aria-label="WITTY LOONG">
      <path d={DRAGON_PATH} />
    </svg>
  );
}

/** "WITTY / LOONG" in the logo's own lettering. */
export function Wordmark({ height = 28, className }: { height?: number; className?: string }) {
  return (
    <svg className={className} height={height} viewBox="0 0 94 49" fill="currentColor" role="img" aria-label="WITTY LOONG">
      <path d={WORD_TOP_PATH} transform="translate(-0.08 22.23)" />
      <g transform="translate(0 25.85) scale(1.02636)">
        <path d={WORD_BOTTOM_PATH} transform="translate(-0.68 21.86)" />
      </g>
    </svg>
  );
}

/** The framed vertical logo: dragon on top, wordmark underneath. */
export function LogoVertical({ width = 150, className }: { width?: number; className?: string }) {
  return (
    <svg className={className} width={width} viewBox="-1 -1 112 178.27" fill="currentColor" role="img" aria-label="WITTY LOONG">
      <rect x="0" y="0" width="110" height="176.27" fill="none" stroke="currentColor" strokeWidth="2" />
      <rect x="0" y="117.53" width="110" height="2" />
      <svg x="5" y="5" width="100" height="107.53" viewBox={DRAGON_VIEWBOX}>
        <path d={DRAGON_PATH} />
      </svg>
      <g transform="translate(8.08 122.53)">
        <path d={WORD_TOP_PATH} transform="translate(-0.08 22.23)" />
        <g transform="translate(0 25.85) scale(1.02636)">
          <path d={WORD_BOTTOM_PATH} transform="translate(-0.68 21.86)" />
        </g>
      </g>
    </svg>
  );
}
