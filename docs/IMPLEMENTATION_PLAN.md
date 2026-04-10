# AutoLitManager 实施计划

## 目标

只运行一个入口文件 [main.py](d:\storage\project\AutoLitManager\main.py)，完成以下全流程：

1. 读取你选定的期刊主页 URL。
2. 自动解析期刊信息并定位目标期次。
3. 抓取最新一期、倒数第二期或你指定期次的全部文章。
4. 使用 Elsevier 官方 API 补全摘要、元数据和图像。
5. 自动调用 Gemini Web，优先使用 `Pro`，不可用时回退到 `Thinking`。
6. 生成中文结构化分析、研究进展总结、图像摘要和流程图。
7. 输出 Markdown 报告，并发布到 `reports/latest_report.md`。

## 当前已完成

### 1. 单入口收敛

- 用户统一入口： [main.py](d:\storage\project\AutoLitManager\main.py)
- 实际主流程： [run_once.py](d:\storage\project\AutoLitManager\run_once.py)
- 当前已支持：
  - 正式运行
  - 打开 Gemini 专用 Chrome Profile
  - 准备 Gemini 登录会话
  - CLI 选择期刊、覆盖期次、关闭 Gemini、关闭图片采集

### 2. 期刊配置

默认已配置以下期刊：

- `cmame`: Computer Methods in Applied Mechanics and Engineering
- `ijp`: International Journal of Plasticity
- `jmps`: Journal of the Mechanics and Physics of Solids
- `ijss`: International Journal of Solids and Structures

配置文件：
- [config/run_once.yaml](d:\storage\project\AutoLitManager\config\run_once.yaml)

控制方式：
- `journals[].enabled`: 是否参与本次运行
- `journals[].issue_index`: `0` 最新一期，`1` 倒数第二期
- `journals[].max_items`: 每刊最大抓取篇数
- `journals[].alias`: 命令行简称

### 3. Gemini 自动化

代码位置：
- [src/analysis/gemini_web_agent.py](d:\storage\project\AutoLitManager\src\analysis\gemini_web_agent.py)

当前能力：

- 使用专用 Chrome Profile 复用登录态
- 每次新对话优先选择 `Pro`
- `Pro` 不可用时自动回退到 `Thinking`
- 自动上传文章图像
- 自动发送 prompt
- 自动提取结构化 JSON
- 自动识别“抄 schema 的假结果”并视为失败
- 支持第二轮只补跑失败/不完整论文

### 4. 报告生成

代码位置：
- [src/report/report_builder.py](d:\storage\project\AutoLitManager\src\report\report_builder.py)
- [src/report/diagram_renderer.py](d:\storage\project\AutoLitManager\src\report\diagram_renderer.py)

当前输出：

- 最新报告： [reports/latest_report.md](d:\storage\project\AutoLitManager\reports\latest_report.md)
- 运行归档： `data/runs/<run_id>/`
- 图表、图像、自动流程图、原始 JSON 响应均保留在对应 run 目录
- 报告已包含：
  - 期刊分布图
  - 分类分布图
  - 关注关键词分布图
  - 与上次可比运行的差异对比
  - “关注方向专题”分组页
  - 按期刊精读与全量论文索引
  - 图像按“摘要/概览图 -> 模型/方法图 -> 结果/实验图”优先级展示
  - 每张图显示角色和选图理由

### 5. 最近一次验证结果

- 已验证运行： `data/runs/20260307_093316`
- 覆盖期刊： `cmame`、`ijp`、`jmps`、`ijss`
- 论文总数： `41`
- Gemini 完整更新论文数： `41`
- 报告中可用图片数： `45`
- 自动生成流程图数： `41`
- 已确认 `reports/latest_report.md` 中的图片路径会自动重写到 `reports/latest/` 目录

## 推荐运行方式

### 1. 查看已配置期刊

```powershell
conda run -n ALM python main.py --list-journals
```

### 2. 只运行部分期刊

```powershell
conda run -n ALM python main.py --journal cmame --journal jmps
```

### 3. 正式运行

```powershell
set ELSEVIER_API_KEY=your_key_here
conda run -n ALM python main.py
```

说明：
- 图像提取会优先尝试保留摘要附近概览图、方法/模型图、主要结果或实验图
- 当图注信号不够强时，会把排序提示一并交给 Gemini，让 Gemini 在总结时修正理解

### 4. 只补跑失败论文

```powershell
conda run -n ALM python main.py --retry-failed-run 20260307_093316
```

说明：
- 只读取已有 run 目录中的 `papers.json`
- 只对缺失 Gemini 关键字段的论文重新调用 Gemini
- 完成后会自动重建图、报告并重新发布 `latest_report.md`

### 5. 只重建报告

```powershell
conda run -n ALM python main.py --rebuild-report-run 20260307_093316
```

说明：
- 不重抓期刊
- 不重跑 Gemini
- 直接基于已有 run 的论文数据重建报告、趋势图和发布目录

### 4. Gemini 登录准备

普通 Chrome 打开专用 Profile：

```powershell
conda run -n ALM python main.py --open-gemini-profile
```

自动化接管前准备登录会话：

```powershell
conda run -n ALM python main.py --prepare-gemini-login
```

## 验证策略

### 第一步：配置验证

- `main.py --list-journals` 能列出所有已配置期刊
- `main.py --journal cmame --journal ijp` 能只解析指定期刊

### 第二步：抓取验证

- `resolved_journals.json` 生成成功
- 每个期刊都能抓到指定期次的文章
- `papers.json` 中存在 DOI、标题、期次信息

### 第三步：内容增强验证

- Elsevier API 能补全摘要和图像
- `article_media` 或 Elsevier API 图像目录下有实际图片文件

### 第四步：Gemini 验证

- `gemini_web_logs/prompt_*.md` 正常生成
- `gemini_web_logs/response_*.json` 或 `response_*_raw_try*.txt` 可追踪
- 第二轮补跑只针对缺失论文执行

### 第五步：报告验证

- `report.md` 能正常渲染
- 图片路径能在 `reports/latest_report.md` 中正确显示
- 自动流程图能显示在报告中
- “关注方向专题”能按 `PINN`、`constitutive model`、`multiscale`、`damage mechanics`、`surrogate model` 分组输出
- 关键词分布图 `focus_keywords.png` 已生成并发布
- 多次运行趋势章节已生成
- 历史论文数趋势图、关注方向热点变化图、高频方法变化图已生成

## 下一阶段建议

### A. 提高 Gemini 覆盖率

目标：
- 把目前的有效结构化覆盖率继续提高

建议动作：
- 对失败论文采用更短 prompt
- 对带图论文和无图论文拆分不同模板
- 对失败论文增加文本模式兜底

### B. 增加跨多次运行趋势分析

目标：
- 不只看“相比上次运行新增了什么”，还要看多次运行中的持续热点和方向变化

建议动作：
- 按 DOI 聚合多次运行结果
- 增加关键词热度变化趋势图
- 增加连续多次出现的高价值方向榜单

### C. 增加更多出版社

目标：
- 除了 Elsevier/ScienceDirect，再支持 Springer、Wiley、Taylor & Francis

建议动作：
- 扩展 URL 解析器
- 为不同出版社增加专用抓取与元数据增强模块

### D. 增加修复模式

目标：
- 当完整运行后只剩少量失败论文时，不必重跑整批期刊

建议动作：
- 增加“只补跑缺失 Gemini 字段论文”的命令
- 增加“只重建报告”的命令
- 将补跑结果合并回指定 run 目录

## 当前阶段结论

当前项目已经达到“一个入口文件即可运行”的目标，后续重点不再是入口收敛，而是：

1. 提高 Gemini 结构化成功率
2. 提高失败论文自动补跑和局部修复效率
3. 增加多次运行趋势分析和长期积累能力
