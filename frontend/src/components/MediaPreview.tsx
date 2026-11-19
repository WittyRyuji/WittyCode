import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Pause, Play, RotateCcw, Volume2, VolumeX } from "lucide-react";
import { messageOf } from "../lib/errors";
import type { VideoCommand, VideoInfo, VideoState } from "../protocol";
import { useStore } from "../store";

let nextToken = 0;
export const VIDEO_FRAME = "data:image/jpeg;base64,";
type LocalCommand = VideoCommand extends infer V ? V extends VideoCommand ? Omit<V, "id" | "token"> : never : never;
type Lease = { token: string; unsubscribe: () => void };
const idle = (): VideoState => ({ position: 0, playing: false, ended: false });

export function videoWidth(node: HTMLElement | null): number {
  return Math.min(1280, Math.max(240, Math.round((node?.clientWidth ?? 640) * window.devicePixelRatio)));
}

function clock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  const mm = Math.floor(seconds / 60);
  const ss = String(seconds % 60).padStart(2, "0");
  return mm < 60 ? `${mm}:${ss}` : `${Math.floor(mm / 60)}:${String(mm % 60).padStart(2, "0")}:${ss}`;
}

export function MediaPreview({ sessionId, fileRef, size, label, poster, renderError }: {
  sessionId: string; fileRef: string; size?: string; label?: string; poster?: VideoInfo;
  renderError: (message: string) => ReactNode;
}) {
  const inline = !!poster;
  const fileVideo = useStore((s) => s.fileVideo);
  const watchVideo = useStore((s) => s.watchVideo);
  const area = useRef<HTMLElement>(null);
  const active = useRef<Lease | null>(null);
  const [opening, setOpening] = useState(false);
  const [released, setReleased] = useState(false);
  const [liveInfo, setInfo] = useState<VideoInfo | null>(null);
  const [state, setState] = useState<VideoState>(idle);
  const [volume, setVolume] = useState(0.7);
  const [muted, setMuted] = useState(false);
  const [scrub, setScrub] = useState<number | null>(null);
  const dragging = useRef(false);

  const retire = useCallback((close: boolean) => {
    const lease = active.current;
    active.current = null;
    if (!lease) return;
    lease.unsubscribe();
    if (close) void fileVideo({ action: "close", id: sessionId, token: lease.token }).catch(() => {});
  }, [fileVideo, sessionId]);

  const open = useCallback((first?: LocalCommand, audio?: { volume: number; muted: boolean }) => {
    if (active.current) return;
    setOpening(true);
    setReleased(false);
    setState(idle());
    const token = `video-${++nextToken}`;
    const reset = () => {
      dragging.current = false;
      setScrub(null);
      setInfo(null);
      setOpening(false);
    };
    const fail = (message: string) => {
      retire(true);
      reset();
      setState({ ...idle(), error: message });
    };
    const unsubscribe = watchVideo(token, (update) => {
      if (active.current?.token !== token) return;
      if (update.released) {
        retire(false);
        reset();
        setState(idle());
        if (!inline) {
          setVolume(0.7);
          setMuted(false);
          setReleased(true);
        }
        return;
      }
      const { position, playing, ended, url, error } = update;
      if (error) { fail(error); return; }
      setState((s) => ({ position, playing, ended, url: url?.startsWith(VIDEO_FRAME) ? url : s.url }));
    });
    active.current = { token, unsubscribe };
    const width = videoWidth(area.current);
    void fileVideo({ action: "open", id: sessionId, token, path: fileRef, width, ...(inline ? { inline } : {}) }).then(async (result) => {
      if (active.current?.token !== token) return;
      if (!result?.frame.startsWith(VIDEO_FRAME)) { fail("这个视频不能预览。"); return; }
      setInfo(result);
      setState((s) => ({ ...s, url: s.url ?? result.frame }));
      // A poster is not an open decoder. Apply this click's intent immediately after acquiring it.
      if (audio && result.hasAudio) await fileVideo({ action: "volume", id: sessionId, token, ...audio });
      if (active.current?.token !== token) return;
      if (first) await fileVideo({ ...first, id: sessionId, token });
      if (active.current?.token === token) setOpening(false);
    }).catch((error) => {
      if (active.current?.token === token) fail(messageOf(error));
    });
  }, [fileRef, fileVideo, inline, retire, sessionId, watchVideo]);

  useEffect(() => {
    if (!inline) open();
    return () => retire(true);
  }, [inline, open, retire]);

  const control = (command: LocalCommand) => {
    const token = active.current?.token;
    if (!token) return;
    void fileVideo({ ...command, id: sessionId, token }).catch((error) => {
      if (active.current?.token === token) setState((s) => ({ ...s, error: messageOf(error) }));
    });
  };
  const perform = (command: LocalCommand) => {
    if (opening) return;
    if (inline && !active.current) open(command, muted || volume !== 0.7 ? { muted, volume } : undefined);
    else control(command);
  };
  const toggle = () => perform({ action: state.playing ? "pause" : "play" });
  const seek = (position: number) => {
    setScrub(null);
    perform({ action: "seek", position });
  };
  const info = liveInfo ?? poster;
  const facts = [label ?? "视频", ...(info ? [`${info.width} × ${info.height}`, [info.videoCodec, info.audioCodec].filter(Boolean).join(" / "), clock(info.duration)] : []), size].filter(Boolean).join(" · ");
  const playbackLabel = state.playing ? "暂停" : state.ended ? "重播" : "播放";
  const Box = inline ? "span" : "div";
  return <Box ref={(node) => { area.current = node; }} className="fp-media">
    <span className="fp-facts">{facts}</span>
    {inline && state.error && renderError(state.error)}
    {released ? <span className="fp-state"><button type="button" className="btn small" onClick={() => open()}>重新加载视频</button></span> :
      !inline && state.error ? renderError(state.error) : !info ? <span className="fp-state"><Loader2 size={15} className="spin" /> 正在读取视频…</span> : <>
      <span className="fp-video-stage" tabIndex={0} aria-label="视频画面，空格播放或暂停" onKeyDown={(e) => {
        if (e.key === " " && !e.repeat) { e.preventDefault(); e.stopPropagation(); toggle(); }
      }}>
        <img src={state.url ?? info.frame} width={info.width} height={info.height} alt={label ?? "视频画面"} draggable={false} />
      </span>
      <span className="fp-video-controls" role="group" aria-label="视频播放控制">
        <button type="button" className="btn small fp-video-play" aria-label={playbackLabel} title={playbackLabel} aria-disabled={opening} onClick={toggle}>
          {opening ? <Loader2 size={15} className="spin" /> : state.playing ? <Pause size={15} /> : state.ended ? <RotateCcw size={15} /> : <Play size={15} />}
        </button>
        <input className="fp-video-time" aria-label="播放进度" aria-valuetext={`${clock(scrub ?? state.position)} / ${clock(info.duration)}`} type="range"
          min={0} max={info.duration || 1} step={1} value={Math.min(info.duration, scrub ?? state.position)} disabled={opening || !info.seekable || info.duration <= 0}
          onPointerDown={() => { dragging.current = true; setScrub(state.position); }}
          onChange={(e) => { const position = e.currentTarget.valueAsNumber; if (dragging.current) setScrub(position); else seek(position); }}
          onPointerUp={(e) => { if (dragging.current) { dragging.current = false; seek(e.currentTarget.valueAsNumber); } }}
          onPointerCancel={() => { dragging.current = false; setScrub(null); }}
          onBlur={() => { dragging.current = false; setScrub(null); }} />
        <span className="fp-video-clock">{clock(scrub ?? state.position)} / {clock(info.duration)}</span>
        {info.hasAudio && <>
          <button type="button" className="btn small fp-video-mute" disabled={opening} aria-label={muted ? "取消静音" : "静音"} title={muted ? "取消静音" : "静音"} onClick={() => {
            setMuted(!muted);
            control({ action: "volume", volume, muted: !muted });
          }}>{muted ? <VolumeX size={15} /> : <Volume2 size={15} />}</button>
          <input className="fp-video-volume" aria-label="音量" disabled={opening} type="range" min={0} max={1} step={0.05} value={volume} onChange={(e) => {
            const value = e.currentTarget.valueAsNumber;
            setVolume(value);
            control({ action: "volume", volume: value, muted });
          }} />
        </>}
      </span>
    </>}
  </Box>;
}
