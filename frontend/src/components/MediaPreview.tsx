import { useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Pause, Play, RotateCcw, Volume2, VolumeX } from "lucide-react";
import { messageOf } from "../lib/errors";
import type { VideoCommand, VideoInfo, VideoUpdate } from "../protocol";
import { useStore } from "../store";

let nextToken = 0;
const JPEG = "data:image/jpeg;base64,";
type LocalCommand = VideoCommand extends infer V ? V extends VideoCommand ? Omit<V, "id" | "token"> : never : never;

function clock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const mm = Math.floor(seconds / 60);
  const ss = String(seconds % 60).padStart(2, "0");
  return mm < 60 ? `${mm}:${ss}` : `${Math.floor(mm / 60)}:${String(mm % 60).padStart(2, "0")}:${ss}`;
}

export function MediaPreview({ sessionId, fileRef, size, renderError }: { sessionId: string; fileRef: string; size: string; renderError: (message: string) => ReactNode }) {
  const fileVideo = useStore((s) => s.fileVideo);
  const watchVideo = useStore((s) => s.watchVideo);
  const area = useRef<HTMLDivElement>(null);
  const active = useRef<string | null>(null);
  const [info, setInfo] = useState<VideoInfo | null>(null);
  const [state, setState] = useState<Omit<VideoUpdate, "token">>({ position: 0, playing: false, ended: false });
  const [volume, setVolume] = useState(0.7);
  const [muted, setMuted] = useState(false);
  const [scrub, setScrub] = useState<number | null>(null);
  const dragging = useRef(false);

  useEffect(() => {
    const token = `video-${++nextToken}`;
    active.current = token;
    const unsubscribe = watchVideo(token, ({ position, playing, ended, url, error }) => {
      if (active.current !== token) return;
      setState((s) => ({ position, playing, ended, url: url?.startsWith(JPEG) ? url : s.url, error }));
    });
    const width = Math.min(1280, Math.max(240, Math.round((area.current?.clientWidth ?? 640) * window.devicePixelRatio)));
    void fileVideo({ action: "open", id: sessionId, token, path: fileRef, width }).then((result) => {
      if (active.current !== token) return;
      if (result?.frame.startsWith(JPEG)) {
        setInfo(result);
        setState((s) => ({ ...s, url: s.url ?? result.frame }));
      } else setState((s) => ({ ...s, error: "这个视频不能在侧栏里预览：用默认程序打开。" }));
    }, (error) => {
      if (active.current === token) setState((s) => ({ ...s, error: messageOf(error) }));
    });
    return () => {
      active.current = null;
      unsubscribe();
      void fileVideo({ action: "close", id: sessionId, token }).catch(() => {});
    };
  }, [fileRef, fileVideo, sessionId, watchVideo]);

  const control = (command: LocalCommand) => {
    const token = active.current;
    if (!token) return;
    void fileVideo({ ...command, id: sessionId, token }).catch((error) => {
      if (active.current === token) setState((s) => ({ ...s, error: messageOf(error) }));
    });
  };
  const toggle = () => control({ action: state.playing ? "pause" : "play" });
  const seek = (position: number) => {
    setScrub(null);
    control({ action: "seek", position });
  };
  const facts = ["视频", ...(info ? [`${info.width} × ${info.height}`, [info.videoCodec, info.audioCodec].filter(Boolean).join(" / "), clock(info.duration)] : []), size].filter(Boolean).join(" · ");
  const playbackLabel = state.playing ? "暂停" : state.ended ? "重播" : "播放";
  return <div ref={area} className="fp-media">
    <div className="fp-facts">{facts}</div>
    {state.error ? renderError(state.error) : !info ? <div className="fp-state"><Loader2 size={15} className="spin" /> 正在读取视频…</div> : <>
      <div className="fp-video-stage" tabIndex={0} aria-label="视频画面，空格播放或暂停" onKeyDown={(e) => {
        if (e.key === " " && !e.repeat) { e.preventDefault(); e.stopPropagation(); toggle(); }
      }}>
        <img src={state.url ?? info.frame} width={info.width} height={info.height} alt="视频画面" draggable={false} />
      </div>
      <div className="fp-video-controls" aria-label="视频播放控制">
        <button type="button" className="btn small fp-video-play" aria-label={playbackLabel} title={playbackLabel} onClick={toggle}>
          {state.playing ? <Pause size={15} /> : state.ended ? <RotateCcw size={15} /> : <Play size={15} />}
        </button>
        <input className="fp-video-time" aria-label="播放进度" aria-valuetext={`${clock(scrub ?? state.position)} / ${clock(info.duration)}`} type="range"
          min={0} max={info.duration || 1} step={1} value={Math.min(info.duration, scrub ?? state.position)} disabled={!info.seekable || info.duration <= 0}
          onPointerDown={() => { dragging.current = true; setScrub(state.position); }}
          onChange={(e) => { const position = e.currentTarget.valueAsNumber; if (dragging.current) setScrub(position); else seek(position); }}
          onPointerUp={(e) => { if (dragging.current) { dragging.current = false; seek(e.currentTarget.valueAsNumber); } }}
          onPointerCancel={() => { dragging.current = false; setScrub(null); }}
          onBlur={() => { dragging.current = false; setScrub(null); }} />
        <span className="fp-video-clock">{clock(scrub ?? state.position)} / {clock(info.duration)}</span>
        {info.hasAudio && <>
          <button type="button" className="btn small fp-video-mute" aria-label={muted ? "取消静音" : "静音"} title={muted ? "取消静音" : "静音"} onClick={() => {
            setMuted(!muted);
            control({ action: "volume", volume, muted: !muted });
          }}>{muted ? <VolumeX size={15} /> : <Volume2 size={15} />}</button>
          <input className="fp-video-volume" aria-label="音量" type="range" min={0} max={1} step={0.05} value={volume} onChange={(e) => {
            const value = e.currentTarget.valueAsNumber;
            setVolume(value);
            control({ action: "volume", volume: value, muted });
          }} />
        </>}
      </div>
    </>}
  </div>;
}
