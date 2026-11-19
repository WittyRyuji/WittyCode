# 对话正文里的本地图片和视频

助手可以在回复正文里写：

```markdown
![运行结果](outputs/result.png)
![动画演示](outputs/demo.gif)
![视频片段](outputs/clip.mp4)
[播放第二段](outputs/second.mp4)
```

回复文字块完成后，滚到图片所在位置才会读取图片。视频先显示加载按钮；点击会在正文展开并停在首帧，再用播放／暂停按钮、空格或进度条控制。同一窗口只保留一个 Qt 解码器：换到另一段（包括右侧文件面板里的视频）、收起播放器或切离该会话，都会释放旧视频。打开普通文本／图片不会抢占正文视频。代码里的路径（例如反引号包住的 `outputs/clip.mp4`）仍是原来的右侧面板链接；文件面板、计划 Markdown 和专家面板不自动加载正文媒体。

目标必须是所属会话项目内的相对路径。可用 `/` 或 `\` 分隔、用 `%20` 在 Markdown 中表示空格。自动预览不接受绝对路径（含盘符）、UNC、映射网络盘、`..`、符号链接、硬链接或 Windows 重解析点；支持 PNG/APNG、JPEG、GIF、WebP，以及 MP4/M4V/MOV/3GP 和 WebM/MKV。未支持的图片嵌入只显示说明文字；找不到文件、超限或无法解码会提示。普通文件链接仍按原先的点击规则打开。

- 正文图片最多 6 MiB、1600 万像素，同时最多读取两张；原始 GIF/APNG/WebP 字节不重编码。正文视频最多 512 MiB，Qt 读取已验证的本地文件描述符，不把整个视频送进网页。网页只接收限速的 JPEG 帧与播放状态。图片和帧不写入历史，不添加 CSP 例外，也不把 Markdown 中的 URL 交给浏览器自动请求。
- 右侧文件面板仍使用原来的格式和大小规则；`.ts` 源文件不会被当成正文视频。

## 回归入口

非 GUI：`tests/test_app_inline_media.py`、`tests/test_app_video.py`、`tests/test_app_files.py`。

真实 Qt（需最新前端构建、隔离 pytest 运行目录及支持 MP4 的 Qt FFmpeg 后端）：

- `tests/test_app_inline_media_e2e.py::test_inline_images_and_missing_refs_use_the_message_project`
- `tests/test_app_inline_media_e2e.py::test_plan_markdown_does_not_auto_load_media`
- `tests/test_app_inline_media_e2e.py::test_inline_mp4_plays_pauses_seeks_and_releases_its_decoder`
- `tests/test_app_video_e2e.py::test_mp4_draws_and_unloads_when_switching_conversations`（原文件面板回归）

窗口用例在测试目录内生成 PNG/GIF 和 MP4，不需要下载素材或访问真实模型。
