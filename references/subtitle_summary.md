# 字幕、总结与保存

## 获取字幕与去重

编排见 `pipeline.md`。单条只跑：

```bash
SCRATCH="${SCRATCHPAD:-.scratchpad}/video_summary"
mkdir -p "$SCRATCH"
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" check --dir . --url "URL"
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" probe \
  --dir . --scratchpad "$SCRATCH" --url "URL"
```

`check` / `probe` 查 `output/.index.json`（A3）；`cached` 则复用已有 Markdown。`probe` 内部调用 `fetch_video.py`（元信息、原语言、Cookie 回退、人工字幕优先、原文+中文轨下载、自动字幕去重、压缩）；禁止手写 `yt-dlp --list-subs`、`--print` 或直接读取 `.srt/.vtt`。JSON 核心字段：`status`、`agent_action`、`title`、`uploader`、`duration_string`、`upload_date`、`language`、`video_id`、`platform`、`subtitle_type`、`subtitle_lang`、`transcript_file`、`subtitle_file`、`zh_subtitle_file`、`download_seconds`、`webpage_url`；`no_srt` 时可能含 `audio_url`、`hint`。

- `ok`：只读取 `transcript_file` 的 `.txt`；过大时分段读取至完整。记住 `subtitle_file` / `zh_subtitle_file` 供 `register` 附 SRT。
- `no_srt`：转入 `whisper.md`，先等待用户同意（小红书 / Apple Podcasts / 小宇宙常见）。
- `error`：如实报告错误与 `hint`（若有），不把获取失败误判为无字幕。
- `cached`：回复已有路径，除非用户要求重跑（`--force`）；重跑必须另存，绝不覆盖。

原文字幕选择固定为：原语言人工字幕 > 原语言自动字幕 > 无字幕；忽略 B 站 `danmaku`。自动字幕成稿只写「自动生成」。

非中文原视频时，`fetch_video.py` 还会尝试下载平台中文对照轨（人工中文 > 平台机翻/AI 中文，偏好 `zh-Hans` / `zh-CN` / `zh` 等）；成功时返回 `zh_subtitle_file`、`zh_subtitle_lang`、`zh_subtitle_type`。

平台与 ID（索引键 `platform:video_id`）：

| 平台 | URL 特征 | ID |
|---|---|---|
| YouTube | `watch`、`youtu.be`、`shorts` | 11 位视频 ID |
| Bilibili | `BV…`、`b23.tv` | BV 号 |
| 小红书 | `xiaohongshu.com/explore/` | 24 位 note id（去重用）；完整链接宜含 `xsec_token` |
| Apple Podcasts | `podcasts.apple.com/.../id…?i=` | 单集 `i=` 数字 ID |
| 小宇宙 | `xiaoyuzhoufm.com/episode/` | episode id |
| 长桥直播 | `longbridge.com`／`longbridge.cn/.../lives/` | lives 数字 ID |

其他平台直接说明不支持。不要再用 `grep VIDEO_ID output/*.md` 做主去重路径。

长桥直播由 `fetch_video.py` 走平台 REST 取逐字稿并自建 SRT，直接返回 `ok`（`subtitle_type=auto`，标「自动生成」）；无逐字稿时返回 `error`（非 `no_srt`），不进入 Whisper 流程。

## 总结规则

中文总结，原文金句保留原语言并附中文翻译；时间戳必须是可点击链接（优先用 `fetch_video.py` / `fetch_audio.py` 返回的 `webpage_url` 为基链）：

- YouTube：`https://www.youtube.com/watch?v=VIDEO_ID&t=秒数s`
- Bilibili：`https://www.bilibili.com/video/BVxxxx?t=秒数`
- 小红书：保留用户提供的完整 explore 链接（含 `xsec_token` 若有），时间戳用同一 URL（平台未必支持秒级跳转，仍须带可点击原链）
- Apple Podcasts：`https://podcasts.apple.com/{country}/podcast/id{COLLECTION}?i={EPISODE}#t=秒数`
- 小宇宙：`https://www.xiaoyuzhoufm.com/episode/{EID}?t=秒数`
- 长桥直播：`https://longbridge.com/zh-CN/lives/{ID}`（平台未必支持秒级跳转，仍须带可点击原链；用 `webpage_url` 为基链）

秒数按真实字幕/转写时间计算。逻辑分 3–8 段，链接指向段落起点；金句链接指向句子时间。上传日期将 `YYYYMMDD` 转成 `YYYY-MM-DD`，缺失则写「上传日期不可用」。

```markdown
> 📺 UP主/频道名 | 上传于 YYYY-MM-DD | 时长 XX:XX | 字幕：人工 / 自动生成 / 语音转写（Whisper large-v3-turbo） | 原链接

## 总结

一句话概括整个视频。

## 分段要点

**[00:00 - 03:15](链接) 段落主题**
该段内容的总结……

## 金句摘录

> "原文引用" [05:32](链接)
>（外语附中文翻译）

（最多 3 句）

## 行动建议 / 启发

1. ……
```

金句仅选信息密度高且转写有明确证据的话；行动建议最多 3 条。标题仅用于文件名，不写在正文开头。元信息行的「UP主/频道」对播客可写节目名/主播名。

## 保存与错误处理

保存前 `mkdir -p output`。文件名仅使用清理非法字符、压缩连续空格、UTF-8 不超过 180 字节的标题，加固定后缀 `_总结.md`，例如 `裸辞逃离深圳，搬到小县城后悔了吗？12万全款买房，捡漏还是接盘？_总结.md`；**不得**把视频 ID 写入文件名，也**不要**在正文单独写「视频 ID：xxx」行。去重依赖 `output/.index.json` 与正文链接自带的真实 ID，因此「原链接」与时间戳链接必须使用真实 ID，不得省略或改写；文件名冲突时追加递增序号。Markdown 末尾必须附「运行统计」（由 `runtime_statistics.md` 提供；用时一行括号拆分下载/总结，Whisper 时再含语音转写）。下载/转写秒数优先用 pipeline 返回的 `download_seconds` / `whisper_seconds`。

**「## 原文字幕」为硬性交付条件**（HTML「原文」tab 依赖它）。保存正文后先 `register` 附字幕，再回填运行统计（避免旧版统计回填误删字幕；现行 `append_srt` 会插在「运行统计」之前，`cursor_usage` 也只替换统计节本身）：

```bash
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" register \
  --dir . --markdown "output/标题_总结.md" --url "URL" --title "标题" \
  --subtitle-file "SUBTITLE_FILE" \
  --zh-subtitle-file "ZH_SUBTITLE_FILE"
# 必须核验：
rg -n '^## 原文字幕' "output/标题_总结.md"
```

- `--subtitle-file` **必填**（除非文件里已有「## 原文字幕」）。Whisper 路径换成转写产出的 `.srt`。
- 无平台中文轨时省略 `--zh-subtitle-file`；`register` 在仍无原文时会以 `status=error` / `agent_action=fix_original_srt` 失败——须修好后再交付。
- `append` 输出中 `skip` 表示小节已存在；原文 `error` **阻断交付**；仅中文轨失败时可说明缺失并继续。默认不询问、不调用 LLM 翻译。
- 「原文字幕」「中文字幕」供汇总页「原文」视图与「显示翻译」按钮使用，对话中不展示、不粘贴 SRT 正文。

回填运行统计并再次确认「## 原文字幕」仍在后：

```bash
python3 "$SKILL_DIR/scripts/summarize_pipeline.py" finalize --dir .
```

## LLM 翻译中文字幕（仅在用户明确要求时）

原语言以 `zh` 开头时无需中文对照。平台中文轨已由 `fetch_video.py` 自动尝试并在 `register` 时附好；probe 未返回 `zh_subtitle_file` 时默认直接交付，**不要询问用户是否翻译，也不要调用 LLM 翻译**。只有用户在初始请求或后续消息中明确要求翻译字幕时，才执行以下流程。用户只说“总结”“摘要”“讲解”不构成翻译授权。

1. 导出待翻译行：
   ```bash
   python3 "$SKILL_DIR/scripts/translate_srt.py" export \
     --srt "SUBTITLE_FILE" --out "$SCRATCHPAD/video_summary/VIDEO_ID.lines.txt"
   ```
2. Read 导出文件（过长分段读取），把每行「序号<TAB>原文」翻译成「序号<TAB>中文」，用 Write 保存为 `$SCRATCHPAD/video_summary/VIDEO_ID.zh.txt`。序号与制表符原样保留、行数一一对应；译文只写入文件，不在对话正文粘贴。
3. 重建中文 SRT 并追加小节：
   ```bash
   python3 "$SKILL_DIR/scripts/translate_srt.py" build \
     --srt "SUBTITLE_FILE" --lines "….zh.txt" --out "$SCRATCHPAD/video_summary/VIDEO_ID.zh.srt"
   python3 "$SKILL_DIR/scripts/append_srt.py" \
     --md "output/标题_总结.md" --srt "….zh.srt" --heading 中文字幕
   ```

`build` 报缺行/多行时按提示补齐译文文件后重跑，不得跳过校验。追加后重跑 `summarize_pipeline.py finalize --dir .` 刷新汇总页。用户未明确要求翻译时直接跳过，不将缺少中文字幕当作阻断项；任一步失败同样如实报告，不阻断交付。多视频批次也不为缺少平台中文字幕暂停或询问。

对话中：单条展示完整正文（不重复运行统计）；多条仅展示元信息、总结和文件路径。常见错误处理：

- 无字幕：进入 `whisper.md`，不回复 `no srt`。
- 小红书短链 / 风控：提示复制含 `xsec_token` 的完整地址栏链接，并确认 Chrome 已登录。
- Apple 缺少 `?i=`：说明需要单集链接。
- cookie 登录失败：提示用户先在 Chrome 登录；仍失败则报告获取失败。
- yt-dlp 缺失：提示安装。macOS 可用 `brew install yt-dlp`；Windows 可用 `winget install yt-dlp.yt-dlp`；通用：`python3 -m pip install -U yt-dlp`。详见仓库 `README.md`。
- 视频删除、地区限制：原样报告错误。
