"""Evidence-bound Chinese report text via a configured Chat Completions API."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

HAN = re.compile(r"[\u4e00-\u9fff]")
PLACEHOLDERS = ("待人工", "待核校", "待翻译", "this study", "the study")
SECTIONS = ("研究背景", "研究设计", "主要方法", "主要结果", "结论与局限", "科研启发")
VERSION = "academic-cn-v1"


class ContentError(RuntimeError):
    pass


def _check(value: str, minimum: int = 12) -> bool:
    return isinstance(value, str) and len(HAN.findall(value)) >= minimum and not any(
        p in value.lower() for p in PLACEHOLDERS
    )


def _request(messages: list[dict], config: dict) -> dict:
    endpoint = config["base"].rstrip("/") + "/chat/completions"
    upper_cost = (len(json.dumps(messages, ensure_ascii=False)) * config["input_per_million"] +
                  1400 * config["output_per_million"]) / 1_000_000
    if config["spent"] + upper_cost > config["budget"]:
        raise ContentError("预计模型费用超过本期预算；停止报告生成")
    payload = json.dumps({"model": config["model"], "messages": messages,
                          "temperature": 0.1, "max_tokens": 1400,
                          "response_format": {"type": "json_object"}}, ensure_ascii=False).encode()
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(endpoint, data=payload,
                                         headers={"Authorization": "Bearer " + config["key"],
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=config["timeout"]) as response:
                raw = json.load(response)
            choice = raw["choices"][0]["message"]["content"]
            result = json.loads(choice)
            usage = raw.get("usage", {})
            cost = (usage.get("prompt_tokens", 0) * config["input_per_million"] +
                    usage.get("completion_tokens", 0) * config["output_per_million"]) / 1_000_000
            config["spent"] += cost
            if config["spent"] > config["budget"]:
                raise ContentError("模型调用超过本期预算；停止报告生成")
            return result
        except ContentError:
            raise
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError) as exc:
            last = type(exc).__name__
            if attempt < 2:
                time.sleep(2 ** attempt)
    raise ContentError("中文生成接口失败：" + str(last))


def config_from_env() -> dict:
    key = os.getenv("LLM_API_KEY", "")
    base = os.getenv("LLM_API_BASE", "")
    model = os.getenv("LLM_MODEL", "")
    if not all((key, base, model)) or not base.startswith("https://"):
        raise ContentError("缺少 LLM_API_KEY、LLM_API_BASE 或 LLM_MODEL（API 地址必须为 HTTPS）")
    if not all(os.getenv(k) for k in ("LLM_INPUT_USD_PER_MILLION", "LLM_OUTPUT_USD_PER_MILLION")):
        raise ContentError("缺少模型输入或输出价格配置；无法执行费用上限")
    return {"key": key, "base": base, "model": model,
            "timeout": int(os.getenv("LLM_TIMEOUT_SECONDS", "45")),
            "budget": float(os.getenv("LLM_MAX_REPORT_USD", "2")),
            "input_per_million": float(os.getenv("LLM_INPUT_USD_PER_MILLION", "0")),
            "output_per_million": float(os.getenv("LLM_OUTPUT_USD_PER_MILLION", "0")),
            "spent": 0.0}


def _valid_article(a: dict) -> bool:
    if not _check(a.get("title_cn", ""), 3):
        return False
    if not a.get("abstract"):
        return a.get("summary_cn") == "摘要缺失，无法进行结果解读。" and a.get("detail_cn") == "摘要缺失，无法进行结果解读。"
    return (_check(a.get("summary_cn", ""), 20) and
            _check(a.get("detail_cn", ""), 60) and
            all(s in a["detail_cn"] for s in SECTIONS) and
            a["abstract"].strip() not in a["summary_cn"] + a["detail_cn"])


def enrich(records: list[dict], cache_dir: Path) -> tuple[str, dict]:
    cfg = config_from_env()
    cache_dir.mkdir(parents=True, exist_ok=True)
    for article in records:
        source = {k: article.get(k, "") for k in ("doi", "pmid", "title", "journal", "abstract", "type")}
        digest = hashlib.sha256(json.dumps([VERSION, source], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        cache = cache_dir / (digest + ".json")
        candidate = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
        if candidate is None or not _valid_article({**article, **candidate}):
            prompt = ("只根据给定英文题名和摘要撰写医学学术中文，不补充摘要未报告的样本量、结果或因果解释。"
                      "仅返回 JSON 对象，键为 title_cn、summary_cn、detail_cn。summary_cn 为1至2句。"
                      "detail_cn 必须包含研究背景、研究设计、主要方法、主要结果、结论与局限、科研启发六个中文小标题。"
                      "保留基因符号、DOI及研究类型；如摘要缺失，summary_cn 与 detail_cn 均须严格等于"
                      "“摘要缺失，无法进行结果解读。”")
            candidate = _request([{"role": "system", "content": prompt},
                                  {"role": "user", "content": json.dumps(source, ensure_ascii=False)}], cfg)
            if not _valid_article({**article, **candidate}):
                raise ContentError("中文论文卡片质量检查失败：" + (article.get("doi") or article.get("pmid") or article.get("title", ""))[:100])
            cache.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
        article.update({k: candidate[k] for k in ("title_cn", "summary_cn", "detail_cn")})
    if records:
        takeaway = _request([{"role": "system", "content": "只根据提供的中文论文提炼写完整、连贯的本期要点，最多250个汉字。仅返回JSON对象，键weekly_takeaway_cn。不可拼接或截断句子。"},
                             {"role": "user", "content": json.dumps([a["summary_cn"] for a in records], ensure_ascii=False)}], cfg).get("weekly_takeaway_cn", "")
        if not _check(takeaway, 20) or len(takeaway) > 250:
            raise ContentError("中文本期要点质量检查失败")
    else:
        takeaway = "本期未检索到符合纳入条件的新论文。"
    for article in records:
        article["weekly_takeaway_cn"] = takeaway
    return takeaway, {"model": cfg["model"], "estimated_cost_usd": round(cfg["spent"], 6), "content_version": VERSION}
