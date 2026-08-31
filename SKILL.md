---
name: video-summary
description: 给定一个或多个 YouTube / Bilibili / 小红书 / X（Twitter）/ Apple Podcasts / 小宇宙 / 长桥直播（Longbridge lives）链接，优先下载字幕（原语言优先，否则任意可用语言）并生成带时间戳的中文总结、保存为 Markdown；无字幕时先征得用户同意，再下载音频并以本地 whisper-large-v3-turbo 转写（macOS Apple Silicon 用 MLX，其他平台用 faster-whisper）。当用户要求总结、摘要、讲解或查看上述平台视频/播客时使用。
metadata:
  version: "2026.09.01"
---

# video_summary — 视频字幕总结

安装、系统要求、依赖与隐私说明见 [`README.md`](./README.md)（给人看；Agent 执行时不必通读）。

## 目的

生成有可跳转时间戳、可复用的中文视频/播客总结，同时避免不必要的 token、下载和重复工作。

本 skill 同时供 **Claude Code / Codex / Cursor / OpenCode** 使用；运行统计必须按当前 agent 分支执行，禁止混用其他环境的脚本。

**分工：** `scripts/summarize_pipeline.py` 负责去重索引、字幕探测、音频下载、Whisper、注册索引与 HTML 重建；agent 只读压缩转写稿并写中文总结（含运行统计回填）。详见 `references/pipeline.md`。

## 支持平台

| 平台 | 链接示例 | 字幕 | 音频 / Whisper |
|---|---|---|---|
| YouTube | `youtube.com/watch`、`youtu.be`、`shorts` | 优先 | 无字幕时 |
| Bilibili | `bilibili.com/video/BV…`、`b23.tv` | 优先 | 无字幕时 |
| 小红书 | `xiaohongshu.com/explore/…`（**须含 `xsec_token`**） | 通常无 | Cookie + yt-dlp |
| X（Twitter） | `x.com/<user>/status/<id>`、`twitter.com/…/status/<id>` | 通常无 | yt-dlp（受限内容需 Cookie） |
| Apple Podcasts | `podcasts.apple.com/…/id…?i=…`（**须含单集 `i=`**） | 通常无 | iTunes/RSS 直链 |
| 小宇宙 | `xiaoyuzhoufm.com/episode/…` | 通常无 | 页面公开音频直链 |
| 长桥直播 | `longbridge.com`／`longbridge.cn/…/lives/<id>` | 平台逐字稿 | 不支持（无字幕即失败） |

**YouTube 频道/播放列表（非单视频链接，如 `youtube.com/@handle`）：** 不直接总结；用 pipeline `list-videos` 拉取最新 10 条（标题、时长、链接）以表格反馈给用户挑选，用户选定后再按单条/多条流程处理（见 `pipeline.md`）。  
**小红书：** 短链（无 `xsec_token`）常触发风控 `300031`；请用户从浏览器地址栏复制完整链接，并确保 Chrome 已登录。  
**X（Twitter）：** 链接须含 `/status/<数字 id>`；平台无字幕轨，走 Whisper 流程（先征得同意）。去重与正文链接一律用 URL 的 status id（yt-dlp 返回的媒体 id 不同，脚本已统一覆盖）。公开推文无需登录；受限/敏感内容自动回退浏览器 Cookie。  
**Apple Podcasts：** 只要播客主页、没有 `?i=` 单集 ID 时拒绝并说明。  
**长桥直播：** 走 Longbridge 公开 REST 取平台逐字稿（优先 zh-CN、空则回退 en），无需登录、无需 yt-dlp；平台标为自动生成字幕。字幕接口只保留最近约 3 场，更早或仍在直播/生成中的场次会返回「暂无字幕」错误，不进入 Whisper 流程。  
**不支持 Spotify**（DRM），勿尝试绕过。

## 按需读取参考

本文件是路由；不要无条件读取全部参考文档。

| 情况 | 必须读取 |
|---|---|
| 所有条目 | `references/pipeline.md`、`references/subtitle_summary.md` |
| 无可用字幕且用户同意转写 | `references/whisper.md` |
| 单条或多条协调者的运行统计 | `references/runtime_statistics.md`，再读当前 agent 专页（见下） |
| 两个及以上链接 | `references/multi_video.md` |
| output/ 有新增或更新的 Markdown（需重建 HTML 汇总页） | `references/html_viewer.md` |

### 运行统计：按 Agent 分读

先读 `runtime_statistics.md` 识别环境，再**只读**对应专页：

| Agent | 专页 | Token / LLM 速度脚本 |
|---|---|---|
| Cursor | `references/runtime_cursor.md` | `scripts/cursor_usage.py` |
| Codex | `references/runtime_codex.md` | `scripts/codex_usage.py` |
| OpenCode | `references/runtime_opencode.md` | `scripts/opencode_usage.py` |
| Claude Code | `references/runtime_claude.md` | `scripts/claude_usage.py` |

多视频工作任务读取本文件、`pipeline.md`、`subtitle_summary.md`、`runtime_statistics.md` 与当前 agent 专页；每个工作 agent 只为自己负责的视频建 baseline 并回填统计。除非协调者在获得用户许可后明确分派 Whisper，不读 `whisper.md`。

## 不可违反的规则

1. 只支持上表平台。字幕/音频/转写/去重/HTML **必须**经 `summarize_pipeline.py`（见 `pipeline.md`）；禁止手写 `yt-dlp --list-subs` / `--print`、直接读 `.srt/.vtt`、自造各平台下载逻辑。探测阶段不得为「只要字幕」而下载视频；播客/小红书/X 无字幕经用户同意后由 pipeline 下载**仅音频**。
2. 字幕选型不限定语言：优先原语言人工字幕 > 原语言自动字幕 > 任意语言人工字幕 > 任意语言自动字幕（由 `fetch_video.py` 执行；忽略 B 站 `danmaku`）；总结正文不要把整份字幕丢进模型。非中文主轨时可由 `fetch_video.py` 附带平台已有的中文对照轨；无平台中文轨时默认直接交付，不询问是否翻译，也不调用 LLM 翻译。只有用户明确要求翻译字幕时，才按 `subtitle_summary.md` 使用 `translate_srt.py` 逐条翻译。完全无可用字幕轨时必须先询问用户，用户同意 Whisper 后才可 `download-audio` / `transcribe`。多视频时：先 `batch-probe`；只要有待确认的 `no_srt`，必须先问清 Whisper 意向，再总结任何条目。用户确认后：无须语音转文字的条目可同时派多个子 agent 并行总结——**Cursor CLI 除外**，Cursor 不记录内置子 agent 的 token，须在当前 turn 内逐条串行并**自动继续到整批完成，不得要求用户逐条回复「继续」**；每条正文**必须**优先尝试嵌套 `cursor-agent -p --output-format stream-json` worker 生成并用 `--finalize-from-result` 回填**单条实测** Token / LLM 速度（见 `runtime_cursor.md`）；仅当 `cursor-agent` 不存在或 worker 失败时才回退协调者自行串行总结，该批各条 Token / LLM 速度由脚本写入**批次实测值**并标注「本批 N 条共用」，不得去掉标注冒充单条实测。**OpenCode 也不派子 agent 总结**（子 agent 用量落在独立子会话，无法归属）：在当前会话内按输入顺序串行，每条转写稿就绪后各自 snapshot baseline，同一会话累计差即单条实测，无需批次标注，且同样自动续跑整批、不得要求用户逐条回复「继续」（见 `runtime_opencode.md`）。全批同一时刻最多只做 **1** 个 Whisper；**下一条音频下载可与当前总结或当前 Whisper 重叠（B5）**，详见 `multi_video.md`。
3. 临时字幕、音频与 SRT 都放在 `$SCRATCHPAD/video_summary/`（默认工作目录下 `.scratchpad/video_summary/`），不保存到用户目录。只读取 `.txt` 转写稿做总结；原文/中文 SRT 由 `register --subtitle-file` / `--zh-subtitle-file`（或 `append_srt.py`）写入 Markdown 小节，供 HTML「原文」tab 使用，勿在对话中粘贴。
4. **成功成稿必须含「## 原文字幕」**：`register` 时**必须**传入 probe/转写的 `--subtitle-file`（Whisper 用转写 `.srt`）。`register` 后立刻用 `rg -n '^## 原文字幕' "$MD"`（或读文件）核验；缺失则重新 `append_srt.py --heading 原文字幕`，仍无则不得交付、不得 `finalize` 声称完成。平台有中文字幕则附；没有则直接交付，不主动询问或翻译。仅在用户明确要求翻译字幕时按 `subtitle_summary.md` 执行；「原文」不可省略。
5. 开始前用 pipeline `check`（或 `probe`）查 `output/.index.json`；已总结则复用，除非用户要求重跑（`--force`）。重跑必须另存，绝不覆盖。正文不单独写「视频 ID」行，也不得写入文件名——去重依赖索引与正文链接中的真实 ID。
6. 总结必须为中文，带可点击时间戳、最多 3 句金句，以及最多 3 条行动建议/启发。自动字幕标「自动生成」；Whisper 标「语音转写（Whisper large-v3-turbo）」。
7. Whisper 后端：macOS Apple Silicon 必须用 MLX（`mlx-community/whisper-large-v3-turbo`）；其他平台用 faster-whisper。详见 `whisper.md`。
8. 每份成功 Markdown 必须有「运行统计」（建议在字幕小节之后），字段依次为：模型、**Skill 版本**、开始时间、完成时间、用时、Token 用量、LLM 速度。用时须写成一行括号拆分：有 Whisper 为 `用时：…（下载用时：…，语音转写用时：…，总结用时：…）`；无 Whisper 为 `用时：…（下载用时：…，总结用时：…）`。**「用时」= 该条各阶段之和**，只计本条实际处理；不得计入等待用户确认、其他条目或整批闲置。**Token baseline 必须在压缩转写稿已经就绪后、开始读取和总结之前 snapshot**：字幕探测、音频下载、等待用户确认和 Whisper 均在 baseline 之前，不计入 Token 增量或 LLM 速度。**Skill 版本**取自本文件 frontmatter 的 `metadata.version`，由脚本写成 `v2026.08.18`。**LLM 速度** = baseline 后的输出 token ÷ **总结阶段 token 归属窗口秒数**（总结 baseline → 最后一次归属用量事件；拿不到事件时间戳时退回 `summary_seconds`），保留 1 位小数，写成 `92.7 tok/s（总结阶段，含工具执行）`。输出不可用或窗口 ≤ 0 时写「不可用」。**「输出」是转写稿就绪后相对 baseline 的模型增量（可含总结阶段的思考/工具），不是总结正文字数，也不是字幕附录**；若输出≈整会话累计，按当前 agent 专页修复后再交付。Claude Code / Codex 多视频必须一条一个独立工作 agent generation / session 和唯一总结 baseline；Cursor CLI 多视频**必须**优先经嵌套 worker 逐条实测（`--finalize-from-result`，见 `runtime_cursor.md`）；仅嵌套 worker 不可用而自动续跑的多条共用一次 generation 时，脚本会把**批次实测值**写进各条字段并标注口径。OpenCode 多视频在当前会话内串行、每条各自 baseline（单条实测，见 `runtime_opencode.md`）。统计不可用时写「不可用」，不得补零或估算。**Cursor / Claude / Codex / OpenCode 均须用各自 `*_usage.py` 回填统计块（含 Token、LLM 速度与 Skill 版本），禁止手写数字行**；不得跨环境调用脚本。pipeline 的 `download_seconds` / `whisper_seconds` 仍直接用于总用时阶段明细，但不进入 LLM 速度窗口。
9. 每 2 秒的音频下载/Whisper 进度必须更新同一个终端状态行或可变状态卡；不得反复追加新的助手对话文本，也不得等待百分比阈值。
10. 推荐顺序：保存正文 → `register`（必带 `--subtitle-file`）→ 回填运行统计 → 核验「## 原文字幕」仍在 → `finalize`（重建 `视频总结.html`，含 B 站 `bili_bridge`），详见 `html_viewer.md`；重建直接覆盖，无需询问。**禁止**在确认无原文的情况下交付。

## 单条流程

1. 运行当前 agent 的统计 `--doctor` 预检；**此时不要 snapshot baseline**（见 `runtime_statistics.md`）。
2. `summarize_pipeline.py check` → `cached` 则回复路径并结束（cached 文件也须已有「## 原文字幕」，否则当 miss 重跑附原文）。cached 响应现在也会自动确保本地播放服务在运行并返回 `server_running` / `url`，交付时把该 `url` 一并告知；`server_running=false` 时如实说明播放器暂不可用。
3. `probe`。`ok` 时转写稿已就绪；`no_srt` 时按 `whisper.md` 询问，用户同意后完成 `download-audio` 与 `transcribe`；`error` 时报告真实错误（含小红书 `hint`）。
4. **转写稿就绪后立即 snapshot 唯一总结 baseline**，再开始读取 `.txt` 和撰写正文。不得在 Whisper 前复用旧 baseline。
5. 按 `subtitle_summary.md` 生成并保存正文 → `register --subtitle-file …`（必填）→ 回填运行统计 → 核验原文小节 → `finalize`。
6. 交付时附上 HTML 路径与 `finalize` 返回的 `url`。

进度只报告阶段变更：统计预检、识别平台、获取字幕/转写、记录总结 baseline、读取与总结、保存展示。具体工具日志保持一行且精炼。

## 多条流程

两个及以上链接时，读取 `multi_video.md` 与 `pipeline.md`。协调者：`batch-init` → `batch-probe` →（门禁询问）→ 按 `batch-status` 的 `actions` 调度 summarize / download_audio / transcribe。Claude Code / Codex 每条 summarize 交给独立工作 agent；Cursor CLI 与 OpenCode 在当前 turn / 会话内按输入顺序串行处理全部条目，完成一条后直接开始下一条，不向用户索要「继续」。单个失败不得中断其他条目。

## 最终交付

- 单条：告知保存路径并展示总结正文；运行统计只保留在文件内；交付前确认文件含「## 原文字幕」。
- 多条：按输入顺序给出元信息、2–3 句总结和各自路径；最后展示一次批次运行统计；每条成功成稿同须有原文。
- 两种情况都要在最后附上重建后的 `视频总结.html` 路径与本地 `url`；双击 HTML 会自动跳转到该 url（服务未运行时页面自带修复提示）。
- 没有字幕且用户拒绝或未确认 Whisper：只说明没有字幕可供下载并结束该条（此时无 Markdown 成稿）。
