# 编排 CLI（summarize_pipeline.py）

Agent **只负责**：读 `transcript_file` → 按 `subtitle_summary.md` 写中文总结 Markdown → 回填运行统计。  
探测、去重、音频下载、Whisper、索引、HTML 一律走本 CLI（或它包装的脚本）。

`$SKILL_DIR` = skill 根目录；工作目录通常为当前目录（含 `output/`）。

```bash
PIPE='python3 "$SKILL_DIR/scripts/summarize_pipeline.py"'
SCRATCH="${SCRATCHPAD:-.scratchpad}/video_summary"
```

## 单条

```bash
# 1) 去重（A3）
$PIPE check --dir . --url "URL"
# status=cached → 回复路径并结束；miss → 继续

# 2) 探测字幕（内含再次 check）
$PIPE probe --dir . --scratchpad "$SCRATCH" --url "URL"
# ok → agent_action=summarize，读 transcript_file
# no_srt → agent_action=ask_whisper，先问用户（见 whisper.md）
# error → 报告 error/hint

# 3) 用户同意 Whisper 后
$PIPE download-audio --dir . --scratchpad "$SCRATCH" --url "URL"
$PIPE transcribe --dir . --scratchpad "$SCRATCH" \
  --audio "$AUDIO_FILE" --video-id "$VIDEO_ID" --language "$LANG"

# 4) 保存正文后：先 register 附原文（--subtitle-file 必填），再回填运行统计
$PIPE register --dir . --markdown "output/xxx_总结.md" --url "URL" --title "标题" \
  --subtitle-file "$SUBTITLE_FILE" \
  --zh-subtitle-file "$ZH_SUBTITLE_FILE"   # 可选
# register 后核验「## 原文字幕」存在，再写运行统计
$PIPE finalize --dir .
```

`register` 成功响应含 `has_original_srt=true`；若返回 `fix_original_srt` / 无「## 原文字幕」，禁止交付。

重跑已有总结：`check` / `probe` 加 `--force`。

禁止：手写 `yt-dlp --list-subs`、直接读 `.srt/.vtt`、绕过 pipeline 自造音频下载逻辑。  
`fetch_video.py` / `fetch_audio.py` / `transcribe_whisper.py` 仅由 pipeline 或本文件列出的等价调用使用。

## 多条 + B5 流水线

```bash
$PIPE batch-init --dir . --scratchpad "$SCRATCH" --url "U1" --url "U2"
# 或 --urls-file urls.txt
$PIPE batch-probe --dir . --scratchpad "$SCRATCH"
$PIPE batch-status --scratchpad "$SCRATCH"
```

`batch-status` / `batch-probe` 返回的 `actions`：

| type | 含义 | 可否并行 |
|---|---|---|
| `ask_whisper_consent` | 硬门禁：先问清全部 `no_srt` | 确认前禁止 summarize/transcribe |
| `summarize` | 转写稿已就绪 | 多条可并行（多子 agent） |
| `download_audio` | 下一条需 Whisper 的音频 | **可与 summarize / 当前 whisper 重叠（B5）** |
| `transcribe` | 音频已就绪，可开 Whisper | **全批同一时刻最多 1 路** |

用户回复后：

```bash
$PIPE batch-set-consent --scratchpad "$SCRATCH" --consent yes --all
# 或 --video-id ID1 --video-id ID2；拒绝用 --consent no
```

协调者循环：

1. `batch-status` 看 `actions`
2. 对每个 `summarize`：派独立工作 agent（一条一个 generation / session）总结并用该条唯一 baseline 回填统计；开始时 `batch-mark-summary --status running`；完成后 `register` + 当前环境 `*_usage.py --finalize` + `batch-mark-summary --status done --markdown PATH`
3. 若有 `download_audio`：**立刻**在另一终端/后台执行  
   `$PIPE download-audio --batch --scratchpad "$SCRATCH" --url "..."`  
   不要等当前 summarize 结束（B5）
4. 若有 `transcribe` 且 `whisper_active` 为空：执行  
   `$PIPE transcribe --batch --audio ... --video-id ...`  
   完成前不要开下一条 Whisper
5. 全部完成后 `finalize --dir .`

## 索引（A3）

- 文件：`output/.index.json`（由 `register` / `rebuild-index` 维护）
- 去重键：`platform:video_id`
- 删除 HTML 侧栏条目时，`serve.py` 会 `unregister`
- 首次接入或索引可疑：`$PIPE rebuild-index --dir .`

仍可用 `index_store.py` 子命令：`lookup` / `register` / `unregister` / `rebuild` / `show`。
