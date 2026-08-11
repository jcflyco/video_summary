# video_summary

给 Cursor / Claude Code / Codex 用的 **视频 / 播客字幕总结** skill：粘贴链接后，优先下载原语言字幕，生成带可跳转时间戳的中文 Markdown，并维护本地 HTML 汇总页。

Agent 执行细则见 [`SKILL.md`](./SKILL.md)；本文件给人看（安装、平台、隐私）。

## 支持平台

| 平台 | 链接示例 | 说明 |
|---|---|---|
| YouTube | `youtube.com/watch`、`youtu.be`、`shorts` | 优先字幕 |
| Bilibili | `bilibili.com/video/BV…`、`b23.tv` | 优先字幕 |
| 小红书 | `xiaohongshu.com/explore/…`（宜含 `xsec_token`） | 通常无字幕；需浏览器已登录 |
| X（Twitter） | `x.com/<user>/status/<id>`、`twitter.com/…` | 通常无字幕；公开推文无需登录，受限内容回退浏览器 Cookie |
| Apple Podcasts | `podcasts.apple.com/…/id…?i=…`（须含单集 `i=`） | 通常无字幕；走音频 |
| 小宇宙 | `xiaoyuzhoufm.com/episode/…` | 通常无字幕；走公开音频直链 |
| 长桥直播 | `longbridge.com`／`longbridge.cn/…/lives/<id>` | 走平台逐字稿（公开 REST，无需登录/yt-dlp）；不支持音频/Whisper |

不支持 Spotify（DRM）。无原语言字幕时，经你同意后可本地下载**仅音频**并用 Whisper 转写再总结（长桥直播除外：无逐字稿即失败）。

## 系统要求

- **不强制 Mac**：Windows / Linux / macOS 均可。
- **有公开字幕**时：装好 Python 3 + `yt-dlp` + Agent 即可。
- **无字幕、要用 Whisper**时：
  - macOS Apple Silicon → `mlx-whisper`（MLX）
  - 其他平台（含 Intel Mac、Windows）→ `faster-whisper`
  - 另需 `ffmpeg`

## 依赖安装

### 必装（字幕路径）

```bash
# Python 3（自行安装后确认）
python3 --version

# yt-dlp
# macOS (Homebrew):
brew install yt-dlp
# Windows (winget / scoop / pip 任选其一):
winget install yt-dlp.yt-dlp
# 或:
python3 -m pip install -U yt-dlp
```

建议安装 **Chrome**（或 Safari / Firefox），并在浏览器中登录可能需要 Cookie 的站点（B 站、小红书等）。脚本通过 `yt-dlp --cookies-from-browser` **在本机读取** Cookie，不会把 Cookie 写进本仓库。

### 按需（无字幕转写）

```bash
# ffmpeg
# macOS:
brew install ffmpeg
# Windows:
winget install Gyan.FFmpeg
# 或从 https://ffmpeg.org/download.html 安装，并加入 PATH

# Apple Silicon Mac:
python3 -m pip install --user mlx-whisper

# Windows / Linux / Intel Mac:
python3 -m pip install --user faster-whisper
```

首次转写会下载模型权重，体积较大，属正常现象。

### Agent

任选其一并安装本 skill：

- [Cursor](https://cursor.com/)
- Claude Code
- Codex

安装方式因 Agent 而异（例如指向本仓库的 `dev` 分支 / 将本目录放入 skills 路径）。装好后对 Agent 说：总结下面链接，或使用 `/video_summary`（若你的环境已配置 slash command）。

## 简单用法

1. 在 Agent 对话中粘贴一个或多个支持平台的链接。
2. 有字幕：直接生成中文总结 Markdown（含时间戳、金句、行动建议）。
3. 无字幕：Agent 会先询问是否用本地 Whisper；同意后再转写并总结。
4. 成功成稿会写入工作目录的 `output/`，并重建 `视频总结.html`（本地打开；勿把含个人总结的目录推上 Git）。

## 隐私与安全

本仓库**不含**密码、API Key、Cookie 文件或你的总结稿。

| 内容 | 是否进仓库 |
|---|---|
| 流程文档与脚本（`SKILL.md` / `references/` / `scripts/`） | 是 |
| `output/` 总结 Markdown、`.scratchpad/` 临时字幕/音频 | 否（见 `.gitignore`） |
| 浏览器 Cookie / 登录态 | 否；仅运行时在本机被 yt-dlp 读取 |
| 各 Agent 的 Token / 账号密钥 | 否；用量统计只读本机 Agent 数据 |

分享本 skill 给别人时：对方使用**自己的** AI 额度与本机环境；不会消耗你的 Cookie 或密钥。`SKILL.md` 头部的 `author` 字段会随仓库公开，若不想暴露邮箱可自行删改后再分发。

## 仓库结构（给人 / 给 Agent）

```
README.md          ← 你在看的安装与说明
SKILL.md           ← Agent 路由与硬性规则
references/        ← 按需读取的详细流程
scripts/           ← pipeline / yt-dlp / Whisper / HTML 等
```

## 常见问题

- **YouTube SSL / 证书主机名不匹配**：多为本机代理（如 fake-IP）劫持；修好代理或临时直连后再试。
- **小红书 300031**：用浏览器地址栏完整链接（含 `xsec_token`），并确认 Chrome 已登录。
- **Apple Podcasts 只要播客主页**：必须换成带 `?i=` 的单集链接。
- **只要总结、不要转写**：拒绝 Whisper 即可；该条会因无字幕而跳过。
