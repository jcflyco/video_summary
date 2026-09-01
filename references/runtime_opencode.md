# 运行统计 — OpenCode

先读 `runtime_statistics.md`（共通字段与阶段用时）。本页只写 OpenCode 专用流程。

## 识别

`OPENCODE=1`（或 `OPENCODE_PID` 已设置）。OpenCode 会给 shell 工具子进程注入这两个变量；不要凭 `~/.local/share/opencode` 目录存在就判定（可能只是本机装过）。

## 数据来源

OpenCode 把会话与逐请求用量写进 SQLite：`${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`（可用 `OPENCODE_DB` 覆盖）。`session` 表的 `tokens_*` 列是该会话累计值（等于各 assistant 消息用量之和），`message` 表的 JSON 带每次请求的 `tokens` 与 `time.completed` 时间戳。脚本只读（`mode=ro`），不写 DB。

会话定位：OpenCode 不向子进程导出 session id，脚本按「6 小时内最新、`directory` 等于当前工作目录的会话」定位（正在执行本脚本的 shell 调用会让该会话保持最新）；匹配不到再回退 6 小时内全局最新会话。可用 `--session ses_…` 显式指定（session id 可在 opencode TUI 或 `sqlite3` 里查）。

## Token / 模型 / LLM 速度（必须用脚本回填，禁止手写数字）

`$SKILL_DIR` = 本 skill 根目录。使用 `scripts/opencode_usage.py`。

任务开始时先做环境预检，但不要提前 snapshot：

```bash
python3 "$SKILL_DIR/scripts/opencode_usage.py" --doctor        # 预检，非零退出即降级
```

字幕探测或 Whisper 完成、`transcript_file` 已就绪后，在读取转写稿之前 snapshot 唯一的**总结 baseline**：

```bash
python3 "$SKILL_DIR/scripts/opencode_usage.py" --snapshot-baseline "$BASELINE"
# 记下输出中的 SESSION_ID=… 供结束时复用；此前下载、Whisper、确认等待不计 Token / LLM 速度
date +"SUMMARY_START %Y-%m-%d %H:%M:%S (%s)"
```

本视频整体 `START` 仍按 `runtime_statistics.md` 在下载/字幕阶段记录，用于开始时间与阶段明细。

该视频 Markdown **正文写完并已 `register --subtitle-file` 附上原文**后：

```bash
date +"END %Y-%m-%d %H:%M:%S (%s)"   # 本视频 END
python3 "$SKILL_DIR/scripts/opencode_usage.py" --finalize \
  --baseline-file "$BASELINE" \
  --markdown "$MD_PATH" \
  --start-epoch "$START" \
  --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" \
  --summary-seconds "$SUMMARY_SECS" \
  --whisper-seconds "$WHISPER_SECS"
rg -n '^## 原文字幕' "$MD_PATH"
```

可选：`--session "$SESSION_ID"`、`--model-name "$MODEL"`、`--effort "$EFFORT"`。未走 Whisper 时省略 `--whisper-seconds`。漏记阶段传 `-1`。

脚本写入的字段（OpenCode 口径）：

- 输入（非缓存）= `Δtokens_input`（OpenCode 的 `input` 本身就是 nonCachedInputTokens，不再扣缓存）
- 输出 = `Δ(tokens_output + tokens_reasoning)`（OpenCode 的 `output` 是可见输出、reasoning 单列，二者相加才对齐 Claude/Codex 把思考计入输出的口径）
- 缓存读 = `Δtokens_cache_read`；缓存写 = `Δtokens_cache_write`
- 合计 = 四项之和（session 表没有单独的 total 列）
- **LLM 速度** = 总结 baseline 后的输出 token ÷ baseline 到最后一条 assistant 消息 `time.completed` 的窗口，写成 `tok/s（总结阶段，含工具执行）`；事件时间戳不可用时回退 `END − baseline`，再退 `--summary-seconds`

模型：优先 `--model-name`；否则取最后一条带用量的 assistant 消息 `modelID`，再退 session 当前选中模型；取不到写「不可用」。推理档位：session 的 `model.variant` 命中 `none/minimal/low/medium/high/xhigh/max` 白名单时拼在模型名后（`variant=default` 等不显示，不算降级）；确知档位时用 `--effort <level>` 传入，不得手写统计行。

## 多视频（OpenCode 专属调度）

**不派子 agent 总结**：OpenCode 的 task 子 agent 用量落在独立子会话，工作 agent 的 shell 内无法可靠归属到自己的会话。多视频时在**当前会话内按输入顺序串行**处理：每条转写稿就绪后各自 `--snapshot-baseline`（每条一个 baseline 文件），写完即 `--finalize`，再开始下一条。同一会话的累计差就是**单条实测**，无需「本批共用」标注。完成一条后直接开始下一条，不等待用户说「继续」；唯一暂停点是 Whisper 明示同意等硬门禁。

## 规则

- **不得**手写或改写脚本写入的「运行统计」数字行（含 Token / LLM 速度）；只能通过上述 `--finalize` 回填。
- 每条视频一个 baseline 文件；只能在该条转写稿就绪后 snapshot，禁止复用上一条的 baseline。
- 「用时」= 下载用时 + 总结用时（Whisper 时再加语音转写用时）；起止 epoch 必须是该视频自身窗口。
- 不要调用 `cursor_usage.py` / `claude_usage.py` / `codex_usage.py` / `pi_usage.py`。

仍可用只读探测（不写 Markdown）：

```bash
python3 "$SKILL_DIR/scripts/opencode_usage.py" --label BASELINE
python3 "$SKILL_DIR/scripts/opencode_usage.py" --label CURRENT --session "$SESSION_ID"
```
