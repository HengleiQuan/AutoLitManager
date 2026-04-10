# AutoLitManager

## 项目目标

自动抓取指定期刊的最新一期论文，调用已登录的 ChatGPT 网页版生成中文总结，并输出最新报告和按期刊期次归档的历史报告。

## 主要文件

- `main.py`
- `run_once.py`
- `config/run_once.yaml`
- `config/run_once.example.yaml`

GitHub 分享时建议保留 `config/run_once.example.yaml`，本地实际使用自己的 `config/run_once.yaml`。

## 配置

### 1. 安装依赖

```powershell
conda run -n ALM pip install -r requirements.txt
```

ALM为anaconda下创建的环境名称

### 2. 配置期刊

先复制 `config/run_once.example.yaml` 为你自己的 `config/run_once.yaml`，再编辑：

```yaml
journals:
  - url: https://www.sciencedirect.com/journal/computer-methods-in-applied-mechanics-and-engineering
    alias: cmame
    name: Computer Methods in Applied Mechanics and Engineering
    enabled: true
    issue_index: 0
```

### 3. 配置关注关键词和领域

重点关键词可直接写在 `config/run_once.yaml`：

```yaml
analysis:
  focus_keywords:
    - PINN
    - constitutive model
    - multiscale
    - damage mechanics
    - surrogate model
```

分类领域写在 `config/taxonomy.yaml`：

```yaml
taxonomy:
  Multiscale Mechanics:
    keywords:
      - homogenization
      - multiscale

  Machine Learning in Mechanics:
    keywords:
      - PINN
      - surrogate model
```

也可以直接编辑 `config/keywords.yaml`：

```yaml
focus_keywords:
  - PINN
  - constitutive model
  - multiscale
```

### 4. 配置 Elsevier Key

二选一：

```powershell
set ELSEVIER_API_KEY=your_key_here
```

或写入 `config/run_once.yaml`：

```yaml
elsevier_api:
  enabled: true
  api_key: your_key_here
  insttoken: ""
  max_images_per_paper: 3
```

### 5. 配置 Chrome 专用目录

先复制你的 Chrome 用户目录到一个单独位置，例如：

```text
D:\Chrome_Automation_Profile\User Data
```

然后在 `config/run_once.yaml` 中填写：

```yaml
chatgpt_web:
  enabled: true
  chat_url: https://chatgpt.com/
  history_mode: temporary
  model_preference: gpt-5.4
  model_fallback_preference: gpt-5.3
  user_data_dir: D:\Chrome_Automation_Profile\User Data
  profile_directory: Default
  auto_close_existing_profile: true
  executable_path: C:\Program Files\Google\Chrome\Application\chrome.exe
  headless: false
```

如果图片抓取也要复用同一套 Chrome 配置，再补：

```yaml
article_media:
  enabled: false
  user_data_dir: D:\Chrome_Automation_Profile\User Data
  profile_directory: Default
  auto_close_existing_profile: true
  executable_path: C:\Program Files\Google\Chrome\Application\chrome.exe
```

### 6. 初次登录

打开专用 Chrome Profile：

```powershell
conda run -n ALM python main.py --open-chatgpt-profile
```

手动登录 ChatGPT Plus 后关闭窗口。

验证登录状态：

```powershell
conda run -n ALM python main.py --prepare-chatgpt-login
```

## 运行

查看当前期刊：

```powershell
conda run -n ALM python main.py --list-journals
```

正式运行：

```powershell
conda run -n ALM python main.py
```

重试未完成的 ChatGPT 处理：

```powershell
conda run -n ALM python run_once.py --config config/run_once.yaml --retry-failed-run <run_id>
```

重建某次运行的报告：

```powershell
conda run -n ALM python run_once.py --config config/run_once.yaml --rebuild-report-run <run_id>
```

## 输出

- 最新总览：`reports/latest_report.md`
- 最新整合报告：`reports/latest/report.md`
- 历史报告：`reports/history/<journal_id>/<issue_key>/report.md`
- 运行数据：`data/runs/<run_id>/`
- GitHub 示例输出：`examples/sample_paper_summary.md`

## 分类与更新规则

- 先按 `config/run_once.yaml` 中配置的期刊列表逐本跟踪。
- 每篇论文会按 `config/taxonomy.yaml` 的关键词规则打分类标签。
- 重点关注词来自 `config/keywords.yaml` 或 `config/run_once.yaml > analysis > focus_keywords`。
- 每次运行会先检查当前抓到的 `issue_key` 是否已经存在于 `reports/history/_registry.json`。
- 如果是新卷或新期，就生成新报告并归档到 `reports/history/<journal_id>/<issue_key>/`。
- 如果该期刊没有新卷，就不重复更新历史报告，最新总览中会显示该期刊本次无更新。
