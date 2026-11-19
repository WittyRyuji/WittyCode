# 对话正文里的本地图片和视频

助手可以在回复正文里写：

```markdown
![运行结果](outputs/result.png)
![动画演示](outputs/demo.gif)
![视频片段](outputs/clip.mp4)
[播放第二段](outputs/second.mp4)
```

回复文字块完成后，图片或视频进入可视区域才会读取。正文 GIF 先显示真正静止的首帧；用鼠标或键盘激活图片中央的播放按钮后，按钮消失，动画按 GIF 文件本身的循环设置播放。再点图片不会暂停、重置或恢复按钮。正文视频进入视口后显示静音取得的首帧和控制条，不自动播放；点一次播放（或聚焦画面按空格）就会播放，进度条可定位。

同屏可以同时显示多段视频的首帧。首帧按需串行提取，不会占用或中断交互播放器；同时只允许一段可交互的 Qt 视频。点击另一段或显式打开右侧文件面板的视频，会释放旧播放器，正文旧框保留并回到首帧。切离会话也会释放播放器；打开普通文本／图片不会抢占正文视频。代码里的路径（例如反引号包住的 `outputs/clip.mp4`）仍是原来的右侧面板链接；文件面板、计划 Markdown 和专家面板不自动加载正文媒体。

目标必须是所属会话项目内的相对路径。可用 `/` 或 `\` 分隔、用 `%20` 在 Markdown 中表示空格。自动预览不接受绝对路径（含盘符）、UNC、映射网络盘、`..`、符号链接、硬链接或 Windows 重解析点；支持 PNG/APNG、JPEG、GIF、WebP，以及 MP4/M4V/MOV/3GP 和 WebM/MKV。未支持的图片嵌入只显示说明文字；找不到文件、超限或无法解码会提示。普通文件链接仍按原先的点击规则打开。

- 正文图片最多 6 MiB、1600 万像素，同时最多读取两张。GIF 的快照由 Qt 读取首帧并编码为 PNG（最长边最多 2048 像素），点播放才把原 GIF 挂到页面上。PNG/APNG、JPEG 和 WebP 仍用原始字节。
- 正文视频最多 512 MiB。Qt 从已校验的本地文件描述符读取，海报返回后立即释放取帧解码器和句柄。网页只收到 JPEG 海报、限速的播放帧与状态，不接收整个视频。图片和帧不写入历史，不添加 CSP 例外，也不把 Markdown 中的 URL 交给浏览器自动请求。
- 右侧文件面板的动画、格式和大小规则不变；`.ts` 源文件不会被当成正文视频。龙图不使用正文媒体渲染器。

## 回归入口

非 GUI：`tests/test_app_inline_media.py`、`tests/test_app_video.py`、`tests/test_app_files.py`。

真实 Qt（需最新前端构建、隔离 pytest 运行目录及支持 MP4 的 Qt FFmpeg 后端）：

- `tests/test_app_inline_media_e2e.py::test_inline_images_and_missing_refs_use_the_message_project`
- `tests/test_app_inline_media_e2e.py::test_plan_markdown_does_not_auto_load_media`
- `tests/test_app_inline_media_e2e.py::test_gif_is_a_centered_still_until_started_then_removes_play_button`（覆盖鼠标和键盘启动后移除按钮、实绘两帧、再点图片不中断动画，以及原文件面板预览）
- `tests/test_app_inline_media_e2e.py::test_inline_mp4_plays_pauses_seeks_and_releases_its_decoder`（两段同时静止、一次点击播放、暂停、拖动、切换、释放）
- `tests/test_app_inline_media_e2e.py::test_offscreen_video_loads_on_scroll_without_interrupting_sidebar`（验证可见才取帧，不打断侧栏正在播放的视频）
- `tests/test_app_video_e2e.py::test_mp4_draws_and_unloads_when_switching_conversations`（原文件面板回归）

窗口用例在测试目录内生成 PNG/GIF 和 MP4，不需要下载素材或访问真实模型。
