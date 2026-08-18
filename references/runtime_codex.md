# 运行统计 — Codex

先读 `runtime_statistics.md`（共通字段与阶段用时）。本页只写 Codex 专用流程。

## 识别

`CODEX_THREAD_ID` 已设置，或当前 CLI / 进程为 Codex（且非 Cursor）。

## Token / 模型 / LLM 速度（必须用脚本回填，禁止手写数字）

`$SKILL_DIR` = 本 skill 根目录。使用 `scripts/codex_usage.py`：读 Codex rollout 累计 usage，并写入完整「运行统计」块（含 **LLM 速度**）。

任务开始时先做环境预检，但不要提前 snapshot：

```bash
python3 "$SKILL_DIR/scripts/codex_usage.py" --doctor           # 预检，非零退出即降级
```

字幕探测或 Whisper 完成、`transcript_file` 已就绪后，在读取转写稿之前 snapshot 唯一的**总结 baseline**：

```bash
python3 "$SKILL_DIR/scripts/codex_usage.py" --snapshot-baseline "$BASELINE"
# 记下输出中的 SESSION_FILE=… 供结束时复用；此前下载、Whisper、确认等待不计 Token / LLM 速度
date +"SUMMARY_START %Y-%m-%d %H:%M:%S (%s)"
```

本视频整体 `START` 仍按 `runtime_statistics.md` 在下载/字幕阶段记录，用于开始时间与阶段明细。

会话定位优先 `CODEX_THREAD_ID`；该变量未导出时回退到 `$CODEX_HOME/sessions` 下 6 小时内最新的 rollout（超时则判定无活跃会话，不冒充旧会话）。

该视频 Markdown **正文写完并已 `register --subtitle-file` 附上原文**后：

```bash
date +"END %Y-%m-%d %H:%M:%S (%s)"   # 本视频 END
python3 "$SKILL_DIR/scripts/codex_usage.py" --finalize \
  --baseline-file "$BASELINE" \
  --markdown "$MD_PATH" \
  --start-epoch "$START" \
  --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" \
  --summary-seconds "$SUMMARY_SECS" \
  --whisper-seconds "$WHISPER_SECS"
rg -n '^## 原文字幕' "$MD_PATH"
```

可选：`--session "$SESSION_FILE"`、`--model-name "$MODEL"`、`--effort "$EFFORT"`。未走 Whisper 时省略 `--whisper-seconds`。漏记阶段传 `-1`。

脚本写入的字段（Codex 口径）：

- 输入（非缓存）= `Δinput_total - Δcache_read`（负则「不可用」）
- 输出 = `Δoutput`；缓存读 = `Δcache_read`
- 缓存写 = `Δcache_write`，取 rollout 的 `cache_write_input_tokens`（Codex 的真实字段名；不是 Anthropic 风格的 `cache_creation_input_tokens`）
- 合计优先用 rollout 的 `total_tokens`，缺失时回退四项差值之和
- **LLM 速度** = 总结 baseline 后的输出 token ÷ baseline 到最后归属用量事件的窗口，写成 `tok/s（总结阶段，含工具执行）`；事件时间戳不可用时才回退 `--summary-seconds`

模型：优先 `--model-name`；否则脚本从会话读取；取不到写「不可用」。

推理档位（effort）：脚本从 rollout 读 `payload.collaboration_mode.settings.reasoning_effort` / `payload.thread_settings.reasoning_effort` / `payload.effort`（同一条记录内后者更具体者胜，整体以最新记录为准），写成 `- 模型：gpt-5.5 high`。档位优先级 `--effort` > rollout 实测 > baseline 快照；只接受 `none/minimal/low/medium/high/xhigh/max`，因此 `reasoning_output_tokens` 这类数值字段不会被误当档位。读不到时只写模型名（`EFFORT=不可用`，不算降级），**不得**手改统计行补档位。

## 规则

- **不得**手写或改写脚本写入的「运行统计」数字行（含 Token / LLM 速度）；只能通过上述 `--finalize` 回填。
- 多视频必须一条视频一个工作 agent / thread 和一个总结 baseline 文件；工作 agent 只能在该条转写稿就绪后 snapshot，禁止复用整批 baseline。
- 「用时」= 下载用时 + 总结用时（Whisper 时再加语音转写用时）；起止 epoch 必须是该视频自身窗口。
- 不要调用 `cursor_usage.py` 或 `claude_usage.py`。

仍可用只读探测（不写 Markdown）：

```bash
python3 "$SKILL_DIR/scripts/codex_usage.py" --label BASELINE
python3 "$SKILL_DIR/scripts/codex_usage.py" --label CURRENT --session "$SESSION_FILE"
```
