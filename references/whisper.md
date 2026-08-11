# 无字幕时的 Whisper 兜底

## 同意与安装

检测到 `no_srt` 时，固定回复：

> 没有字幕可供下载。是否要使用本地 whisper-large-v3-turbo 将音频转成字幕后继续总结？如果尚未安装，我可以帮你安装。

多视频批次：在阶段 A 探测完成后、任何总结开始前，一次性列出全部 `no_srt` 并询问（见 `multi_video.md` 阶段 B）；未获回复前不得总结已有字幕的视频。用户确认后，Whisper 全批串行（同一时刻最多 1 个转写）；有字幕视频的并行总结见 `multi_video.md` 阶段 C，不在此重复。

没有明确“要 / 继续 / 使用 Whisper”的同意，不得下载音频、安装依赖或转写。用户拒绝则结束该视频。用户仅说没有模型时，先询问是否同意安装依赖和下载模型。

确认后，经 pipeline 下载**仅音频**到 scratchpad（禁止手写各平台下载命令）：

```bash
SCRATCH="${SCRATCHPAD:-.scratchpad}/video_summary"
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" download-audio \
  --dir . --scratchpad "$SCRATCH" --url "URL"
# 多视频批次加 --batch，以便更新 batch_state 并允许与总结重叠（B5）
```

读取 JSON：`status=ok` 时用 `audio_file` 与 `download_seconds`；`error` 时报告 `error`/`hint` 并停止。内部调用 `fetch_audio.py`：YouTube / Bilibili / 小红书 / X 走 yt-dlp；Apple Podcasts / 小宇宙走公开音频直链。小红书可能短暂拉取含画面的媒体再抽出音轨，属允许例外；不得为此保存视频到用户目录。ffmpeg 缺失时，在用户同意后按平台安装：macOS 可用 `brew install ffmpeg`；Windows 可用 `winget install Gyan.FFmpeg` 或从 [ffmpeg.org](https://ffmpeg.org/download.html) 安装并加入 PATH；也可用各平台包管理器等价方式。详见仓库 `README.md`。

## 后端选择（不可违反）

- **macOS Apple Silicon（`Darwin` + `arm64`）**：必须用 **MLX Whisper large-v3-turbo**（`mlx-community/whisper-large-v3-turbo`）。脚本 `--backend auto` 会自动选 `mlx`。
- **其他平台**（含 Intel Mac）：用 `faster-whisper` + `large-v3-turbo`。
- 不得在 Apple Silicon Mac 上默认走 CPU 版 faster-whisper。

检查依赖：

Apple Silicon Mac：

```bash
python3 -c 'import platform; assert platform.system()=="Darwin" and platform.machine()=="arm64"; import mlx_whisper; print("mlx-whisper 可用")'
```

模块缺失时，在用户同意后执行 `python3 -m pip install --user mlx-whisper`。

其他平台：

```bash
python3 -c 'from faster_whisper import WhisperModel; print("faster-whisper 可用")'
```

模块缺失时，在用户同意后执行 `python3 -m pip install --user faster-whisper`。

模型未缓存时说明首次初始化会下载对应权重，确认后才继续。

## 转写与进度

统一经 pipeline（内部 `--backend auto`：Apple Silicon → MLX，其余 → faster-whisper）。返回的 `whisper_seconds` 即为「语音转写用时」：

```bash
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" transcribe \
  --dir . --scratchpad "$SCRATCH" \
  --audio "$AUDIO_FILE" \
  --video-id "$VIDEO_ID" \
  --language "$ORIGINAL_LANGUAGE"
# 多视频批次加 --batch（会拒绝并行第二路 Whisper）
```

其中 `$AUDIO_FILE` / `$VIDEO_ID` 来自 `download-audio`（或先前 `probe` 的 `no_srt`）。原语言未知或不可靠时省略 `--language`。成功后直接读返回的 `transcript_file`（已 compact），再按 `subtitle_summary.md` 总结。成稿统一标注「语音转写（Whisper large-v3-turbo）」。

**B5：** 多视频时，下一条 `download-audio` 可与当前总结或当前 Whisper 重叠；全批仍最多 1 路 `transcribe`（见 `multi_video.md`）。

实时进度是**状态界面**而非不断追加的助手文本：运行命令的 PTY/终端状态行或可更新状态卡每 2 秒刷新一次，显示音频下载或转写的最新真实百分比、时间和状态。模型只在开始、阶段变化、完成或错误时发送一条对话消息。

- 每 2 秒读取一次 stdout；不得等待百分比阈值或正则匹配。
- 有百分比：`🎙️ Whisper 转写 [████████░░░░░░░░░░] 44% · 14:40 / 33:20`。
- 无百分比：`🎙️ Whisper：模型加载/下载中…`；不编造百分比或剩余时间。
- `complete` 才能开始压缩和总结；`error` 立即停止并报告真实错误。

音频与原始 SRT 均为临时文件，不保存到用户目录；转写失败时不以网页描述替代总结。
