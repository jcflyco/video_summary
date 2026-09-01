# 运行统计 — pi

先读 `runtime_statistics.md`（共通字段与阶段用时）。本页只写 pi（pi coding agent）专用流程。

## 识别

`PI_CODING_AGENT=true` 或 `AI_AGENT=pi` 已设置。pi 会给 shell 工具子进程注入这两个变量；不要凭 `~/.pi` 目录存在就判定（可能只是本机装过）。

## 数据来源

pi 把会话写成 JSONL：`~/.pi/agent/sessions/<cwd 转义>/<ISO 时间戳>_<uuid>.jsonl`（agent 目录可用 `PI_CODING_AGENT_DIR` 覆盖，sessions 目录可用 `PI_CODING_AGENT_SESSION_DIR` 覆盖）。每条 assistant 消息带**逐请求** `usage`（`input` / `output` / `cacheRead` / `cacheWrite` / `reasoning` / `totalTokens`）与毫秒级 `timestamp`；`model_change` / `thinking_level_change` 事件记录当前模型与思考档位。脚本只读会话文件，不写。

会话定位：pi 不向子进程导出 session id，脚本按「cwd 转义目录内 mtime 最新的 `.jsonl`」定位（正在执行本脚本的 shell 调用会让该文件保持最新）；目录不存在时回退 6 小时内全局最新会话。cwd 转义规则 = `--` + 工作路径去掉开头 `/` 后把每个 `/`、`\`、`:` 换成 `-`（点、下划线与非 ASCII **保留原样**，与 Claude Code 的规则不同）+ `--`。仍失败时用 `--session <绝对路径>` 显式指定。

## Token / 模型 / LLM 速度（必须用脚本回填，禁止手写数字）

`$SKILL_DIR` = 本 skill 根目录。使用 `scripts/pi_usage.py`。

任务开始时先做环境预检，但不要提前 snapshot：

```bash
python3 "$SKILL_DIR/scripts/pi_usage.py" --doctor          # 预检，非零退出即降级
```

字幕探测或 Whisper 完成、`transcript_file` 已就绪后，在读取转写稿之前 snapshot 唯一的**总结 baseline**：

```bash
python3 "$SKILL_DIR/scripts/pi_usage.py" --snapshot-baseline "$BASELINE" --cwd "$PWD"
# 记下 SESSION_FILE=…；此前下载、Whisper、确认等待不计 Token / LLM 速度
date +"SUMMARY_START %Y-%m-%d %H:%M:%S (%s)"
```

本视频整体 `START` 仍按 `runtime_statistics.md` 在下载/字幕阶段记录，用于开始时间与阶段明细。

该视频 Markdown **正文写完并已 `register --subtitle-file` 附上原文**后：

```bash
date +"END %Y-%m-%d %H:%M:%S (%s)"   # 本视频 END
python3 "$SKILL_DIR/scripts/pi_usage.py" --finalize \
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

脚本写入的字段（pi 口径）：

- 输入（非缓存）= `Δinput`（pi 的 `input` 不含缓存读/写，不再扣减）
- 输出 = `Δoutput`；仅当某条消息 `totalTokens > input+output+cacheRead+cacheWrite`（如 Gemini 把思考单列）时补加该条 `reasoning`，对齐「思考计入输出」的口径且不双计
- 缓存读 = `ΔcacheRead`；缓存写 = `ΔcacheWrite`；合计 = 四项之和
- **LLM 速度** = 总结 baseline 后的输出 token ÷ baseline 到最后一条 assistant 消息 `timestamp` 的窗口，写成 `tok/s（总结阶段，含工具执行）`；事件时间戳不可用时回退 `END − baseline`，再退 `--summary-seconds`

模型：优先 `--model-name`；否则取最后一条带用量的 assistant 消息 `model`（`model_change` 事件兜底）；取不到写「不可用」。推理档位：取最后一次 `thinking_level_change` 的 `thinkingLevel`，命中 `none/minimal/low/medium/high/xhigh/max` 白名单时拼在模型名后；`off` 或未设置时只写模型名（`EFFORT=不可用`，不算降级）；确知档位时用 `--effort <level>` 传入，不得手写统计行。

## 多视频（pi 专属调度）

**不派子 agent 总结**：pi 默认没有可靠归属用量的子 agent 机制。多视频时在**当前会话内按输入顺序串行**处理：每条转写稿就绪后各自 `--snapshot-baseline`（每条一个 baseline 文件），写完即 `--finalize`，再开始下一条。同一会话的累计差就是**单条实测**，无需「本批共用」标注。完成一条后直接开始下一条，不等待用户说「继续」；唯一暂停点是 Whisper 明示同意等硬门禁。

## 规则

- **不得**手写或改写脚本写入的「运行统计」数字行（含 Token / LLM 速度）；只能通过上述 `--finalize` 回填。
- 每条视频一个 baseline 文件；只能在该条转写稿就绪后 snapshot，禁止复用上一条的 baseline。
- 「用时」= 下载用时 + 总结用时（Whisper 时再加语音转写用时）；起止 epoch 必须是该视频自身窗口。
- 不要调用 `cursor_usage.py` / `claude_usage.py` / `codex_usage.py` / `opencode_usage.py`。

仍可用只读探测（不写 Markdown）：

```bash
python3 "$SKILL_DIR/scripts/pi_usage.py" --label BASELINE --cwd "$PWD"
python3 "$SKILL_DIR/scripts/pi_usage.py" --label CURRENT --session "$SESSION_FILE"
```
