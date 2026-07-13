"""Seed deterministic, clean-room demonstration data without network access."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from research.calculations import net_liquidity, yield_curve_spreads
from research.models import (
    CodingAgentProfile,
    Company,
    DashboardSnapshot,
    FedDocument,
    FinancialFact,
    FundLetter,
    GeneratedAnalysis,
    GitHubProject,
    GlossaryTerm,
    IngestionRun,
    Instrument,
    MetricSnapshot,
    ModelProfile,
    NewsItem,
    Observation,
    OptionContract,
    ResearchMention,
    SeriesDefinition,
    Source,
    SourceLicense,
    SupplyChainEdge,
    SupplyChainNode,
    Thesis,
)

ANCHOR_DATE = date(2026, 7, 11)
SEED_NAMESPACE = uuid.UUID("6ee7fc75-b90d-4778-8b2b-98e733d7350f")


def _uuid(name: str) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, name)


def _aware(day: date, hour: int = 16) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=UTC)


def _decimal(value: float, places: str = "0.00000001") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places))


class Command(BaseCommand):
    help = "Seed deterministic clean-room demo data (offline and idempotent)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--allow-demo-data",
            action="store_true",
            help="Acknowledge that synthetic records will be written (development/test only).",
        )
        parser.add_argument(
            "--anchor-date",
            type=date.fromisoformat,
            default=ANCHOR_DATE,
            help="Latest demo date in YYYY-MM-DD form (default: 2026-07-11).",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        if not options["allow_demo_data"]:
            raise CommandError(
                "seed_platform writes synthetic demonstration records; rerun with "
                "--allow-demo-data only in an isolated development or test database."
            )
        anchor: date = options["anchor_date"]
        self.sources = self._seed_sources()
        run = self._seed_ingestion_run(anchor)
        self._seed_series_and_observations(anchor, run)
        instruments = self._seed_instruments_and_observations(anchor, run)
        self._seed_options(anchor, instruments["SPY"])
        self._seed_snapshots(anchor, run)
        self._seed_theses(anchor)
        self._seed_content(anchor)
        nodes = self._seed_nodes()
        self._seed_companies(anchor, nodes)
        self._seed_edges(nodes)
        self._seed_model_profiles(anchor)
        self._seed_coding_agents(anchor)
        self._seed_github_projects(anchor)
        self._seed_glossary()
        self._report_counts()

    def _seed_sources(self) -> dict[str, Source]:
        catalog = {
            "fred": (
                "Federal Reserve Economic Data",
                "https://fred.stlouisfed.org/",
                "aggregator",
                Source.LicenseStatus.REVIEW,
                False,
            ),
            "federal-reserve": (
                "Federal Reserve Board",
                "https://www.federalreserve.gov/",
                "official",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "ny-fed": (
                "Federal Reserve Bank of New York",
                "https://www.newyorkfed.org/markets",
                "official",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "treasury": (
                "U.S. Treasury Fiscal Data",
                "https://fiscaldata.treasury.gov/",
                "official",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "bls": (
                "U.S. Bureau of Labor Statistics",
                "https://www.bls.gov/",
                "official",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "sec": (
                "SEC EDGAR",
                "https://www.sec.gov/edgar",
                "official",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "cftc": (
                "CFTC Commitments of Traders",
                "https://www.cftc.gov/MarketReports/CommitmentsofTraders/",
                "official",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "github": (
                "GitHub REST API",
                "https://docs.github.com/en/rest",
                "public-api",
                Source.LicenseStatus.REVIEW,
                False,
            ),
            "okx": (
                "OKX Public Market Data",
                "https://www.okx.com/docs-v5/",
                "public-api",
                Source.LicenseStatus.REVIEW,
                False,
            ),
            "deribit": (
                "Deribit Public API",
                "https://docs.deribit.com/",
                "public-api",
                Source.LicenseStatus.REVIEW,
                False,
            ),
            "demo-market": (
                "Clean-room Demonstration Market Data",
                "https://example.com/data-policy",
                "synthetic",
                Source.LicenseStatus.OPEN,
                True,
            ),
            "internal": (
                "Atlas Macro Derived Data",
                "https://example.com/methodology",
                "derived",
                Source.LicenseStatus.OPEN,
                True,
            ),
        }
        result: dict[str, Source] = {}
        for key, (name, homepage, kind, license_status, redistributable) in catalog.items():
            source, _ = Source.objects.update_or_create(
                key=key,
                defaults={
                    "name": name,
                    "homepage": homepage,
                    "kind": kind,
                    "license_status": license_status,
                    "license_scope": (
                        "Synthetic clean-room demonstration values"
                        if key == "demo-market"
                        else "Metadata and attributed derived display; upstream terms apply"
                    ),
                    "redistribution_allowed": redistributable,
                    "attribution": name,
                },
            )
            SourceLicense.objects.update_or_create(
                source=source,
                reviewed_by="clean-room seed policy",
                defaults={
                    "is_current": False,
                    "status": license_status,
                    "scope": source.license_scope,
                    "terms_url": homepage,
                    "redistribution_allowed": redistributable,
                    "notes": "Production publication still requires a current terms review.",
                },
            )
            result[key] = source
        return result

    def _seed_ingestion_run(self, anchor: date) -> IngestionRun:
        batch_id = _uuid(f"seed-batch:{anchor.isoformat()}")
        run, _ = IngestionRun.objects.update_or_create(
            batch_id=batch_id,
            defaults={
                "source": self.sources["demo-market"],
                "dataset": "clean-room-demo-seed",
                "started_at": _aware(anchor, 15),
                "completed_at": _aware(anchor, 16),
                "status": IngestionRun.Status.SUCCESS,
                "row_count": 0,
                "error": "",
                "metadata": {"offline": True, "synthetic": True, "schema_version": 1},
            },
        )
        return run

    def _seed_series_and_observations(self, anchor: date, run: IngestionRun) -> None:
        definitions = {
            "DGS3MO": ("3-Month Treasury Yield", "%", 5.20, -0.002),
            "DGS2": ("2-Year Treasury Yield", "%", 4.82, -0.0015),
            "DGS5": ("5-Year Treasury Yield", "%", 4.44, -0.001),
            "DGS10": ("10-Year Treasury Yield", "%", 4.51, 0.0005),
            "DGS30": ("30-Year Treasury Yield", "%", 4.78, 0.0008),
            "SOFR": ("Secured Overnight Financing Rate", "%", 4.34, -0.0008),
            "IORB": ("Interest on Reserve Balances", "%", 4.40, 0.0),
            "WALCL": ("Federal Reserve Total Assets", "USD million", 6_720_000, -850),
            "RRPONTSYD": (
                "Overnight Reverse Repurchase Agreements",
                "USD billion",
                118,
                -0.35,
            ),
            "WTREGEN": ("Treasury General Account", "USD million", 745_000, 320),
            "WRESBAL": ("Reserve Balances", "USD million", 3_285_000, 120),
            "VIXCLS": ("CBOE Volatility Index", "index", 16.4, -0.004),
            "BAMLC0A0CM": ("US Corporate Master OAS", "%", 0.86, -0.0004),
            "BAMLH0A0HYM2": ("US High Yield Master II OAS", "%", 2.94, -0.0008),
            "NFCI": ("Chicago Fed National Financial Conditions Index", "index", -0.48, 0.0002),
            "CPIAUCSL": ("Consumer Price Index", "index", 322.6, 0.018),
            "UNRATE": ("Unemployment Rate", "%", 4.1, 0.001),
            "GDPC1": ("Real Gross Domestic Product", "USD billion", 23_580, 1.4),
        }
        for key, (name, unit, latest, slope) in definitions.items():
            series, _ = SeriesDefinition.objects.update_or_create(
                key=key,
                defaults={
                    "name": name,
                    "unit": unit,
                    "frequency": "daily" if key.startswith("DGS") else "weekly",
                    "source": self.sources["fred"],
                    "description": (
                        "Synthetic demonstration series; replace through FRED ingestion."
                    ),
                },
            )
            for offset in range(180):
                day = anchor - timedelta(days=179 - offset)
                wave = math.sin(offset / 11.0) * (
                    0.06 if abs(latest) < 100 else abs(latest) * 0.002
                )
                value = latest + slope * (offset - 179) + wave
                value_date = _aware(day)
                Observation.objects.update_or_create(
                    series=series,
                    instrument=None,
                    value_date=value_date,
                    source=self.sources["demo-market"],
                    defaults={
                        "value": _decimal(value),
                        "as_of": value_date,
                        "fetched_at": _aware(anchor, 17),
                        "batch_id": run.batch_id,
                        "fallback_source": self.sources["fred"],
                        "quality_status": Observation.Quality.ESTIMATED,
                        "metadata": {"demo": True, "synthetic": True, "series_key": key},
                    },
                )

    def _seed_instruments_and_observations(
        self, anchor: date, run: IngestionRun
    ) -> dict[str, Instrument]:
        rows = [
            ("SPY", "S&P 500 ETF", "equity", "NYSE Arca", 632.40),
            ("QQQ", "Nasdaq 100 ETF", "equity", "Nasdaq", 565.80),
            ("IWM", "Russell 2000 ETF", "equity", "NYSE Arca", 224.10),
            ("TLT", "20+ Year Treasury ETF", "bond", "Nasdaq", 88.65),
            ("HYG", "High Yield Corporate Bond ETF", "credit", "NYSE Arca", 79.20),
            ("LQD", "Investment Grade Corporate Bond ETF", "credit", "NYSE Arca", 109.35),
            ("GLD", "Gold ETF", "commodity", "NYSE Arca", 310.80),
            ("IEF", "7-10 Year Treasury ETF", "bond", "Nasdaq", 94.85),
            ("CL=F", "WTI Crude Oil Future", "commodity", "NYMEX", 73.40),
            ("DX-Y.NYB", "U.S. Dollar Index", "fx", "ICE", 97.60),
            ("EURUSD=X", "Euro / U.S. Dollar", "fx", "OTC", 1.17),
            ("USDJPY=X", "U.S. Dollar / Japanese Yen", "fx", "OTC", 147.80),
            ("BTC-USD", "Bitcoin / U.S. Dollar", "crypto", "Composite", 118_200.0),
            ("ETH-USD", "Ether / U.S. Dollar", "crypto", "Composite", 3_520.0),
            ("ES", "E-mini S&P 500 Future", "future", "CME", 6_355.0),
            ("NQ", "E-mini Nasdaq 100 Future", "future", "CME", 23_450.0),
            ("VIX", "CBOE Volatility Index", "volatility", "CBOE", 16.4),
        ]
        instruments: dict[str, Instrument] = {}
        for index, (symbol, name, asset_class, exchange, latest) in enumerate(rows):
            instrument, _ = Instrument.objects.update_or_create(
                symbol=symbol,
                defaults={
                    "name": name,
                    "asset_class": asset_class,
                    "exchange": exchange,
                    "currency": "USD",
                    "metadata": {"demo": True, "delayed": True},
                },
            )
            instruments[symbol] = instrument
            volatility = 0.007 + index * 0.00025
            for offset in range(120):
                day = anchor - timedelta(days=119 - offset)
                trend = 1.0 + (offset - 119) * (0.00045 + index * 0.00001)
                cycle = 1.0 + math.sin((offset + index) / 8.0) * volatility
                value = max(0.0001, latest * trend * cycle)
                value_date = _aware(day, 20)
                Observation.objects.update_or_create(
                    instrument=instrument,
                    series=None,
                    value_date=value_date,
                    source=self.sources["demo-market"],
                    defaults={
                        "value": _decimal(value),
                        "as_of": value_date,
                        "fetched_at": _aware(anchor, 21),
                        "batch_id": run.batch_id,
                        "quality_status": Observation.Quality.ESTIMATED,
                        "metadata": {"demo": True, "synthetic": True},
                    },
                )
        return instruments

    def _seed_options(self, anchor: date, underlying: Instrument) -> None:
        expiry = anchor + timedelta(days=35)
        source = self.sources["demo-market"]
        as_of = _aware(anchor, 20)
        for strike in range(560, 711, 10):
            distance = abs(strike - 630)
            iv = 0.17 + distance / 1600
            for kind in ("call", "put"):
                call_delta = max(0.04, min(0.96, 0.5 - (strike - 630) / 180))
                delta = call_delta if kind == "call" else call_delta - 1
                gamma = max(0.0002, 0.0105 - distance / 11_000)
                oi = max(250, 6_200 - distance * 45 + (700 if strike % 50 == 0 else 0))
                OptionContract.objects.update_or_create(
                    instrument=underlying,
                    expiry=expiry,
                    strike=Decimal(str(strike)),
                    option_type=kind,
                    defaults={
                        "open_interest": oi,
                        "volume": max(40, oi // 5),
                        "implied_volatility": _decimal(iv, "0.00001"),
                        "delta": _decimal(delta, "0.00001"),
                        "gamma": _decimal(gamma, "0.00000001"),
                        "as_of": as_of,
                        "source": source,
                        "quality_status": Observation.Quality.ESTIMATED,
                    },
                )

    def _seed_snapshots(self, anchor: date, run: IngestionRun) -> None:
        source = self.sources["internal"]
        as_of = _aware(anchor, 21)
        metric_rows = [
            ("sp500", "S&P 500", 632.40, "+0.42%", 0.42, "USD"),
            ("us10y", "10Y Treasury", 4.51, "4.51%", 0.03, "%"),
            ("dollar", "Dollar Index", 97.60, "97.60", -0.28, "index"),
            ("gold", "Gold", 310.80, "$310.80", 0.65, "USD"),
            ("bitcoin", "Bitcoin", 118200, "$118,200", 1.35, "USD"),
            ("vix", "VIX", 16.40, "16.40", -2.1, "index"),
            ("hy-oas", "HY OAS Proxy", 2.94, "294 bp", -0.02, "%"),
            ("fed-assets", "Fed Assets", 6_720_000, "$6.72T", -0.1, "USDm"),
            ("tga", "Treasury General Account", 745_000, "$745B", 0.2, "USDm"),
            ("rrp", "Reverse Repo", 118_000, "$118B", -1.0, "USDm"),
            ("unemployment", "Unemployment", 4.10, "4.1%", 0.0, "%"),
            ("cpi", "CPI YoY", 2.80, "2.8%", -0.1, "%"),
        ]
        for key, label, value, display, change, unit in metric_rows:
            MetricSnapshot.objects.update_or_create(
                key=key,
                batch_id=run.batch_id,
                defaults={
                    "label": label,
                    "value": _decimal(value),
                    "display_value": display,
                    "change": _decimal(change, "0.000001"),
                    "unit": unit,
                    "value_date": as_of,
                    "as_of": as_of,
                    "fetched_at": as_of,
                    "source": source,
                    "fallback_source": self.sources["demo-market"],
                    "quality_status": Observation.Quality.ESTIMATED,
                    "license_scope": "Synthetic clean-room demonstration data",
                    "metadata": {"demo": True},
                },
            )

        spreads = yield_curve_spreads(
            {"3m": 5.20, "2y": 4.82, "5y": 4.44, "10y": 4.51, "30y": 4.78}
        )
        dashboard_rows = [
            ("home", "今日宏观总览"),
            ("assets", "大类资产"),
            ("rates", "利率与曲线"),
            ("liquidity", "流动性传导"),
            ("economy", "经济数据"),
            ("volatility", "波动率全景"),
            ("credit", "信用压力"),
            ("crypto", "加密衍生品"),
            ("research", "机构研究"),
            ("ai-industry", "AI 产业链"),
        ]
        for key, title in dashboard_rows:
            DashboardSnapshot.objects.update_or_create(
                key=key,
                batch_id=run.batch_id,
                defaults={
                    "title": title,
                    "as_of": as_of,
                    "quality_status": Observation.Quality.ESTIMATED,
                    "summary": "本页展示离线生成的清洁室演示数据，不构成投资建议。",
                    "data": {
                        "demo": True,
                        "net_liquidity_usd_m": net_liquidity(6_720_000, 745_000, 118_000),
                        "spreads": spreads,
                    },
                    "source": source,
                    "is_published": True,
                },
            )

    def _seed_theses(self, anchor: date) -> None:
        regimes = ["增长与流动性共振", "通胀降温", "防御轮动", "风险偏好修复"]
        statuses = [Thesis.Status.HIT, Thesis.Status.PARTIAL, Thesis.Status.MISSED]
        for index in range(86):
            day = anchor - timedelta(days=index)
            status = Thesis.Status.PENDING if index == 0 else statuses[index % len(statuses)]
            Thesis.objects.update_or_create(
                date=day,
                defaults={
                    "regime": regimes[index % len(regimes)],
                    "confidence": ["高", "中", "中高"][index % 3],
                    "summary": (
                        f"演示日报 {day.isoformat()}：跨资产信号仍需收益率曲线与信用利差共同确认。"
                    ),
                    "evidence": [
                        {"label": "10Y 收益率", "value": f"{4.35 + index % 9 * 0.03:.2f}%"},
                        {"label": "VIX", "value": f"{15.2 + index % 7 * 0.4:.1f}"},
                        {"label": "HY OAS 代理", "value": f"{285 + index % 12 * 4}bp"},
                    ],
                    "triggers": ["美债曲线斜率连续两日改善", "信用利差不扩张"],
                    "invalidation": "若信用利差与隐含波动率同时突破 90 日分位，则该判断失效。",
                    "status": status,
                    "hit_rate": (
                        None if status == Thesis.Status.PENDING else Decimal(str(55 + index % 41))
                    ),
                    "simulated_return": None
                    if status == Thesis.Status.PENDING
                    else Decimal(str(round(-1.2 + (index % 17) * 0.23, 3))),
                    "review_status": Thesis.ReviewStatus.DRAFT,
                    "reviewed_by": "",
                    "reviewed_at": None,
                    "publication_fingerprint": "",
                    "is_published": False,
                    "published_at": None,
                    "source_snapshot": None,
                },
            )

    def _seed_content(self, anchor: date) -> None:
        categories = ["宏观", "利率", "AI 产业", "半导体", "信用", "加密资产"]
        sentiments = ["偏多", "中性", "谨慎"]
        for index in range(96):
            url = f"https://example.com/clean-room/news-{index + 1:03d}"
            NewsItem.objects.update_or_create(
                source_url=url,
                defaults={
                    "title": f"清洁室演示资讯 {index + 1:03d}",
                    "original_title": f"Clean-room demonstration brief {index + 1:03d}",
                    "summary": "用于验证资讯筛选、标签和时间线的原创演示摘要。",
                    "source_name": "Atlas Demo Wire",
                    "category": categories[index % len(categories)],
                    "published_at": _aware(anchor - timedelta(days=index // 4), 8 + index % 4),
                    "tickers": [["SPY"], ["QQQ", "NVDA"], ["TLT"], ["BTC"]][index % 4],
                    "themes": [categories[index % len(categories)]],
                    "sentiment": sentiments[index % len(sentiments)],
                    "relevance": 60 + index % 40,
                    "license_status": "synthetic",
                },
            )

        banks = ["Atlas Research", "Northstar Macro", "Blue Harbor", "Signal Ridge"]
        for index in range(48):
            url = f"https://example.com/clean-room/research-{index + 1:03d}"
            ResearchMention.objects.update_or_create(
                source_url=url,
                defaults={
                    "bank": banks[index % len(banks)],
                    "title": f"机构观点演示 {index + 1:03d}",
                    "summary": "原创演示摘要，仅用于展示多空观点矩阵与来源链接。",
                    "category": categories[index % len(categories)],
                    "stance": sentiments[index % len(sentiments)],
                    "importance": 5 + index % 5,
                    "published_at": _aware(anchor - timedelta(days=index // 2), 9),
                    "review_status": "reviewed" if index % 3 == 0 else "ai",
                },
            )

        strategies = ["宏观", "价值", "成长", "多策略", "信用"]
        stances = ["积极", "中性", "谨慎"]
        for index in range(267):
            fund_name = f"Atlas Clean-Room Fund {index + 1:03d}"
            published = anchor - timedelta(days=index % 730)
            FundLetter.objects.update_or_create(
                fund_name=fund_name,
                defaults={
                    "fund_name_en": f"Atlas Clean-Room Fund {index + 1:03d}",
                    "manager": f"Demo Manager {index % 23 + 1:02d}",
                    "quarter": f"{published.year}Q{(published.month - 1) // 3 + 1}",
                    "strategy": strategies[index % len(strategies)],
                    "stance": stances[index % len(stances)],
                    "aum_usd_m": Decimal(str(250 + index * 7.5)),
                    "summary": "原创演示基金信摘要，不包含任何受限正文或 PDF。",
                    "key_points": ["重视现金流质量", "关注估值与流动性", "保留证伪条件"],
                    "asset_views": [
                        {"asset": "股票", "view": stances[index % len(stances)]},
                        {"asset": "债券", "view": stances[(index + 1) % len(stances)]},
                    ],
                    "original_url": f"https://example.com/clean-room/fund-letter-{index + 1:03d}",
                    "source_label": "合成演示来源",
                    "license_status": "synthetic",
                    "published_at": published,
                },
            )

        speakers = ["FOMC", "Governor A", "President B", "Vice Chair C"]
        doc_types = [
            FedDocument.DocumentType.STATEMENT,
            FedDocument.DocumentType.SPEECH,
            FedDocument.DocumentType.NEWS,
        ]
        for index in range(36):
            FedDocument.objects.update_or_create(
                slug=f"clean-room-fed-document-{index + 1:03d}",
                defaults={
                    "document_type": doc_types[index % len(doc_types)],
                    "title": f"美联储文档演示 {index + 1:03d}",
                    "speaker": speakers[index % len(speakers)],
                    "summary": "用于验证声明、演讲和公告页面的原创演示内容。",
                    "key_points": ["数据依赖", "通胀风险", "就业平衡"],
                    "published_at": _aware(anchor - timedelta(days=index * 5), 18),
                    "hawkish_score": -4 + index % 9,
                    "original_url": f"https://example.com/clean-room/fed-{index + 1:03d}",
                },
            )

        for index in range(20):
            day = anchor - timedelta(days=index)
            GeneratedAnalysis.objects.update_or_create(
                slug=f"clean-room-analysis-{day.isoformat()}",
                defaults={
                    "title": f"{day.isoformat()} 跨资产数据摘要",
                    "body": "本摘要由离线模板生成，仅用于验证证据链和审核状态。",
                    "model_name": "clean-room-template",
                    "prompt_version": "demo-v1",
                    "generated_at": _aware(day, 22),
                    "review_status": GeneratedAnalysis.ReviewStatus.REVIEWED,
                    "evidence": [{"source": "demo-market", "batch": f"demo-{day}"}],
                    "data_as_of": _aware(day, 21),
                    "stale": False,
                },
            )

    def _seed_nodes(self) -> list[SupplyChainNode]:
        layers = [
            "能源与电力",
            "晶圆与制造",
            "算力芯片",
            "光通信与网络",
            "服务器与散热",
            "数据中心",
            "云与模型平台",
            "企业软件",
            "AI 应用",
        ]
        quadrants = ["核心", "成长", "周期", "可选", "观察"]
        featured_nodes = {
            6: ("advanced-nodes", "先进制程"),
            7: ("cowos", "CoWoS 先进封装"),
            8: ("hbm", "HBM 高带宽存储"),
            11: ("gpu", "GPU 加速器"),
            12: ("ai-asic", "AI ASIC"),
            16: ("optical-modules", "高速光模块"),
            21: ("liquid-cooling", "液冷系统"),
            31: ("cloud-providers", "云服务商"),
        }
        nodes: list[SupplyChainNode] = []
        for layer_index, layer in enumerate(layers):
            for node_index in range(5):
                number = layer_index * 5 + node_index + 1
                slug, name = featured_nodes.get(
                    number, (f"clean-room-node-{number:02d}", f"{layer}节点 {node_index + 1}")
                )
                node, _ = SupplyChainNode.objects.update_or_create(
                    slug=slug,
                    defaults={
                        "name": name,
                        "layer": layer,
                        "description": f"{layer}中的清洁室演示节点，用于表达产业链依赖。",
                        "thesis": "产业需求、产能约束和资本开支需结合公司原始披露验证。",
                        "quadrant": quadrants[node_index],
                        "narrative_score": Decimal(str(62 + number % 35)),
                        "revenue_growth": Decimal(str(8 + number % 28)),
                        "gross_margin": Decimal(str(22 + number % 43)),
                        "median_pe": Decimal(str(14 + number % 31)),
                        "median_ps": Decimal(str(2 + number % 12)),
                        "market_cap_usd_m": Decimal(str(2_500 + number * 1_750)),
                        "source_note": "合成演示数据；上线前须替换为可追溯来源",
                    },
                )
                nodes.append(node)
        return nodes

    def _seed_companies(self, anchor: date, nodes: list[SupplyChainNode]) -> None:
        source = self.sources["demo-market"]
        for index in range(219):
            number = index + 1
            company, _ = Company.objects.update_or_create(
                slug=f"clean-room-company-{number:03d}",
                defaults={
                    "name": f"产业链演示公司 {number:03d}",
                    "name_en": f"Clean-Room Company {number:03d}",
                    "ticker": f"D{number:03d}",
                    "exchange": ["NASDAQ", "NYSE", "HKEX", "SSE"][index % 4],
                    "country": ["美国", "中国", "日本", "韩国", "荷兰"][index % 5],
                    "currency": "USD",
                    "primary_node": nodes[index % len(nodes)],
                    "description": "原创合成的公司档案，用于展示产业链筛选和详情页。",
                    "business": "演示性业务描述；不代表任何真实发行人。",
                    "price": Decimal(str(18 + number * 1.17)),
                    "market_cap_usd_m": Decimal(str(900 + number * 415)),
                    "return_1m": Decimal(str(-12 + number % 29)),
                    "return_6m": Decimal(str(-20 + number % 67)),
                    "revenue_growth": Decimal(str(4 + number % 42)),
                    "gross_margin": Decimal(str(18 + number % 61)),
                    "pe": Decimal(str(11 + number % 48)),
                    "ps": Decimal(str(1 + number % 17)),
                    "rating": ["积极", "中性", "谨慎"][index % 3],
                    "quality_grade": ["A", "A-", "B+", "B"][index % 4],
                    "data_source_note": "合成演示数据",
                    "investor_relations_url": f"https://example.com/clean-room/company-{number:03d}",
                    "data_as_of": anchor,
                },
            )
            for fiscal_year in (2023, 2024, 2025):
                age = fiscal_year - 2023
                revenue = 500 + number * 22 + age * (45 + number % 17)
                FinancialFact.objects.update_or_create(
                    company=company,
                    fiscal_year=fiscal_year,
                    defaults={
                        "revenue_usd_m": Decimal(str(revenue)),
                        "revenue_growth": Decimal(str(7 + number % 23 + age)),
                        "gross_margin": Decimal(str(24 + number % 47 + age * 0.4)),
                        "net_income_usd_m": Decimal(
                            str(round(revenue * (0.08 + number % 7 / 100), 2))
                        ),
                        "operating_cash_flow_usd_m": Decimal(
                            str(round(revenue * (0.12 + number % 5 / 100), 2))
                        ),
                        "source": source,
                        "filed_at": date(fiscal_year + 1, 3, 15),
                    },
                )

    def _seed_edges(self, nodes: list[SupplyChainNode]) -> None:
        for index, node in enumerate(nodes):
            target = nodes[(index + 5) % len(nodes)]
            SupplyChainEdge.objects.update_or_create(
                source_node=node,
                target_node=target,
                relation="产品与容量依赖",
                defaults={
                    "confidence": Decimal("0.75"),
                    "evidence_url": f"https://example.com/clean-room/edge-{index + 1:02d}",
                    "reviewed": True,
                },
            )
            if index < 36:
                secondary = nodes[(index + 9) % len(nodes)]
                SupplyChainEdge.objects.update_or_create(
                    source_node=node,
                    target_node=secondary,
                    relation="二级产能传导",
                    defaults={
                        "confidence": Decimal("0.65"),
                        "evidence_url": (
                            f"https://example.com/clean-room/secondary-edge-{index + 1:02d}"
                        ),
                        "reviewed": True,
                    },
                )

    def _seed_model_profiles(self, anchor: date) -> None:
        for index in range(12):
            number = index + 1
            ModelProfile.objects.update_or_create(
                slug=f"clean-room-model-{number:02d}",
                defaults={
                    "name": f"Frontier Demo Model {number:02d}",
                    "provider": f"Demo Lab {index % 5 + 1}",
                    "release_date": anchor - timedelta(days=index * 28),
                    "context_tokens": 32_000 * (2 ** (index % 4)),
                    "input_price": Decimal(str(round(0.3 + index * 0.42, 3))),
                    "output_price": Decimal(str(round(1.1 + index * 1.15, 3))),
                    "capability_score": Decimal(str(92 - index * 2.15)),
                    "tier": ["T0", "T1", "T2"][min(index // 4, 2)],
                    "description": "合成演示模型档案，价格与能力分数不对应真实产品。",
                    "sources": [
                        {"label": "Synthetic demo", "url": "https://example.com/methodology"}
                    ],
                },
            )

    def _seed_coding_agents(self, anchor: date) -> None:
        for index in range(11):
            number = index + 1
            CodingAgentProfile.objects.update_or_create(
                slug=f"clean-room-coding-agent-{number:02d}",
                defaults={
                    "name": f"Coding Agent Demo {number:02d}",
                    "provider": f"Demo Studio {index % 4 + 1}",
                    "product_type": ["IDE", "CLI", "Cloud", "Review"][index % 4],
                    "release_date": anchor - timedelta(days=index * 35),
                    "price_label": ["免费", "$20/月", "按用量", "企业版"][index % 4],
                    "capability_score": Decimal(str(91 - index * 2.4)),
                    "description": "合成演示 Coding Agent 档案，用于榜单和详情页验收。",
                    "homepage": f"https://example.com/clean-room/coding-agent-{number:02d}",
                },
            )

    def _seed_github_projects(self, anchor: date) -> None:
        categories = ["Agent", "RAG", "开发工具", "多模态", "数据基础设施"]
        for index in range(45):
            number = index + 1
            GitHubProject.objects.update_or_create(
                repo=f"atlas-clean-room/demo-project-{number:02d}",
                defaults={
                    "category": categories[index % len(categories)],
                    "description": "合成演示仓库元数据，不代表真实 GitHub 仓库。",
                    "stars": 850 + number * 1_137,
                    "stars_7d": 12 + number * 17,
                    "forks": 80 + number * 31,
                    "open_issues": 5 + number % 37,
                    "pushed_at": _aware(anchor - timedelta(days=index % 9), 12),
                    "momentum_score": Decimal(str(98 - index * 1.35)),
                    "homepage": f"https://example.com/clean-room/github-project-{number:02d}",
                },
            )

    def _seed_glossary(self) -> None:
        terms = [
            ("Net Liquidity", "净流动性", "流动性"),
            ("SOFR-IORB", "SOFR-IORB 利差", "流动性"),
            ("Reverse Repo", "逆回购", "流动性"),
            ("Treasury General Account", "财政部一般账户", "流动性"),
            ("Yield Curve", "收益率曲线", "利率"),
            ("Term Premium", "期限溢价", "利率"),
            ("Breakeven Inflation", "盈亏平衡通胀", "利率"),
            ("Real Yield", "实际收益率", "利率"),
            ("Bid-to-Cover", "投标倍数", "利率"),
            ("Auction Tail", "拍卖尾差", "利率"),
            ("OAS", "期权调整利差", "信用"),
            ("CDS", "信用违约互换", "信用"),
            ("SLOOS", "银行信贷调查", "信用"),
            ("NFCI", "全国金融状况指数", "信用"),
            ("Contango", "正向市场", "衍生品"),
            ("Backwardation", "现货溢价结构", "衍生品"),
            ("Basis", "基差", "衍生品"),
            ("Implied Volatility", "隐含波动率", "期权"),
            ("Gamma Exposure", "Gamma 暴露", "期权"),
            ("Delta Exposure", "Delta 暴露", "期权"),
            ("Vanna", "Vanna", "期权"),
            ("Charm", "Charm", "期权"),
            ("Max Pain", "最大痛点", "期权"),
            ("Skew", "波动率偏斜", "期权"),
            ("Open Interest", "未平仓量", "期权"),
            ("COT", "持仓报告", "仓位"),
            ("Percentile Rank", "百分位排名", "统计"),
            ("Rolling Correlation", "滚动相关性", "统计"),
            ("Inference", "推理", "AI 模型"),
            ("Context Window", "上下文窗口", "AI 模型"),
            ("Tokens per Second", "每秒 Token", "AI 模型"),
            ("SWE-bench", "SWE-bench", "AI 模型"),
        ]
        for index, (term_en, term, category) in enumerate(terms):
            GlossaryTerm.objects.update_or_create(
                slug=f"clean-room-term-{index + 1:02d}",
                defaults={
                    "term": term,
                    "term_en": term_en,
                    "category": category,
                    "subcategory": "平台基础概念",
                    "difficulty": ["入门", "中级", "高级"][index % 3],
                    "definition": f"{term}的原创简要定义，用于理解页面中的指标。",
                    "formula": "详见方法论页面；所有比率均标明单位与口径。",
                    "interpretation": "应结合时间窗口、数据来源和其他资产交叉验证。",
                    "tags": [category, term_en],
                    "source_url": "https://example.com/methodology",
                },
            )

    def _report_counts(self) -> None:
        counts = {
            "Thesis": Thesis.objects.count(),
            "FundLetter": FundLetter.objects.count(),
            "SupplyChainNode": SupplyChainNode.objects.count(),
            "Company": Company.objects.count(),
            "FinancialFact": FinancialFact.objects.count(),
            "ModelProfile": ModelProfile.objects.count(),
            "CodingAgentProfile": CodingAgentProfile.objects.count(),
            "GitHubProject": GitHubProject.objects.count(),
            "GlossaryTerm": GlossaryTerm.objects.count(),
        }
        expected = {
            "Thesis": 86,
            "FundLetter": 267,
            "SupplyChainNode": 45,
            "Company": 219,
            "FinancialFact": 657,
            "ModelProfile": 12,
            "CodingAgentProfile": 11,
            "GitHubProject": 45,
            "GlossaryTerm": 32,
        }
        mismatches = {
            key: (counts[key], target) for key, target in expected.items() if counts[key] != target
        }
        if mismatches:
            self.stdout.write(
                self.style.WARNING(
                    "Seed-owned targets were created, but the database also contains other rows: "
                    + ", ".join(
                        f"{key}={actual} (target {target})"
                        for key, (actual, target) in mismatches.items()
                    )
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Offline clean-room seed complete: "
                + ", ".join(f"{key}={value}" for key, value in counts.items())
            )
        )
