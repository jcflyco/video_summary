# 运行统计

单视频处理者和多视频工作 agent 都必须记录开始/结束 epoch，并在每份成功 Markdown 的最后写：模型、**Skill 版本**、开始时间、完成时间、用时（含阶段拆分）、Token 用量、LLM 速度。**目标：任何模型、任何情况都拿到实测的 Token 用量与 LLM 速度**——单条实测优先；确实无法拆分到单条时由脚本写入带「本批 N 条共用」标注的批次实测值（见下），只有数据在本地根本不存在时才允许「不可用」。多视频时由**每条视频自己的工作 agent**回填；协调者只校验，不手工分摊，也不得把批次值去掉标注冒充单条用量。

**只读当前运行环境对应的 agent 文档**，不要混用其他 agent 的脚本或写法。

## 识别当前 Agent

按优先级判断（命中即停）：

| 优先级 | 环境信号 | Agent | 必读 |
|---|---|---|---|
| 1 | `CURSOR_AGENT` 已设置，或存在 `CURSOR_CONVERSATION_ID` | Cursor | `runtime_cursor.md` |
| 2 | `CODEX_THREAD_ID` 已设置，或进程/CLI 为 Codex | Codex | `runtime_codex.md` |
| 3 | `CLAUDECODE` / `CLAUDE_CODE_ENTRYPOINT` 已设置，或 CLI 为 `claude` | Claude Code | `runtime_claude.md` |

不确定时用：

```bash
python3 - <<'PY'
import os
if os.environ.get("CURSOR_AGENT") or os.environ.get("CURSOR_CONVERSATION_ID"):
    print("cursor")
elif os.environ.get("CODEX_THREAD_ID"):
    print("codex")
elif os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
    print("claude")
else:
    print("unknown")
PY
```

勿仅凭 `CODEX_HOME` / `~/.codex` 目录存在就判定为 Codex（可能只是本机装过）。`unknown` 时也可根据实际正在用的 CLI（`cursor` / `codex` / `claude`）人工选定专页。

`unknown` 时：仍记录墙钟与阶段用时；Token / 模型 / LLM 速度写「不可用」，并在交付时说明未能识别运行环境。

## 预检 `--doctor`（所有 Agent 共通，必须，先于 baseline）

三个脚本都支持 `--doctor`：不写任何文件，只报告「当前环境能否解析出模型名与 Token」。`--doctor` 可在去重/探测前执行；**snapshot baseline 必须等压缩转写稿就绪后再执行**。退出码 `0`=ok、`2`=degraded：

```bash
python3 "$SKILL_DIR/scripts/<agent>_usage.py" --doctor
```

输出形如 `SESSION_FILE=… / MODEL=… / EFFORT=… / DOCTOR=ok`（`MODEL` 已含档位）。任一**关键项**为「不可用」时 `DOCTOR=degraded`；`EFFORT` 只是提示项，不影响判定：

| 症状 | 处理 |
|---|---|
| Claude `SESSION_FILE=不可用` | 用 `--session <绝对路径>` 显式指定；`PROJECT_DIR_KEY` 会打印脚本推算出的目录名，可与 `~/.claude/projects/` 实际目录对照 |
| Codex `SESSION_FILE=不可用` | `CODEX_THREAD_ID` 未导出且无 6 小时内的 rollout；用 `--session <rollout.jsonl>` 指定 |
| Cursor `HOOK_INSTALLED=不可用` | 先跑 `--ensure-hook --skill-dir "$SKILL_DIR"`，否则 Token 永远拿不到 |
| Cursor `MODEL_DB=不可用` | 非 GUI 环境或未装 Cursor 桌面端；模型名由 stop hook / ledger 记录值回填 |
| Cursor `MODEL=不可用` 且桌面设置为 Auto | 正常等待本轮 stop hook；不得把 `Auto` 当实际模型手写进成稿 |
| `EFFORT=不可用` | 会话没写档位或该模型无可选档位。不算降级：模型名照写，档位省略；确知档位时用 `--effort <level>` 传入，不得手写统计行 |

`DOCTOR=degraded` 时仍可继续跑总结，但要预期对应字段写「不可用」，并在交付时说明；**不得**因此手写数字。

**baseline 写不出即失败**：转写稿就绪后、读取正文前执行 `--snapshot-baseline`；三个脚本在会话不可用时会打印 `error=…baseline_not_written` 并以非零退出，不再静默跳过。看到该错误必须当场处理，否则 `--finalize` 会在最后一步报 `baseline_unreadable`，此时总结已写完、增量已无法追溯。不得在字幕探测、等待用户确认、音频下载或 Whisper 之前提前 snapshot；提前建立的 baseline 会把非 LLM 阶段混入速度窗口。

## 开始与结束（所有 Agent 共通）

**按视频独立计时**：每条成功成稿的「开始时间 / 完成时间 / 用时」只反映**该视频自身**的实际处理，不得把等待用户、其他视频、整批闲置算进去。

对该视频首次开始实质性处理时记录 `START`（字幕路径：开始跑该视频的 `fetch_video.py`；Whisper 路径：开始下载该视频仅音频）。该视频总结 Markdown 正文写完、准备回填运行统计时记录 `END`。

```bash
date +"START %Y-%m-%d %H:%M:%S (%s)"
# …字幕探测或音频下载 + Whisper…
python3 "$SKILL_DIR/scripts/<agent>_usage.py" --snapshot-baseline "$BASELINE"
date +"SUMMARY_START %Y-%m-%d %H:%M:%S (%s)"
# …读取压缩转写稿并总结…
date +"END %Y-%m-%d %H:%M:%S (%s)"
```

`START` / `END` 继续描述本视频的处理时间；`SUMMARY_START` 与 baseline 只描述 LLM 总结窗口。等待用户确认不得计入任何阶段，Whisper 用时只进入「语音转写用时」，不进入 Token 或 LLM 速度。

多视频时：每条视频各自一对 START/END、一个唯一 baseline 文件和一个独立 agent generation / session；禁止用整批会话的起止 epoch 或 Token baseline 回填到单条成稿。若多个视频只能共用同一次聚合用量事件（仅 Cursor 回退路径会发生），由脚本把**批次实测值**写进各条字段并标注「本批 N 条共用一次生成，未拆分到单条」/「本批 N 条共用，端到端」；批次值必须带标注，禁止去掉标注冒充单条实测，也禁止手工按比例分摊。

## 用时与阶段拆分（所有 Agent 共通，必须）

**用时 = 本视频各阶段用时之和**（有 Whisper：下载 + 语音转写 + 总结；无 Whisper：下载 + 总结）。不足一分钟写秒数。统计读取失败时写「不可用」，不得估算或写全 0。

阶段秒数用真实 wall-clock（`date +%s` 或命令前后 epoch 差）：

| 字段 | 何时写 | 如何计量 |
|---|---|---|
| 下载用时 | 每次成功成稿 | 字幕路径：该视频 `fetch_video.py` 整段墙钟；Whisper 路径：仅音频下载墙钟（不含失败的字幕探测） |
| 语音转写用时 | 仅走了 Whisper | 该视频 `transcribe_whisper.py` 整段墙钟（含模型加载） |
| 总结用时 | 每次成功成稿 | 从开始阅读该视频压缩转写稿到其总结 Markdown 写完（写入/回填运行统计之前）的墙钟 |

**必须计入**：上表各阶段。

**不得计入「用时」**（也不得灌进该视频的 START/END）：

- 等待用户确认 Whisper / 回复门禁
- 其他视频的探测、下载、转写、总结
- HTML 重建、安装依赖、批次汇总闲置、对话间隙

规则：

- 阶段用时全部写在**同一行**「用时」括号内，用中文逗号 `，` 分隔；不要再单独开「下载用时 / 语音转写用时 / 总结用时」列表项。
- 未走 Whisper 时**省略**括号内的「语音转写用时」项，不要写 0 秒；无字幕路径仍须拆分「下载用时」与「总结用时」。
- 某一阶段漏记或读取失败时该项写「不可用」，不得估算；其余已计量阶段仍按秒数相加得到「用时」。
- 「用时」必须等于括号内各阶段秒数之和（省略的 Whisper 项不参与相加）；禁止再用「含等待用户的墙钟差」充当总用时。
- **如何把阶段秒数写进 Markdown**：见当前 agent 专页（三者均经各自 `*_usage.py --finalize` 传入阶段秒数；禁止手写）。

每阶段开始/结束各记一次 epoch，例如：

```bash
date +"DOWNLOAD_START %s"
# …下载命令…
date +"DOWNLOAD_END %s"
```

## Token「输出」口径（所有 Agent 共通）

「Token 用量 · 输出」= **转写稿就绪后相对总结 baseline 的模型输出增量**（可含总结阶段的思考、工具调用、多轮回复），**不是**总结 Markdown 可见正文字数，也不是脚本追加的「原文字幕 / 中文字幕」。下载、Whisper 和等待用户确认发生在 baseline 之前，因此也不进入 Token 增量。正文字数远小于输出 token 是正常的；若输出≈整段会话累计，或推算出的 LLM 速度明显虚高，按当前 agent 专页修复（Cursor：`--repair-from-ledger` / 等待 hook），不得当正常值交付。

## Skill 版本（所有 Agent 共通，必须）

`SKILL.md` frontmatter 的 `metadata.version` 会被脚本读出来写成 `- Skill 版本：v2026.07.30`，紧跟在「模型」之后。用途是把一份成稿绑定到产出它的 skill 修订版，便于回归对比。

| 规则 | 说明 |
|---|---|
| 来源 | `SKILL.md` frontmatter 的 `metadata.version`；脚本自动读取，**禁止手写** |
| 格式 | `v` + 版本号，如 `v2026.07.30` |
| 不可用 | 找不到 `SKILL.md` 或没有 `metadata.version` 时写「不可用」 |
| 改版本 | 只改 `SKILL.md` 的 `metadata.version` 一处；Cursor 需重跑 `--ensure-hook` 让 hook 侧同步 |

## LLM 速度（所有 Agent 共通，必须）

**总结 baseline 后的输出 token ÷ 总结阶段 token 归属窗口墙钟（秒）**，单位 `tok/s`，后缀「（总结阶段，含工具执行）」。

分子和分母必须从同一个“转写稿已就绪”的 baseline 开始。这样下载、Whisper、模型加载和用户确认等待既不进入分子，也不进入分母；总结过程中发生的工具调用仍保留在窗口内，因此它不是纯解码速度。

| 规则 | 说明 |
|---|---|
| 公式 | `LLM 速度 = 输出 token 数 ÷ 归属窗口秒数` |
| 归属窗口 | 从**转写稿就绪后的总结 baseline**到**最后一次归属用量事件**；Cursor 的 stop hook 在 agent 记录 END 之后才触发，用 END 截断会少算真实生成时间 |
| 回退 | 拿不到事件时间戳时使用实测 `summary_seconds`；只有旧记录连总结用时也没有时才退回 `END − START` |
| 格式 | 保留 1 位小数 + `（总结阶段，含工具执行）`，如 `92.7 tok/s（总结阶段，含工具执行）` |
| 批次共用（仅 Cursor 回退路径） | 多条共用一次生成时写**批次实测**速度并保留「本批」标注；窗口只覆盖该批实际总结生成，不含前置下载/Whisper |
| 不可用 | 输出 token 不可用、或窗口秒数 ≤ 0 时写「不可用」（批次事件未落地时暂为「不可用（…待回填）」，stop hook 后自动升级） |
| 写入位置 | 紧接「Token 用量」之后单独一行 |

窗口内包含总结阶段的工具执行时间，所以这是**总结阶段 agent 吞吐**，不是纯解码速度——后缀就是在说明这件事，不要删。音频下载、Whisper 和用户确认等待明确排除。

Cursor / Claude / Codex 均由各自 `*_usage.py --finalize`（Cursor 另有 `--ensure-stats`）自动计算写入；禁止手写 Token / LLM 速度数字。

## Markdown 字段与示例（所有 Agent 共通）

成稿末尾固定结构（字段名不得改）：

「模型」必须写成 `<模型名> <档位>`（如 `claude-opus-5 high`、`gpt-5.5 medium`）。三个 `*_usage.py` 都会自动从当前会话读出推理档位（effort / reasoning_effort）并拼在模型名后；档位已包含在模型名里时（如 `gpt-5.6-sol-high`）不重复追加，读不到档位时只写模型名，模型本身取不到才写「不可用」。禁止手写这一行；如需纠正档位用脚本的 `--effort <level>`（Cursor 见其专页）。

```markdown
---

## 运行统计

- 模型：…（含档位，如 `claude-opus-5 high`）
- Skill 版本：vYYYY.MM.DD
- 开始时间：YYYY-MM-DD HH:MM:SS
- 完成时间：YYYY-MM-DD HH:MM:SS
- 用时：…（下载用时：…，总结用时：…）
- Token 用量：输入（非缓存）… · 输出 … · 缓存读 … · 缓存写 … · 合计 …
- LLM 速度：… tok/s（总结阶段，含工具执行）
```

字幕路径（无须语音转写）示例：

```markdown
---

## 运行统计

- 模型：gpt-5.5 medium
- Skill 版本：v2026.07.30
- 开始时间：2026-07-05 14:52:31
- 完成时间：2026-07-05 14:57:41
- 用时：5 分 10 秒（下载用时：18 秒，总结用时：4 分 52 秒）
- Token 用量：输入（非缓存）1,234 · 输出 8,765 · 缓存读 456,789 · 缓存写 23,456 · 合计 466,788
- LLM 速度：28.1 tok/s（总结阶段，含工具执行）
```

（上例：转写稿就绪后的 baseline 到最后一次用量事件共 312 秒，8765 ÷ 312 ≈ 28.1；若事件时间戳不可用才回退到总结用时 292 秒）

Whisper 路径示例（用时 = 4 + 297 + 39 秒）：

```markdown
- 用时：5 分 40 秒（下载用时：4 秒，语音转写用时：4 分 57 秒，总结用时：39 秒）
- LLM 速度：… tok/s（总结阶段，含工具执行）
```

Cursor 批次回退示例（多条共用一次生成，脚本自动写批次实测值，禁止手写）：

```markdown
- Token 用量：输入（非缓存）2,100 · 输出 32,975 · 缓存读 50,000 · 缓存写 1,000 · 合计 35,075（本批 3 条共用一次生成，未拆分到单条）
- LLM 速度：119.5 tok/s（本批 3 条共用，端到端，总结阶段，含工具执行）
```

多视频每条成稿仍只写**该视频**的开始/完成/用时；另增一行「本批 N 个视频，成功 X / 失败 Y，不含并行子任务」（批次说明，不替代单条用时）。对话交付时可另报一次整批墙钟，但不得写进单条「用时」。

仅使用**当前**运行环境的模型信息；模型或档位取不到写「不可用」。Token / LLM 速度计算公式与脚本调用见对应 agent 专页，禁止跨环境套用（例如在 Claude 里跑 `cursor_usage.py`）。
