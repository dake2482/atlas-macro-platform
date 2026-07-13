from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from research.models import GlossaryTerm

OFFICIAL_GLOSSARY_TERMS = (
    {
        "slug": "rrp",
        "term": "隔夜逆回购工具",
        "term_en": "Overnight Reverse Repurchase Agreement Facility (ON RRP)",
        "category": "流动性",
        "subcategory": "货币政策工具",
        "difficulty": "入门",
        "definition": (
            "纽约联储交易台向合格对手方卖出证券，并约定次日买回的操作。"
            "从联储资产负债表角度看，未到期 ON RRP 是负债；操作期间它会暂时吸收对手方现金。"
        ),
        "formula": "展示量 = 当日 ON RRP 已接受金额（不是上限）",
        "interpretation": (
            "ON RRP 利率为无法获得 IORB 的多类货币市场参与者提供隔夜无风险投资选项，"
            "协助限制隔夜利率的下行压力。"
        ),
        "tags": ["ON RRP", "Federal Reserve", "money market"],
        "source_url": (
            "https://www.newyorkfed.org/markets/domestic-market-operations/"
            "monetary-policy-implementation/repo-reverse-repo-agreements"
        ),
    },
    {
        "slug": "tga",
        "term": "美国财政部一般账户",
        "term_en": "Treasury General Account (TGA)",
        "category": "流动性",
        "subcategory": "财政现金",
        "difficulty": "入门",
        "definition": (
            "美国财政部在纽约联储持有的主要操作现金账户。"
            "Daily Treasury Statement 披露其期初余额、存入、支出与期末余额。"
        ),
        "formula": "TGA 期末余额 = 期初余额 + 存入 - 支出",
        "interpretation": (
            "在其他条件不变时，TGA 上升往往对银行准备金形成抽离，TGA 下降则往往释放准备金；"
            "实际影响还需与财政收支、发债和联储资产负债表同时观察。"
        ),
        "tags": ["TGA", "Treasury", "cash balance"],
        "source_url": (
            "https://fiscaldata.treasury.gov/datasets/"
            "daily-treasury-statement/operating-cash-balance"
        ),
    },
    {
        "slug": "sofr-iorb",
        "term": "SOFR-IORB 利差",
        "term_en": "SOFR minus IORB spread",
        "category": "流动性",
        "subcategory": "货币市场",
        "difficulty": "中级",
        "definition": (
            "有担保隔夜融资利率（SOFR）与准备金付息利率（IORB）之差，"
            "是平台根据两个官方利率计算的派生指标，不是独立的官方统计序列。"
        ),
        "formula": "SOFR-IORB（bp） = (SOFR% - IORB%) × 100",
        "interpretation": (
            "正值表示回购融资利率高于银行持有准备金可获得的利率。"
            "美联储研究会将回购利率相对 IORB 的利差与其他量价指标一起用于观察准备金状况；"
            "月末、季末与大规模国债交割可造成短期扰动。"
        ),
        "tags": ["SOFR", "IORB", "repo spread", "derived metric"],
        "source_url": (
            "https://www.federalreserve.gov/econres/notes/feds-notes/"
            "market-based-indicators-on-the-road-to-ample-reserves-20250131.html"
        ),
    },
    {
        "slug": "cross-currency-basis",
        "term": "跨币种基差",
        "term_en": "Cross-currency basis",
        "category": "外汇",
        "subcategory": "全球美元融资",
        "difficulty": "高级",
        "definition": (
            "通过外汇掉期借入一种货币的隐含成本，与在现金市场直接借入该货币的成本之差。"
            "非零基差意味着有担保利率平价（CIP）偏离。"
        ),
        "formula": (
            "概念口径：basis = 外汇掉期隐含融资利率 - 现金市场融资利率；"
            "实际报价正负号取决于货币对与报价腿。"
        ),
        "interpretation": (
            "必须先确认币种、期限、参考利率与报价惯例，不能把所有负基差简化为同一方向的压力。"
        ),
        "tags": ["FX swap", "CIP", "dollar funding"],
        "source_url": "https://www.bis.org/publ/qtrpdf/r_qt1609e.htm",
    },
    {
        "slug": "aoci",
        "term": "累计其他综合收益",
        "term_en": "Accumulated Other Comprehensive Income (AOCI)",
        "category": "信用",
        "subcategory": "银行资产负债表",
        "difficulty": "中级",
        "definition": (
            "资产负债表中累计记录未计入净利润的其他综合收益的账户；"
            "对银行而言，其中可包含某些投资证券的未实现损益。"
        ),
        "formula": "AOCI = 历期计入其他综合收益、尚未重分类或实现的累计余额",
        "interpretation": (
            "AOCI 不等同于当期净利润或已实现损益；其是否计入监管资本取决于机构类别与适用规则。"
        ),
        "tags": ["AOCI", "bank capital", "unrealized gains and losses"],
        "source_url": "https://www.federalreserve.gov/publications/2023-April-SVB-Glossary.htm",
    },
    {
        "slug": "net-liquidity",
        "term": "净流动性代理",
        "term_en": "Net Liquidity Proxy",
        "category": "流动性",
        "subcategory": "派生指标",
        "difficulty": "中级",
        "definition": (
            "市场常用联储资产与 TGA、ON RRP 等官方负债项构造简化代理，"
            "用来追踪资产负债表变化对金融系统现金的可能方向。"
            "它不是美联储、美国财政部或纽约联储发布的官方统计序列。"
        ),
        "formula": (
            "平台参考代理 = 指定联储资产 - TGA - ON RRP；"
            "页面必须同时披露所用资产口径、单位和日期对齐规则。"
        ),
        "interpretation": (
            "该代理忽略货币市场基础设施、银行资产负债表约束、财政支出去向等多项机制，"
            "不能单独解释风险资产价格。"
        ),
        "tags": ["derived metric", "H.4.1", "TGA", "ON RRP"],
        "source_url": "https://www.federalreserve.gov/releases/h41/",
    },
    {
        "slug": "vix-term-structure",
        "term": "VIX 期限结构",
        "term_en": "VIX Term Structure",
        "category": "波动率",
        "subcategory": "波动率期货",
        "difficulty": "中级",
        "definition": (
            "不同到期日的 VIX 期货价格与到期时间之间的关系；"
            "Cboe 也发布基于不同标准 SPX 期权到期日的隐含波动率期限结构。"
        ),
        "formula": "常用斜率示例 = 远月 VIX 期货 - 近月 VIX 期货",
        "interpretation": (
            "远月高于近月通常称为 contango，近月高于远月通常称为 backwardation；"
            "曲线形状反映各到期时点的波动率定价，不是对现货指数方向的确定预测。"
        ),
        "tags": ["VIX", "contango", "backwardation"],
        "source_url": "https://www.cboe.com/tradable-products/vix/term-structure",
    },
    {
        "slug": "transmission-chain",
        "term": "货币政策传导链",
        "term_en": "Monetary Policy Transmission Chain",
        "category": "流动性",
        "subcategory": "传导机制",
        "difficulty": "中级",
        "definition": (
            "货币政策工具和沟通先影响隔夜与短期利率，再经由预期、长期利率、"
            "风险溢价、信贷、资产价格和汇率等渠道，影响家庭与企业决策，最终传导至就业与通胀。"
        ),
        "formula": "政策工具 → 隔夜利率 → 金融条件 → 支出与投资 → 就业与通胀",
        "interpretation": (
            "传导具有时滞、时变性和不确定性；页面的分层评分是研究框架，"
            "不应被解读为联储发布的官方指数。"
        ),
        "tags": ["monetary policy", "financial conditions", "transmission"],
        "source_url": "https://www.federalreserve.gov/newsevents/speech/jefferson20230327a.htm",
    },
)


class Command(BaseCommand):
    help = "Idempotently install the reviewed, source-linked public glossary baseline."

    def handle(self, *args, **options):
        created = 0
        updated = 0
        with transaction.atomic():
            for payload in OFFICIAL_GLOSSARY_TERMS:
                slug = payload["slug"]
                _, was_created = GlossaryTerm.objects.update_or_create(
                    slug=slug,
                    defaults={key: value for key, value in payload.items() if key != "slug"},
                )
                created += int(was_created)
                updated += int(not was_created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Official glossary synchronized: {created} created, {updated} updated."
            )
        )
