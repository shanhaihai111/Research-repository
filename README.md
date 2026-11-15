# 精神医学与意识科学前沿研究周报

英文名：**Weekly Frontiers in Psychiatry, Taste Biology and Consciousness**。

这是一个独立、可复现的 Python 项目：从 PubMed E-utilities 获取候选论文，按四个模块筛选，以 PMID/DOI/规范化标题去重，生成离线可交互 HTML、含中文字体且可搜索的 PDF、CSV/JSON 历史索引与审计日志。首期报告使用经 PMID/DOI 核验的真实论文样例；之后可使用同一程序进行在线检索。

## 快速开始

```bash
cd psychiatry_frontiers_weekly
python src/run_weekly.py --date 2026-11-15 --sample --no-email
# 在线检索（需要能访问 PubMed E-utilities）
python src/run_weekly.py --date YYYY-MM-DD --no-email
```

输出写入 `reports/YYYY-MM-DD/`；历史记录为 `data/history.jsonl`，不会覆盖旧期报告。

## 邮件与自动化

- 邮件默认禁用。复制 `config/email.example.env` 为本机私有 `config/email.env`，填入 SMTP 凭据后再用 `--send-email`；首次只会生成 `email_preview.eml`，需显式加 `--confirm-send` 才会真实投递。
- GitHub Actions 模板在 `.github/workflows/weekly.yml`。需在仓库 Secrets 配置 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`REPORT_RECIPIENT` 后才可能发送；未配置时只生成并归档报告。请先在 Actions 页面手动运行并审阅产物。
- Windows 备选方案：用任务计划程序导入 `automation/windows_task.xml`，并编辑其中 Python 与项目路径；电脑必须开机并联网。

## 科学审计边界

- 只把出版商页面/PMID/DOI 可核验的条目作为正式文献；预印本另标“早期研究”。
- 首次在线日期优先，其次 Epub/出版日期；数据库发现日期单独记录。旧文献补录会显示为“补录”，不会误称为当周发表。
- 不可合法核验的 CAS 2025 分区与最新 JCR IF 均显示“未核验”，绝不推定。请把获得授权的表格放到 `data/journal_metrics.csv` 后运行。
- 自动中文摘要为可追溯的模板化摘要；`data/curated_notes.json` 可存入人工或获授权模型复核的深入解读。程序不会凭空补全摘要未报告的细节。

## 目录

- `src/run_weekly.py`：核心命令行程序。
- `config/topics.yml`：四模块检索式、纳排关键词和颜色。
- `data/sample_verified_records.json`：首期真实文献的可审计种子数据。
- `data/journal_metrics.csv`：可选的、人工授权后导入的 CAS/JCR 指标表。
- `reports/`：每期独立 HTML、PDF、CSV、审计 JSON。
- `logs/`：每次运行的 JSON 日志。
- `automation/`：GitHub Actions 和 Windows 任务计划模板。

## 修改主题与时间

修改 `config/topics.yml` 中的查询即可调整范围。GitHub cron 使用 UTC；周一 08:30（上海时间）对应周一 00:30 UTC，夏令时不影响中国时区。Windows 任务在本机时区运行。
