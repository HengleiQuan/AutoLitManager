# AutoLitManager 使用说明

## 1. 入口

项目现在只有一个正式入口：

```powershell
conda run -n ALM python main.py
```

不要再单独运行旧的 Gemini 脚本。

## 2. 运行前准备

### 2.1 安装依赖

```powershell
conda run -n ALM pip install -r requirements.txt
```

### 2.2 配置 Elsevier API Key

```powershell
set ELSEVIER_API_KEY=your_key_here
```

或者直接写进配置文件：

```yaml
elsevier_api:
  enabled: true
  api_key: your_key_here
  insttoken: ""
  max_images_per_paper: 3
```

### 2.3 登录 ChatGPT Plus

当前版本不走 OpenAI API，默认复用你的 ChatGPT Plus 网页登录态。
同时默认优先使用 `Temporary Chat`，尽量不把自动化会话写进你的普通聊天历史。

第一次使用时，先打开自动化专用 Chrome Profile：

```powershell
conda run -n ALM python main.py --open-chatgpt-profile
```

确认这个窗口里已经登录 ChatGPT，并且能正常进入对话页。

如果你想先检测自动化登录态：

```powershell
conda run -n ALM python main.py --prepare-chatgpt-login
```

## 3. 常用命令

### 3.1 查看当前已配置期刊

```powershell
conda run -n ALM python main.py --list-journals
```

### 3.2 正式运行全部启用期刊

```powershell
set ELSEVIER_API_KEY=your_key_here
conda run -n ALM python main.py
```

### 3.3 只跑部分期刊

```powershell
conda run -n ALM python main.py --journal cmame --journal ijp
```

### 3.4 临时指定新的期刊 URL

```powershell
conda run -n ALM python main.py `
  --journal-url https://www.sciencedirect.com/journal/computer-methods-in-applied-mechanics-and-engineering `
  --journal-url https://www.sciencedirect.com/journal/international-journal-of-plasticity
```

### 3.5 只补跑缺失的 ChatGPT 分析

```powershell
conda run -n ALM python main.py --retry-failed-run 20260307_111457
```

或者直接给完整目录：

```powershell
conda run -n ALM python main.py --retry-failed-run data/runs/20260307_111457
```

### 3.6 只重建报告

```powershell
conda run -n ALM python main.py --rebuild-report-run 20260307_111457
```

## 4. 主要配置

所有主要参数都在 `config/run_once.yaml`。

### 4.1 `journals`

控制要抓哪些期刊：

- `url`: 期刊主页 URL
- `alias`: 命令行别名
- `name`: 展示名称
- `enabled`: 是否默认启用
- `issue_index`: `0` 表示最新一期，`1` 表示倒数第二期
- `max_items`: 每期最多抓多少篇

### 4.2 `analysis.focus_keywords`

这是你的研究兴趣关键词，会影响：

- 论文优先级
- 报告中的重点方向统计
- ChatGPT 提示词里的关注方向说明

`analysis.skip_seen_papers`

- 默认 `true`
- 会先检查本地数据库里是否已经完整处理过该论文
- 已经跑过且结果完整的论文会自动跳过，不再重复调用后续流程

### 4.3 `chatgpt_web`

这是 ChatGPT 网页自动化主配置：

- `enabled`: 是否启用 ChatGPT 自动分析
- `chat_url`: ChatGPT 页面地址
- `history_mode`: `temporary` 表示尽量使用临时聊天，不保留在普通历史里
- `model_preference`: 首选模型或模式，默认 `thinking`
- `model_fallback_preference`: 回退模型，默认 `gpt-5.3`
- `use_images`: 是否上传论文图片
- `retry_per_batch`: 每批最大重试次数
- `user_data_dir`: 自动化 Chrome 用户数据目录
- `profile_directory`: Chrome Profile 名称
- `response_timeout_sec`: 等待结果超时秒数
- `keep_browser_open`: 任务结束后是否保留浏览器

### 4.4 `chatgpt_web.second_pass`

这是正式运行后的自动补跑策略：

- `enabled`: 是否启用第二轮补跑
- `max_papers`: 第二轮最多补跑多少篇
- `prompt_style`: 建议保留 `minimal`
- `max_abstract_chars`: 第二轮截断摘要长度
- `use_images`: 第二轮是否上传图片
- `retry_per_batch`: 第二轮重试次数

## 5. 输出内容

每次运行都会在 `data/runs/<run_id>/` 下生成：

- `effective_config.yaml`
- `resolved_journals.json`
- `papers.json`
- `crawl_stats.json`
- `manifest.json`
- `article_media/`
- `generated_diagrams/`
- `assets/`
- `report.md`
- `chatgpt_web_logs/`

发布结果：

- `reports/latest_report.md`
- `reports/latest/`
- `reports/history/<run_id>/`
- `reports/report_history.md`
