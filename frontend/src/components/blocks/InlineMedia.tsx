import { useCallback, useEffect, useRef, useState } from "react";
import { Play } from "lucide-react";
import { messageOf } from "../../lib/errors";
import { INLINE_IMAGE_DATA } from "../../lib/inlineMedia";
import { useStore } from "../../store";
import { MediaPreview, videoWidth, VIDEO_FRAME } from "../MediaPreview";

type Props = { sessionId: string; path: string; label: string };
const errorText = (message: string) => <span className="inline-media-error" role="status">{message}</span>;
const unreadable = "这张图片不能在正文中预览。";
let posterQueue = Promise.resolve();

function useVisibleMedia<T>(load: (node: HTMLSpanElement) => Promise<T>, serial = false) {
  const box = useRef<HTMLSpanElement>(null);
  const [result, setResult] = useState<{ data: T } | { error: string }>();
  useEffect(() => {
    const node = box.current;
    if (!node) return;
    let current = true, visible = false, queued = false, started = false;
    const request = async () => {
      queued = false;
      if (!current || !visible) return;
      started = true;
      watcher.disconnect();
      try {
        const data = await load(node);
        if (current) setResult({ data });
      } catch (error) {
        if (current) setResult({ error: messageOf(error) });
      }
    };
    const watcher = new IntersectionObserver((entries) => {
      visible = entries[entries.length - 1]?.isIntersecting ?? false;
      if (!visible || started || queued) return;
      queued = true;
      // Don't send a backlog of poster RPCs for items that could have scrolled away by their turn.
      if (serial) posterQueue = posterQueue.then(request);
      else void request();
    });
    watcher.observe(node);
    return () => { current = false; watcher.disconnect(); };
  }, [load, serial]);
  return { box, result, setResult };
}

export function InlinePicture({ sessionId, path, label }: Props) {
  const read = useStore((s) => s.inlineImage);
  const load = useCallback(async () => {
    const image = await read(sessionId, path);
    if (!INLINE_IMAGE_DATA.test(image.url) || (image.animation &&
      (!image.url.startsWith("data:image/png;base64,") || !image.animation.startsWith("data:image/gif;base64,")))) {
      throw new Error(unreadable);
    }
    return image;
  }, [read, sessionId, path]);
  const { box, result, setResult } = useVisibleMedia(load);
  const [started, setStarted] = useState(false);
  const image = result && "data" in result ? result.data : undefined;
  const picture = image && <img src={(started && image.animation) || image.url} alt={label} draggable={false}
    onError={() => setResult({ error: unreadable })} />;
  const action = `播放 GIF：${label}`;
  return <span className="inline-media inline-picture" ref={box} title={path}>
    {image?.animation ? <span className="inline-gif">
      {picture}
      {!started && <button type="button" className="inline-gif-toggle" aria-label={action} title={action} onClick={() => setStarted(true)}>
        <Play size={19} aria-hidden="true" />
      </button>}
    </span> : picture ?? <><span className="inline-media-label">{label}</span>{result && "error" in result && errorText(result.error)}</>}
  </span>;
}

export function InlineVideo({ sessionId, path, label }: Props) {
  const read = useStore((s) => s.inlineVideoPoster);
  const load = useCallback(async (node: HTMLSpanElement) => {
    const poster = await read(sessionId, path, videoWidth(node));
    if (!poster.frame.startsWith(VIDEO_FRAME)) throw new Error("这个视频不能在正文中预览。");
    return poster;
  }, [read, sessionId, path]);
  const { box, result } = useVisibleMedia(load, true);
  return <span className="inline-media inline-video" ref={box} title={path}>
    {result && "data" in result ? <MediaPreview sessionId={sessionId} fileRef={path} label={label} poster={result.data} renderError={errorText} /> :
      <><span className="inline-media-label">{label}</span>{result && "error" in result && errorText(result.error)}</>}
  </span>;
}
