# 运行统计 — Claude Code

先读 `runtime_statistics.md`（共通字段与阶段用时）。本页只写 Claude Code 专用流程。

## 识别

`CLAUDECODE` 或 `CLAUDE_CODE_ENTRYPOINT` 已设置，或当前 CLI 为 `claude`（且非 Cursor / Codex）。

## Token / 模型 / LLM 速度（必须用脚本回填，禁止手写数字）

`$SKILL_DIR` = 本 skill 根目录。使用 `scripts/claude_usage.py`：读当前项目会话 JSONL 累计 usage（按 `requestId` 去重后求和），并写入完整「运行统计」块（含 **LLM 速度**）。

任务开始时先做环境预检，但不要提前 snapshot：

```bash
python3 "$SKILL_DIR/scripts/claude_usage.py" --doctor          # 预检，非零退出即降级
```

字幕探测或 Whisper 完成、`transcript_file` 已就绪后，在读取转写稿之前 snapshot 唯一的**总结 baseline**：

```bash
python3 "$SKILL_DIR/scripts/claude_usage.py" --snapshot-baseline "$BASELINE" --cwd "$PWD"
# 记下 SESSION_FILE=…；可用时一并记下 MODEL=…；此前下载、Whisper、确认等待不计 Token / LLM 速度
date +"SUMMARY_START %Y-%m-%d %H:%M:%S (%s)"
```

本视频整体 `START` 仍按 `runtime_statistics.md` 在下载/字幕阶段记录，用于开始时间与阶段明细。

会话定位：项目目录名 = 前置 `-`，再把工作路径中**每个** `[A-Za-z0-9-]` 之外的字符各换成一个 `-`（`.` 与中文字符同样逐字符替换）。脚本按此规则并附多候选与全局兜底查找；仍失败时 `--snapshot-baseline` 会非零退出，用 `--session` 显式指定即可。

该视频 Markdown **正文写完并已 `register --subtitle-file` 附上原文**后：

```bash
date +"END %Y-%m-%d %H:%M:%S (%s)"   # 本视频 END
python3 "$SKILL_DIR/scripts/claude_usage.py" --finalize \
  --baseline-file "$BASELINE" \
  --markdown "$MD_PATH" \
  --start-epoch "$START" \
  --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" \
  --summary-seconds "$SUMMARY_SECS" \
  --whisper-seconds "$WHISPER_SECS"
# 统计回填后必须再确认原文仍在：
rg -n '^## 原文字幕' "$MD_PATH"
```

可选：`--session "$SESSION_FILE"`、`--model-name "$MODEL"`、`--effort "$EFFORT"`。未走 Whisper 时省略 `--whisper-seconds`。漏记阶段传 `-1`。

脚本写入的字段（Claude 口径）：

- 输入（非缓存）= `Δinput_total`（JSONL `input_tokens`，不含 cache；**不再**减缓存读）
- 输出 / 缓存读 / 缓存写 / 合计 = 对应差值
- **LLM 速度** = 总结 baseline 后的输出 token ÷ baseline 到最后归属用量事件的窗口，写成 `tok/s（总结阶段，含工具执行）`；事件时间戳不可用时才回退 `--summary-seconds`

禁止把缓存读再加进「输入（非缓存）」。JSONL 异常时 Token / LLM 速度写「不可用」，不得估算。

模型：优先 `--model-name`；否则脚本从会话读取；仍取不到写「不可用」。

推理档位（effort）：脚本从会话 JSONL 的 assistant 记录 `effort` 字段读取最新值（会话中途 `/effort` 切换以最后一次为准），写成 `- 模型：claude-opus-5 high`。档位优先级 `--effort` > 会话实测 > baseline 快照；只接受 `none/minimal/low/medium/high/xhigh/max`，其余值忽略。会话没写档位时只写模型名（`EFFORT=不可用`，不算降级），**不得**手改统计行补档位。

## 规则

- **不得**手写或改写脚本写入的「运行统计」数字行（含 Token / LLM 速度）；只能通过上述 `--finalize` 回填。
- 多视频必须一条视频一个工作 agent / session 和一个总结 baseline 文件；工作 agent 只能在该条转写稿就绪后 snapshot，禁止复用整批 baseline。
- 「用时」= 传入各阶段秒数之和（有 Whisper 含转写）；`--start-epoch` / `--end-epoch` 必须是该视频自身窗口。
- 不要调用 `cursor_usage.py` / `codex_usage.py` / `opencode_usage.py` / `pi_usage.py`。

仍可用只读探测（不写 Markdown）：

```bash
python3 "$SKILL_DIR/scripts/claude_usage.py" --label BASELINE --cwd "$PWD"
python3 "$SKILL_DIR/scripts/claude_usage.py" --label CURRENT --session "$SESSION_FILE"
```
