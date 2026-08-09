# 运行统计 — Cursor

先读 `runtime_statistics.md`（共通字段与阶段用时）。本页只写 Cursor 专用流程。

## 识别

`CURSOR_AGENT` 已设置，或存在 `CURSOR_CONVERSATION_ID`。

## Token 口径（必读）

「Token 用量 · 输出」= **本条处理相对 baseline 的模型输出增量**（含思考 / 工具调用 / 多轮回复），**不是**总结 Markdown 正文字数，也不是「原文字幕 / 中文字幕」附录（字幕由 `register` 脚本写入）。

因此：输出 token 常大于可见总结；但若接近整段会话累计、或 LLM 速度高得离谱（例如数百 tok/s 且总结仅两千字），视为回填错误，必须按下节修复，禁止当正常值交付。

## 子 agent 拿不到 Token —— 多视频自动串行（必读）

Cursor 的 `stop` / `afterAgentResponse` **只对用户直接对话的那个 conversation 触发**。派生出去的子 agent 有自己的 conversation id，但它的 token 用量在本地**任何地方都没有记录**：

- `~/.cursor/hooks/usage/<子 conv>.json` 永远不会被写出；
- `~/.cursor/projects/*/agent-transcripts/<子 conv>/*.jsonl` 里没有 usage 字段；
- `~/.cursor/chats/*/<子 conv>/store.db` 的 blobs 里也没有。

父会话 ledger 里那几百个 output token 是**协调者自己**的，不是子 agent 写总结花掉的。脚本因此会检测「子 agent 且自身 ledger 为空」，直接写：

```
- Token 用量：不可用（Cursor 未记录子 agent 的 token；该条无法获得实测值）
- LLM 速度：不可用
```

**所以在 Cursor CLI 下处理多个视频时，不得派并行子 agent 做总结。** 多视频**必须**先走下节「嵌套 worker」为每条拿到**单条实测** Token / LLM 速度；只有嵌套 worker 确实不可用（`cursor-agent` 缺失、worker 失败、usage 解析不到）时才按以下规则回退：

1. 在用户发起批量任务的当前 turn 内按输入顺序逐条串行；一条完成后立即处理下一条，直到整批完成。**不得结束本轮等待用户回复「继续」**，也不得为自动续跑再次征求许可。唯一需要暂停的仍是无字幕音频的 Whisper 明示同意等硬门禁。
2. 整批只有一次 generation 事件，单条数字无法拆分，但批次本身是实测的：聚合事件落地后，脚本会把**批次实测值直接写进各条字段**并标注口径——Token 行如 `输入（非缓存）2,100 · 输出 32,975 · … · 合计 35,075（本批 3 条共用一次生成，未拆分到单条）`，速度行如 `119.5 tok/s（本批 3 条共用，端到端，含工具执行）`（窗口 = 最早 baseline → 最后归属事件，批次端到端吞吐）。事件未落地前（turn 还没结束）暂为「不可用（本批 N 条共用一次生成，未拆分到单条；批次实测待 stop hook 上报后自动回填）」，本轮结束后由 stop hook / 下一轮 `--flush-pending` 自动升级为实测值。这是回退路径的预期取舍，不因此中断任务。
3. 每条仍单独记录真实阶段用时并使用唯一 baseline / Markdown pending job；stop hook 发现同一 conversation 在本 turn 有多个 pending job 时，会自动把它们全部按批次口径结算，避免较早条目永久 pending。
4. 任何情况下都**不得**去掉「本批」标注把批次值冒充单条用量，也不得手工按比例分摊。

（Claude Code / Codex 无此限制，子 agent 的用量都能独立采样。）

脚本已能通过 `~/.cursor/chats/*/<conv>/store.db` 的 `subagentInfo.parentAgentId` 回溯父会话，所以 pending job 不会再永久卡在「等一个永远不会写出的 ledger」上——但回溯只解决**结算**，解决不了 Cursor 压根没记的数据。

## 多视频单条实测统计 —— 嵌套 worker（多视频必须优先尝试）

内置子 agent 的 token 无处可查、headless 运行的 hook 也不带 token 字段，但嵌套的 `cursor-agent -p --output-format stream-json` 是一次独立 generation，其输出末尾的 `result` 事件带**真实 usage**（`inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens`）和 `duration_api_ms`。多视频时每条视频的总结正文**必须**改由一个嵌套 worker 生成（先 `command -v cursor-agent` 确认存在），即可逐条拿到实测 Token / LLM 速度；跳过 worker 直接串行会让整批只剩批次级统计，属于回退路径而非默认：

```bash
CAP="$(mktemp)"
command cursor-agent -p --output-format stream-json --model "<模型>" \
  "<按 subtitle_summary.md 要求撰写该视频中文总结正文的完整指令；输入为 <压缩转写稿绝对路径>；只输出 Markdown 正文>" > "$CAP"
python3 "$SKILL_DIR/scripts/cursor_usage.py" --extract-result "$CAP" > <正文临时文件>
# 协调者照常保存正文 → register --subtitle-file …，然后回填单条实测统计：
python3 "$SKILL_DIR/scripts/cursor_usage.py" --finalize-from-result "$CAP" \
  --markdown "$MD_PATH" --model-name "<worker 模型>" [--effort <档位>] \
  --start-epoch "$START" --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" --summary-seconds "$SUMMARY_SECS"
```

- usage 与速度窗口都来自该次生成自身：LLM 速度分母 = `duration_api_ms`（worker 全程，含其工具执行）。单条统计彼此独立，**不需要** baseline / pending job / stop hook。
- `--model-name` 写 worker 实际使用的模型（即传给 `--model` 的值）；不确定时省略并接受「不可用」，禁止把协调者自己的模型冒充 worker 模型。
- worker 失败、`--extract-result` / `--finalize-from-result` 非零退出时：该条回退到协调者自己总结 + 原 baseline/pending 路径，该批共用生成的条目按批次实测值结算（上节）。
- **单视频（整轮只此一条）无需嵌套**：本轮 stop hook 事件天然独立，维持原 `--finalize` 流程。
- Whisper 门禁、字幕流程、register/finalize 顺序均不变；嵌套只替换「正文由谁生成」这一步。

## Token / 模型（必须用脚本，禁止手写）

`$SKILL_DIR` = 本 skill 根目录（含 `SKILL.md` 的目录）。

Token baseline 必须在**当前视频的独立工作 agent generation**开始时 snapshot；多视频不得共用 baseline 文件。墙钟 `START` / `END` 与阶段秒数也必须按当前视频记录（见 `runtime_statistics.md`）。

对该视频开始实质性处理时：

```bash
python3 "$SKILL_DIR/scripts/cursor_usage.py" --doctor          # 预检；HOOK_INSTALLED 为空必须先 --ensure-hook
python3 "$SKILL_DIR/scripts/cursor_usage.py" --ensure-hook --skill-dir "$SKILL_DIR"
# --ensure-hook 必须同时安装 video_summary-cursor_usage.py 与 runtime_stats_lib.py
# 到 ~/.cursor/hooks/；缺 lib 时 stop hook 会 ModuleNotFoundError，Token 永久「不可用」。
python3 "$SKILL_DIR/scripts/cursor_usage.py" --flush-pending
python3 "$SKILL_DIR/scripts/cursor_usage.py" --snapshot-baseline "$BASELINE"   # 每条唯一
date +"START %Y-%m-%d %H:%M:%S (%s)"   # 本视频 START
```

该视频 Markdown **正文写完并已 `register --subtitle-file` 附上原文**后（阶段秒数必传；`START`/`END` 为本视频起止）：

```bash
date +"END %Y-%m-%d %H:%M:%S (%s)"   # 本视频 END
python3 "$SKILL_DIR/scripts/cursor_usage.py" --finalize \
  --baseline-file "$BASELINE" \
  --markdown "$MD_PATH" \
  --start-epoch "$START" \
  --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" \
  --summary-seconds "$SUMMARY_SECS" \
  --whisper-seconds "$WHISPER_SECS" \
  --sync-wait 8
python3 "$SKILL_DIR/scripts/cursor_usage.py" --ensure-stats \
  --baseline-file "$BASELINE" \
  --markdown "$MD_PATH" \
  --start-epoch "$START" \
  --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" \
  --summary-seconds "$SUMMARY_SECS" \
  --whisper-seconds "$WHISPER_SECS"
# 统计回填后必须再确认原文仍在（脚本只应替换「运行统计」节）：
rg -n '^## 原文字幕' "$MD_PATH"
```

## `STATS_PENDING` 是正常结果，不是失败

Cursor CLI 里整轮总结都发生在**同一个 agent turn 内**，而 Token 只有 `stop` / `afterAgentResponse` hook 才会写进 ledger——这两个 hook **要等本轮结束（即你把总结交付给用户之后）才触发**。所以：

- `--finalize` 打印 `STATS_PENDING=waiting_for_stop_hook` / `SOURCE=pending_stop_hook`、Markdown 里暂时是「Token 用量：不可用 / LLM 速度：不可用」，属于**预期行为**。
- 此时 `--ensure-stats` 同样只会返回 `ENSURE_STATS=pending`，**不要在本轮里反复重试或干等**——ledger 不可能在本轮内更新。
- `--finalize` 已写好 pending job（每个 Markdown 一个）并拉起后台 `--wait-patch`；本轮 turn 结束时 stop hook 会自动把真实数字回填进 Markdown。**交付时照常说明统计已排队回填即可，禁止手写数字。**
- 因为回填发生在 HTML 生成之后，`视频总结.html` 里会残留「不可用」。**下一轮开头**补一次即可刷新：

```bash
python3 "$SKILL_DIR/scripts/cursor_usage.py" --flush-pending   # 兜底，通常 hook 已完成
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" finalize --dir .
```

statusline 只能提供**上下文窗口占用**（`context_window_size × used_percentage`），不是 token 用量；脚本已拒绝把它当统计写入。若你在旧成稿里看到 `输出 0` + `缓存读 不可用` + `LLM 速度：0.0 tok/s` 同时出现，那就是这个旧 bug 的产物，须按下面 `--repair-from-ledger` 重修。

若本轮结束后 Token 仍为「不可用」，用（须同时传本视频 `START`/`END`，避免把后续对话算进来）：

```bash
python3 "$SKILL_DIR/scripts/cursor_usage.py" --repair-from-ledger \
  --markdown "$MD_PATH" \
  --start-epoch "$START" \
  --end-epoch "$END" \
  --download-seconds "$DOWNLOAD_SECS" \
  --summary-seconds "$SUMMARY_SECS" \
  --whisper-seconds "$WHISPER_SECS"
```

模型与推理档位：Cursor CLI 的实际模型以本轮 `stop` / `afterAgentResponse` hook 上报值为准。Cursor 桌面端 Composer 的 `default` 只代表 **Auto 路由**，不能证明 CLI 实际使用的模型；脚本会把它视为「等待 hook」，绝不把 `Auto` 写成最终模型。hook 暂未触发时模型可暂为「不可用」，回填 pending job 时会替换成真实模型（如 `Grok 4.5 high`）。仅当桌面端设置与 hook 模型完全一致时才补其 `effort`，防止把别的模型档位拼到 CLI 模型上。档位已包含在模型名里时不重复追加；`--doctor` 的 `EFFORT=` 为提示项，读不到不算降级，**不得**手改统计行补档位。

规则：

- **不得**手写或编辑 Cursor 写入的「运行统计」块（含 Token / LLM 速度行）；只能通过上述脚本回填。
- Cursor CLI 多视频回退串行时在同一 conversation / generation 内自动续跑；每条用唯一 baseline 文件和 pending job。脚本检测同 turn 多条后统一按批次口径结算（批次实测值 + 「本批 N 条共用」标注），禁止把 stop-hook 聚合事件当单条增量回填多次。
- 脚本写入的「用时」= 传入的各阶段秒数之和（有 Whisper 含转写；无则不含）；不要指望用整批 `END-START` 当总用时。
- 脚本自动写「LLM 速度」= 输出 token ÷ **token 归属窗口**（baseline snapshot → 最后一次归属用量事件），1 位小数 + `tok/s（含工具执行）`；**分母不是 `--summary-seconds`**，见 `runtime_statistics.md`。输出不可用或窗口 ≤ 0 时写「不可用」。
- 脚本自动写「Skill 版本」= `SKILL.md` frontmatter 的 `metadata.version`。改完版本号要重跑 `--ensure-hook`，否则 hook 侧回填的还是旧版本（`--doctor` 的 `SKILL_VERSION=` 可核对）。
- `--start-epoch` / `--end-epoch` 必须是该视频自身处理窗口，禁止传入整批会话起止。
- 未走 Whisper 时省略 `--whisper-seconds`。
- 漏记的阶段传 `-1`（例如 `--download-seconds -1`）以写入「不可用」。
- `--ensure-stats` 必须带与 `--finalize` 相同的阶段秒数与起止 epoch，避免二次回填抹掉拆分。
- 结束后打开 Markdown，确认 Token / LLM 速度行为具体数字或「不可用」，「用时」等于括号内阶段之和，且 **「## 原文字幕」仍存在**。
- **核验**：对比 snapshot 时打印的 `BASELINE_CONTEXT` / 会话累计——成稿「输出」必须是**增量**，不得≈整会话 `session_output_total`；也不得把字幕附录字数当成输出。可疑时用 `--repair-from-ledger` 或保持「不可用」，禁止交付虚高 LLM 速度。
- 独立工作 agent 的 pending job 按各自 conversation / Markdown 落盘并回填；子 agent 的 job 会记下 `usage_conversation_id`（父会话），由父会话的 stop hook 结算。若两个视频窗口命中同一 ledger generation 事件，脚本判为不可拆分，两条都写带「本批 N 条共用」标注的批次实测值，**不会**把整轮合计当成各自的单条增量——`--wait-patch` 现在也走同一条守卫路径（旧版本它绕过守卫，导致同批两条成稿出现一模一样、无标注的「输出 6,344」）。
- 若多次成稿 Token 均为「不可用」：先查 `~/.cursor/hooks/usage/hook-debug.log` 是否出现 `ingest_hook_error` / `ModuleNotFoundError`；再重跑 `--ensure-hook`，确认输出含 `HOOK_LIB=installed` 与 `HOOK_SMOKE=ok`。
