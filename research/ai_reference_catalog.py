"""Reviewed, source-linked AI model and coding-agent reference catalogue.

This catalogue stores product metadata only.  It does not scrape vendor pages,
copy vendor prose, or infer a synthetic all-purpose score.  Model scores are the
published Terminal-Bench 2.1 success rates identified in each entry and retain
the reporting source plus a harness/effort caveat.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction

from .models import CodingAgentProfile, ModelProfile

CATALOG_VERIFIED_AT = "2026-07-12"
BENCHMARK_LABEL = "Terminal-Bench 2.1 success rate (%)"


def _source(label: str, url: str, *, note: str = "") -> dict[str, str]:
    return {
        "label": label,
        "url": url,
        "verified_at": CATALOG_VERIFIED_AT,
        "note": note,
    }


MODEL_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "slug": "claude-opus-4-7",
        "name": "Claude Opus 4.7",
        "provider": "Anthropic",
        "release_date": date(2026, 4, 16),
        "context_tokens": 0,
        "input_price": Decimal("5"),
        "output_price": Decimal("25"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "Anthropic 2026 年 4 月发布的 Opus 系列模型。官方发布页给出 API "
            "标价，但未在同页给出上下文窗口和 Terminal-Bench 2.1，"
            "因此这两项保持空缺。"
        ),
        "sources": [
            _source(
                "Anthropic · Claude Opus 4.7",
                "https://www.anthropic.com/news/claude-opus-4-7",
                note="发布日与 API 标价；不将其他版本 benchmark 映射为 TB 2.1。",
            )
        ],
    },
    {
        "slug": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "release_date": date(2026, 2, 17),
        "context_tokens": 1_000_000,
        "input_price": Decimal("3"),
        "output_price": Decimal("15"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "Anthropic 的 Sonnet 4.6。上下文窗口为官方发布时披露的 beta "
            "规格，价格是每百万 token 起始标价；未披露 TB 2.1 则不填分。"
        ),
        "sources": [
            _source(
                "Anthropic · Claude Sonnet 4.6",
                "https://www.anthropic.com/news/claude-sonnet-4-6",
                note="发布日、1M beta 上下文和 $3/$15 起始标价。",
            ),
        ],
    },
    {
        "slug": "deepseek-v4",
        "name": "DeepSeek V4",
        "provider": "DeepSeek",
        "release_date": date(2026, 4, 24),
        "context_tokens": 1_000_000,
        "input_price": None,
        "output_price": None,
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "DeepSeek 官方目录中的 V4 系列，API 包含 Pro 和 Flash 两个变体。"
            "官方发布说明两者均支持 1M 上下文；单一价格与 TB 2.1 "
            "无法代表整个系列，因此留空。"
        ),
        "sources": [
            _source(
                "DeepSeek · Transparency Center",
                "https://www.deepseek.com/en/transparency/",
                note="官方模型目录与 2026-04-24 发布日。",
            ),
            _source(
                "DeepSeek API · V4 Preview Release",
                "https://api-docs.deepseek.com/news/news260424/",
                note="V4-Pro/V4-Flash 均为 1M 上下文。",
            ),
        ],
    },
    {
        "slug": "gemini-3-flash",
        "name": "Gemini 3 Flash",
        "provider": "Google",
        "release_date": date(2025, 12, 17),
        "context_tokens": 1_000_000,
        "input_price": Decimal("0.5"),
        "output_price": Decimal("3"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "Google 2025 年 12 月发布的 Gemini 3 Flash。上下文和标价"
            "取官方 Gemini API 文档；官方未在所引来源披露 TB 2.1，故不填分。"
        ),
        "sources": [
            _source(
                "Google · Gemini 3 Flash 发布",
                "https://blog.google/products-and-platforms/products/gemini/"
                "gemini-3-flash/",
                note="发布日与 $0.50/$3 每百万 token 标价。",
            ),
            _source(
                "Google AI for Developers · Gemini 3 guide",
                "https://ai.google.dev/gemini-api/docs/gemini-3",
                note="gemini-3-flash-preview 为 1M 输入上下文。",
            ),
        ],
    },
    {
        "slug": "gemini-3-pro",
        "name": "Gemini 3 Pro",
        "provider": "Google",
        "release_date": date(2025, 11, 18),
        "context_tokens": 1_000_000,
        "input_price": Decimal("2"),
        "output_price": Decimal("12"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "Google 2025 年 11 月发布的 Gemini 3 Pro。发布时公开标价"
            "为不超过 200K token 请求的 $2/$12；更长输入是另一价格档，"
            "表格所示不代表所有请求。"
        ),
        "sources": [
            _source(
                "Google · Gemini 3 Pro for developers",
                "https://blog.google/innovation-and-ai/technology/developers-tools/"
                "gemini-3-developers/",
                note="发布日、1M 上下文及 <=200K token 的 $2/$12 标价。",
            )
        ],
    },
    {
        "slug": "gpt-5-3-codex",
        "name": "GPT-5.3-Codex",
        "provider": "OpenAI",
        "release_date": date(2026, 2, 5),
        "context_tokens": 400_000,
        "input_price": Decimal("1.75"),
        "output_price": Decimal("14"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "OpenAI 针对 agentic coding 发布的 GPT-5.3-Codex。API 模型页给出"
            " 400K 上下文和每百万 token 标价；发布页不是 TB 2.1 口径，故不填分。"
        ),
        "sources": [
            _source(
                "OpenAI · GPT-5.3-Codex 发布",
                "https://openai.com/index/introducing-gpt-5-3-codex/",
                note="官方发布日与产品定位。",
            ),
            _source(
                "OpenAI API · GPT-5.3-Codex",
                "https://developers.openai.com/api/docs/models/gpt-5.3-codex",
                note="400K 上下文、$1.75/$14 每百万 token。",
            ),
        ],
    },
    {
        "slug": "gpt-5-4-mini",
        "name": "GPT-5.4 mini",
        "provider": "OpenAI",
        "release_date": date(2026, 3, 17),
        "context_tokens": 400_000,
        "input_price": Decimal("0.75"),
        "output_price": Decimal("4.5"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "OpenAI 面向高吞吐编码、computer use 与子 Agent 的小型模型。"
            "官方发布披露的 Terminal-Bench 是 2.0，不写入本目录的 2.1 字段。"
        ),
        "sources": [
            _source(
                "OpenAI · GPT-5.4 mini 与 nano",
                "https://openai.com/index/introducing-gpt-5-4-mini-and-nano/",
                note="2026-03-17 发布；40万上下文、$0.75/$4.50；只披露 TB 2.0。",
            ),
            _source(
                "OpenAI API · GPT-5.4 mini",
                "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
            ),
        ],
    },
    {
        "slug": "gpt-5-5",
        "name": "GPT-5.5",
        "provider": "OpenAI",
        "release_date": date(2026, 4, 23),
        "context_tokens": 1_000_000,
        "input_price": Decimal("5"),
        "output_price": Decimal("30"),
        "capability_score": Decimal("85.6"),
        "tier": "Frontier",
        "description": (
            "OpenAI 2026 年 4 月发布的通用旗舰模型。上下文、API 标价取官方发布页；"
            "TB 2.1 值取后续 GPT-5.6 官方同表对 GPT-5.5 的披露值。"
        ),
        "sources": [
            _source(
                "OpenAI · GPT-5.5 发布、上下文与价格",
                "https://openai.com/index/introducing-gpt-5-5/",
            ),
            _source(
                "OpenAI · GPT-5.6 同表基准",
                "https://openai.com/index/gpt-5-6/",
                note=f"{BENCHMARK_LABEL}; GPT-5.5 comparison row.",
            ),
        ],
    },
    {
        "slug": "grok-4-3",
        "name": "Grok 4.3",
        "provider": "SpaceXAI",
        "release_date": date(2026, 6, 17),
        "context_tokens": 1_000_000,
        "input_price": Decimal("1.25"),
        "output_price": Decimal("2.5"),
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "SpaceXAI 官方在 Amazon Bedrock GA 公告中披露的 Grok 4.3 规格。"
            "发布日按这个可验证公告记录，不推断更早的首次上线时间。"
        ),
        "sources": [
            _source(
                "SpaceXAI · Grok on Amazon Bedrock",
                "https://x.ai/news/grok-amazon-bedrock",
                note="2026-06-17 GA；1M 上下文、$1.25/$2.50 每百万 token。",
            )
        ],
    },
    {
        "slug": "llama-4",
        "name": "Llama 4",
        "provider": "Meta",
        "release_date": date(2025, 4, 5),
        "context_tokens": 0,
        "input_price": None,
        "output_price": None,
        "capability_score": None,
        "tier": "Open",
        "description": (
            "Meta 发布的 Llama 4 开放权重模型家族，首批包括 Scout 与 Maverick。"
            "Scout 和 Maverick 的上下文口径不同，且 Meta 不提供统一的自营 API 单价，"
            "所以家族条目不强填单一数值。"
        ),
        "sources": [
            _source(
                "Meta AI · The Llama 4 herd",
                "https://ai.meta.com/blog/llama-4-multimodal-intelligence/",
                note="2025-04-05 发布 Scout 与 Maverick；变体规格不混为单值。",
            )
        ],
    },
    {
        "slug": "mistral-large-3",
        "name": "Mistral Large 3",
        "provider": "Mistral AI",
        "release_date": date(2025, 12, 2),
        "context_tokens": 0,
        "input_price": Decimal("0.5"),
        "output_price": Decimal("1.5"),
        "capability_score": None,
        "tier": "Open",
        "description": (
            "Mistral 3 系列的开放权重旗舰模型。官方发布页给出 AI Studio "
            "标价；未在同页给出可直接存入的统一上下文口径或 TB 2.1，因此留空。"
        ),
        "sources": [
            _source(
                "Mistral AI · Introducing Mistral 3",
                "https://mistral.ai/news/mistral-3/",
                note="2025-12-02 发布；Large 3 标价 $0.50/$1.50 每百万 token。",
            )
        ],
    },
    {
        "slug": "qwen3-max",
        "name": "Qwen3-Max",
        "provider": "Qwen",
        "release_date": date(2025, 9, 24),
        "context_tokens": 0,
        "input_price": None,
        "output_price": None,
        "capability_score": None,
        "tier": "Frontier",
        "description": (
            "Qwen 发布的 Max 系列模型。发布文说明了 1M-token 长上下文"
            "训练，但当前 API 服务的上下文与价格按版本和 token 档位变化，"
            "本家族条目不将其压成一个伪精确值。"
        ),
        "sources": [
            _source(
                "Qwen · Qwen3-Max: Just Scale it",
                "https://qwen.ai/blog?id=qwen3-max",
                note="2025-09-24 官方发布；不把训练长度当作当前 API 限额。",
            )
        ],
    },
)

SUPERSEDED_MODEL_SLUGS = {
    "claude-opus-4-8",
    "gemini-3-5-flash",
    "gpt-5-6-sol",
    "grok-4-5",
}


CODING_AGENT_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "slug": "aider",
        "name": "Aider",
        "provider": "Aider-AI",
        "product_type": "Open-source CLI",
        "release_date": None,
        "price_label": "开源工具；模型费用另计",
        "capability_score": None,
        "description": (
            "面向 Git 仓库的开源终端结对编程工具。官方未提供可与本目录其他产品直接"
            "横比的统一 Agent 分数，首次发布日期也暂不推测。"
        ),
        "homepage": "https://aider.chat/",
    },
    {
        "slug": "amp",
        "name": "Amp",
        "provider": "Sourcegraph",
        "product_type": "CLI / IDE",
        "release_date": None,
        "price_label": "方案与额度见官方",
        "capability_score": None,
        "description": (
            "Sourcegraph 的 Agent 编码产品，可在终端与编辑器中执行多文件任务。"
            "首次发布日期与统一独立分数尚未核验，因此保持空缺。"
        ),
        "homepage": "https://ampcode.com/",
    },
    {
        "slug": "claude-code",
        "name": "Claude Code",
        "provider": "Anthropic",
        "product_type": "CLI / IDE",
        "release_date": date(2025, 2, 24),
        "price_label": "Claude 订阅或 API 用量计费",
        "capability_score": None,
        "description": (
            "Anthropic 的终端编码 Agent，后续扩展至 IDE 与后台任务。当前仅保存官方发布"
            "信息，统一 Agent 评测完成前不显示分数。"
        ),
        "homepage": "https://www.anthropic.com/news/claude-3-7-sonnet",
    },
    {
        "slug": "cline",
        "name": "Cline",
        "provider": "Cline Bot Inc.",
        "product_type": "Open-source IDE / CLI",
        "release_date": None,
        "price_label": "开源工具；模型费用另计",
        "capability_score": None,
        "description": (
            "可在 IDE 与终端中读取、修改和执行代码的开源 Agent。首次发布日期与统一"
            "独立评测暂未核验，不用 GitHub stars 代替能力分。"
        ),
        "homepage": "https://github.com/cline/cline",
    },
    {
        "slug": "cursor",
        "name": "Cursor",
        "provider": "Anysphere",
        "product_type": "Editor / Cloud Agent",
        "release_date": None,
        "price_label": "订阅与 Agent 限额见官方",
        "capability_score": None,
        "description": (
            "集成代码编辑、终端工具与后台 Agent 的开发环境。首次发布日期和跨产品"
            "统一评测未在所引用文档中核验，因此保持空缺。"
        ),
        "homepage": "https://docs.cursor.com/chat/overview",
    },
    {
        "slug": "devin",
        "name": "Devin",
        "provider": "Cognition",
        "product_type": "Cloud Software Agent",
        "release_date": None,
        "price_label": "方案与额度见官方",
        "capability_score": None,
        "description": (
            "Cognition 的云端软件工程 Agent。当前仅保留官方产品入口；首次发布日期、"
            "价格快照和统一独立评测留待后续核验。"
        ),
        "homepage": "https://devin.ai/",
    },
    {
        "slug": "github-copilot",
        "name": "GitHub Copilot Coding Agent",
        "provider": "GitHub",
        "product_type": "Cloud / CLI / IDE",
        "release_date": date(2025, 9, 25),
        "price_label": "GitHub Copilot 付费方案内提供",
        "capability_score": None,
        "description": (
            "GitHub 的异步编码 Agent，可接收任务并在独立环境中提交草稿 PR。目录不把"
            "厂商功能描述转换成未经同批次验证的能力分。"
        ),
        "homepage": (
            "https://github.blog/changelog/2025-09-25-copilot-coding-agent-is-now-"
            "generally-available/"
        ),
    },
    {
        "slug": "openai-codex",
        "name": "Codex",
        "provider": "OpenAI",
        "product_type": "CLI / IDE / Cloud",
        "release_date": date(2025, 5, 16),
        "price_label": "ChatGPT 套餐内含；额外额度另计",
        "capability_score": None,
        "description": (
            "可在终端、IDE 与云端执行代码任务的工程 Agent。官方没有发布可与本表其余"
            "产品直接横比的统一 Agent 分数，因此不填伪精确排名。"
        ),
        "homepage": "https://openai.com/index/introducing-codex/",
    },
    {
        "slug": "replit-agent",
        "name": "Replit Agent",
        "provider": "Replit",
        "product_type": "Hosted App-building Agent",
        "release_date": date(2024, 9, 16),
        "price_label": "免费层与订阅额度见官方",
        "capability_score": None,
        "description": (
            "在 Replit 托管环境中从需求创建、运行并部署应用的 Agent。产品已持续升级，"
            "本条以首次官方介绍为发布日期，不把后续版本特性回填为当时能力。"
        ),
        "homepage": "https://replit.com/blog/introducing-replit-agent",
    },
    {
        "slug": "windsurf",
        "name": "Windsurf",
        "provider": "Windsurf",
        "product_type": "Editor / Agent",
        "release_date": None,
        "price_label": "方案与额度见官方",
        "capability_score": None,
        "description": (
            "提供编辑器内 Agent 工作流的开发产品。首次发布日期和同批次独立分数尚未"
            "从官方来源核验，因此保留空缺。"
        ),
        "homepage": "https://windsurf.com/",
    },
    {
        "slug": "zed",
        "name": "Zed Agent",
        "provider": "Zed Industries",
        "product_type": "Editor Agent",
        "release_date": None,
        "price_label": "编辑器与模型方案见官方",
        "capability_score": None,
        "description": (
            "Zed 编辑器中的原生 Agent，可读写项目并运行终端工具。官方文档是当前功能"
            "真相源；首次发布日期和统一独立评测保持空缺。"
        ),
        "homepage": "https://zed.dev/docs/ai/zed-agent",
    },
)

SUPERSEDED_AGENT_SLUGS = {"codex", "gemini-cli", "grok-build"}


@transaction.atomic
def sync_ai_reference_catalog() -> dict[str, int]:
    model_created = 0
    model_updated = 0
    for item in MODEL_CATALOG:
        payload = dict(item)
        slug = payload.pop("slug")
        _, created = ModelProfile.objects.update_or_create(slug=slug, defaults=payload)
        model_created += int(created)
        model_updated += int(not created)
    ModelProfile.objects.filter(slug__in=SUPERSEDED_MODEL_SLUGS).delete()

    agent_created = 0
    agent_updated = 0
    for item in CODING_AGENT_CATALOG:
        payload = dict(item)
        slug = payload.pop("slug")
        _, created = CodingAgentProfile.objects.update_or_create(slug=slug, defaults=payload)
        agent_created += int(created)
        agent_updated += int(not created)
    CodingAgentProfile.objects.filter(slug__in=SUPERSEDED_AGENT_SLUGS).delete()

    return {
        "models_created": model_created,
        "models_updated": model_updated,
        "agents_created": agent_created,
        "agents_updated": agent_updated,
    }
