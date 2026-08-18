# 多视频模式

提取、去重并保序所有支持的 URL（YouTube / Bilibili / 小红书 / X / Apple Podcasts / 小宇宙 / 长桥直播）；不支持链接说明后跳过。协调者只记录批次墙钟；**每条视频必须在转写稿就绪后、独立 agent generation / session 开始总结前建立自己的总结 Token baseline**，编排一律走 `summarize_pipeline.py`（见 `pipeline.md`）。

```bash
PIPE='python3 "$SKILL_DIR/scripts/summarize_pipeline.py"'
SCRATCH="${SCRATCHPAD:-.scratchpad}/video_summary"
```

## 阶段 A：探测字幕（不总结）

```bash
$PIPE batch-init --dir . --scratchpad "$SCRATCH" --url "U1" --url "U2"
$PIPE batch-probe --dir . --scratchpad "$SCRATCH"
```

`batch-probe` 会对未命中 `output/.index.json` 的条目跑字幕探测；`cached` 直接标记已有总结。此阶段：

- **不得**读取转写稿做总结，**不得**写入 `output/*_总结.md`，**不得** `finalize`。
- 工作任务若参与探测：只返回状态与元信息；遇到 `no_srt` 只回报，绝不下载音频或运行 Whisper。
- `upload_date` 必须从 probe 结果写入批次状态，并继续传入 `summarize` action；平台已返回日期时不得降级为「上传日期不可用」。

## 阶段 B：无字幕先问（硬门禁）

查看 `batch-status`：若 `gated=true` / 存在 `ask_whisper_consent`：

1. **立即暂停**，一次性列出全部无字幕视频（标题 + 链接即可）。
2. 询问用户是否对这些视频下载**仅音频**并用本地 whisper-large-v3-turbo 转写后继续总结；可说明「同意全部 / 同意其中若干 / 全部跳过」。
3. **在用户明确回复之前**，不得对任何视频（含已有字幕的 `ok`）开始总结、Whisper 或 HTML 重建。
4. 用户回复后：

```bash
$PIPE batch-set-consent --scratchpad "$SCRATCH" --consent yes --all
# 或 --consent yes --video-id ID1 --video-id ID2
# 拒绝：--consent no --all 或指定 id
```

若探测结果无待确认 `no_srt`，可跳过本阶段，直接进入阶段 C。

## 阶段 C：用户确认后的并行与 B5 流水线

反复 `$PIPE batch-status --scratchpad "$SCRATCH"`，按返回的 `actions` 调度。**不要**等全部 Whisper 做完才开始有字幕视频的总结。

### C1. `summarize` → 独立工作 agent；Cursor CLI 当前 turn 自动串行

**先判断运行环境**（见 `runtime_statistics.md`），两种调度方式：

| 环境 | 调度 | 原因 |
|---|---|---|
| Claude Code / Codex | 派工作 agent **并行**总结，每个 agent 只处理一个视频 | 子 agent 的用量能独立采样 |
| **Cursor CLI** | **不得派子 agent；必须优先用嵌套 `cursor-agent` worker 逐条生成正文（单条实测统计），worker 不可用才在当前 turn 内串行并自动跑完整批** | Cursor 不记录子 agent token；嵌套 worker 的 `result` 事件带真实 usage，无需用户逐条输入「继续」 |

Cursor CLI 下完成一条后直接开始下一条，不结束回复、不等待用户说「继续」；唯一暂停点是 Whisper 明示同意等硬门禁。具体统计口径见 `runtime_cursor.md` 的「子 agent 拿不到 Token」一节。

工作 agent（或 Cursor 下的串行处理者）还须读取 `runtime_statistics.md` 与自身运行环境专页，在该条 `transcript_file` 已就绪后使用自己的会话建立唯一总结 baseline（推荐 `$SCRATCH/<video_id>.<agent>.baseline.json`），再开始读取与总结。

Claude Code / Codex **禁止让一个工作 agent generation 同时总结两条视频**。Cursor CLI 回退串行是例外：允许当前 generation 串行处理整批，该批每条的 Token / LLM 速度由脚本写入**批次实测值**并标注「本批 N 条共用」（stop hook 落地前暂为「不可用（…待回填）」），不得去掉标注把批次值冒充单条用量。

```bash
$PIPE batch-mark-summary --scratchpad "$SCRATCH" --video-id ID --status running
# …写 Markdown…
$PIPE register --dir . --scratchpad "$SCRATCH" --markdown PATH --url URL --title TITLE \
  --subtitle-file SUBTITLE_FILE [--zh-subtitle-file ZH_FILE]
# 成功成稿必须含「## 原文字幕」；register 后核验再交付
# 工作 agent 用自己的 baseline 立即调用当前环境的 *_usage.py --finalize
$PIPE batch-mark-summary --scratchpad "$SCRATCH" --video-id ID --status done --markdown PATH
```

### C2. Whisper 串行 + 音频预取（B5）

硬约束不变：全批同一时刻最多 **1** 个 `transcribe`（`whisper_active` 非空时禁止再开）。

**B5 允许重叠：**

- `download_audio`（`overlap_ok=true`）可与正在进行的 **summarize** 或 **当前 Whisper** 并行执行。
- 典型节奏：条目 i 转写完成后开始总结 i；同时 `download-audio --batch` 拉取队列中下一条音频；i 的总结与 i+1 的下载重叠；仅当无 `whisper_active` 且下一条 `audio_status=ready` 时再 `transcribe --batch`。

```bash
$PIPE download-audio --dir . --scratchpad "$SCRATCH" --batch --url "URL"
$PIPE transcribe --dir . --scratchpad "$SCRATCH" --batch \
  --audio "$AUDIO" --video-id "$ID" --language "$LANG"
```

禁止：并行多个 Whisper；为抢进度而对多条同时 `transcribe`。

### C3. 汇总

工作任务只返回：`ok/no_srt/error`、标题、频道、时长、字幕类型和语言、保存路径、2–3 句中文总结，以及统计状态；不要返回完整 Markdown。其 Markdown 由该工作 agent 自行回填运行统计。

协调者使用一个可更新状态卡显示本阶段 N 个视频的完成比例（可区分「字幕总结中 / Whisper 队列位置 / 转写中 / 预取下载中」）；不要为每个轮询生成新的对话消息。本阶段全部返回后：

1. 校验每个成功文件非空，包含「总结」「分段要点」「运行统计」及至少一个平台时间戳链接。
2. 校验工作 agent 已用**该视频转写稿就绪后的总结 baseline 与 session**回填统计；协调者不得再次用批次 baseline 覆盖。**每条成稿独立计时**：使用该视频自己的阶段秒数（下载 / 语音转写 / 总结）；「用时」= 该视频阶段秒数之和，但 LLM 速度只使用总结窗口。禁止把等待用户确认、其他视频处理、整批会话墙钟写进单条「用时」或 LLM 速度。pipeline 返回的 `download_seconds` / `whisper_seconds` 可直接用。
3. 按输入顺序展示元信息、总结和文件路径；视频间用 `---` 分隔；最后展示一次批次运行统计（可含整批墙钟，与单条「用时」分开），并 `$PIPE finalize --dir .`。

每个工作 agent 的 token 只写入它负责的视频，协调者 token 不分摊到任何视频。字幕临时文件和 baseline 必须按视频 ID 命名，共用 scratchpad 不互相覆盖。
