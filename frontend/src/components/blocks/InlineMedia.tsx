import { useCallback, useEffect, useRef, useState } from "react";
import { Play, X } from "lucide-react";
import { messageOf } from "../../lib/errors";
import { INLINE_IMAGE_DATA } from "../../lib/inlineMedia";
import { useStore } from "../../store";
import { MediaPreview } from "../MediaPreview";

type Props = { sessionId: string; path: string; label: string };
const errorText = (message: string) => <span className="inline-media-error" role="status">{message}</span>;
const unreadable = "这张图片不能在正文中预览。";

export function InlinePicture({ sessionId, path, label }: Props) {
  const read = useStore((s) => s.inlineImage);
  const box = useRef<HTMLSpanElement>(null);
  const [result, setResult] = useState<{ url: string } | { error: string }>();
  useEffect(() => {
    const node = box.current;
    if (!node) return;
    let current = true;
    let requested = false;
    const watcher = new IntersectionObserver((entries) => {
      if (!current || requested || !entries.some((e) => e.isIntersecting)) return;
      requested = true;
      watcher.disconnect();
      void read(sessionId, path).then((url) => {
        if (current) setResult(INLINE_IMAGE_DATA.test(url) ? { url } : { error: unreadable });
      }, (error) => { if (current) setResult({ error: messageOf(error) }); });
    });
    watcher.observe(node);
    return () => { current = false; watcher.disconnect(); };
  }, [path, read, sessionId]);
  return <span className="inline-media inline-picture" ref={box} title={path}>
    {result && "url" in result ? <img src={result.url} alt={label} draggable={false} onError={() => setResult({ error: unreadable })} /> :
      <><span className="inline-media-label">{label}</span>{result && errorText(result.error)}</>}
  </span>;
}

export function InlineVideo({ sessionId, path, label }: Props) {
  const [active, setActive] = useState(false);
  const release = useCallback(() => setActive(false), []);
  return <span className="inline-media inline-video" title={path}>
    <button type="button" className="btn small inline-video-activate" aria-expanded={active}
      aria-label={`${active ? "关闭视频" : "加载视频"}：${label}`} onClick={() => setActive(!active)}>
      {active ? <X size={14} /> : <Play size={14} />}{active ? "关闭视频" : label}
    </button>
    {active && <MediaPreview sessionId={sessionId} fileRef={path} inline renderError={errorText} onRelease={release} />}
  </span>;
}
