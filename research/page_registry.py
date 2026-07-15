from __future__ import annotations

from copy import deepcopy


def metric(
    label,
    _legacy_value=None,
    _legacy_change="",
    _legacy_status="fresh",
    _legacy_source="官方/授权数据",
    _legacy_as_of="最近批次",
    **_legacy_kwargs,
):
    """Describe an expected metric without shipping a plausible demo value.

    Older registry declarations still pass prototype values so their labels can
    be reviewed against the source-site contract.  Public configuration always
    reduces those declarations to an explicit missing-data card; a published
    dashboard snapshot is the only path that may supply a numeric value.
    """

    return {
        "label": label,
        "value": None,
        "display_value": "—",
        "change": None,
        "status": "stale",
        "source": "等待已授权数据源",
        "as_of": None,
    }


def contract_table(key, title, description, columns, rows):
    """Build a prose-only table whose rendered cell order is contractual."""

    column_keys = tuple(item[0] for item in columns)
    rendered_rows = []
    for values in rows:
        row = dict(zip(column_keys, values, strict=True))
        cells = []
        for column_key in column_keys:
            value = str(row[column_key])
            cell = (
                {"kind": "url", "label": value, "href": value}
                if value.startswith("/")
                else {"kind": "text", "value": value}
            )
            cells.append({"key": column_key, "cell": cell})
        row["cells_list"] = cells
        rendered_rows.append(row)
    return {
        "key": key,
        "title": title,
        "description": description,
        "columns": [
            {"key": column_key, "label": label}
            for column_key, label in columns
        ],
        "rows": rendered_rows,
        "full_width": True,
    }


COMMON = {
    "source_notes": [
        "页面只发布通过许可与质量检查的官方或授权数据；缺数时显示空缺。",
        "每个组件分别显示 value_date、fetched_at、quality_status 与 fallback，不以页面级时间覆盖差异。",
    ],
    "chart_data": [],
}


PAGE_CONFIGS = {
    "assets-equities": {
        "title": "美股",
        "eyebrow": "Equity Breadth",
        "description": "指数表现只是表层，广度、集中度、信用确认与期权结构共同决定上涨质量。",
        "metrics": [
            metric("标普 500", "5,482.31", "+0.42%", source="授权日线适配器"),
            metric("纳斯达克 100", "19,817.24", "+0.31%", source="授权日线适配器"),
            metric("200 日均线上方", "58.4%", "-0.6pp", source="成分股聚合"),
            metric("SPX−RUT 20D", "+3.2pp", "集中度升高", "stale", "成分股聚合"),
        ],
        "analysis": "指数仍处于上行结构，但中小盘确认不足。只有当广度与高收益信用同步改善，才把反弹升级为趋势。",
    },
    "assets-etfs": {
        "title": "ETF 看板",
        "eyebrow": "Cross-Asset ETFs",
        "description": "使用可交易 ETF 观察权益、久期、信用、商品和美元之间的资金迁移。",
        "metrics": [
            metric("SPY", "$548.21", "+0.42%"),
            metric("QQQ", "$486.70", "+0.31%"),
            metric("TLT", "$92.43", "-0.18%"),
            metric("HYG", "$78.94", "+0.06%"),
        ],
        "analysis": "权益 ETF 领先，但久期资产未确认。观察 HYG/LQD 是否继续相对走强。",
    },
    "assets-bonds": {
        "title": "债券",
        "eyebrow": "Official Treasury Curves",
        "description": (
            "展示美国财政部名义 Par Yield 曲线与 Atlas 透明利差计算；"
            "这些数值不是债券或 ETF 价格、久期、信用利差或总回报。"
        ),
        "snapshot_key": "yield-curve",
        "snapshot_contract_version": 2,
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {"value": "curve", "label": "曲线对比", "chart_keys": ["nominal-curve-comparison"]},
            {"value": "spreads", "label": "曲线利差", "chart_keys": ["curve-spreads-history"]},
        ],
        "default_tab": "curve",
        "metrics": [
            metric("2Y 名义收益率"),
            metric("10Y 名义收益率"),
            metric("2s10s"),
            metric("5s30s"),
        ],
        "analysis": "页面只陈述官方 Treasury 收益率和一手输入可复算的曲线利差，不生成未审核的久期或交易判断。",
    },
    "assets-commodities": {
        "title": "商品",
        "eyebrow": "Commodity Complex",
        "description": "能源、贵金属和工业金属用来验证增长、通胀与地缘风险。",
        "metrics": [
            metric("WTI", "$78.42", "+1.20%"),
            metric("黄金", "$2,391", "-0.24%"),
            metric("铜", "$4.56", "+0.61%"),
            metric("天然气", "$2.71", "-1.40%"),
        ],
        "analysis": "油价反弹而黄金回落，短线更像增长/供给交易，尚未形成全面通胀冲击。",
    },
    "assets-fx": {
        "title": "H.10 外汇参考",
        "eyebrow": "Federal Reserve H.10 References",
        "description": (
            "仅展示 Federal Reserve H.10 Nominal Broad Dollar Index 与 "
            "EUR/USD、USD/CNY、USD/JPY 日频参考值。它们不是 ICE DXY、"
            "CNH、可执行现货、远期/NDF 或 cross-currency basis。"
        ),
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "3m", "label": "3 个月", "months": 3},
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
        ],
        "default_period": "1y",
        "tab_options": [
            {
                "value": "broad-dollar",
                "label": "Broad Dollar",
                "chart_keys": ["fx-broad-dollar-history"],
            },
            {
                "value": "major-fx",
                "label": "Major FX",
                "chart_keys": [
                    "fx-major-reference-rates-usd-strength-rebased"
                ],
            },
        ],
        "default_tab": "broad-dollar",
        "metrics": [
            metric("H.10 Nominal Broad Dollar Index"),
            metric("H.10 EUR/USD Reference"),
            metric("H.10 USD/CNY Reference"),
            metric("H.10 USD/JPY Reference"),
        ],
        "analysis": (
            "只陈述 H.10 官方参考 level 和 Atlas Macro 透明变化/重基准计算；"
            "不生成离岸美元压力分数、交易行动或市场报价。"
        ),
    },
    "assets-crypto": {
        "title": "加密货币",
        "eyebrow": "Crypto Market",
        "description": "现货价格、波动率、ETF 资金与杠杆结构分层展示。",
        "metrics": [
            metric("BTC", "$67,420", "+1.14%", source="OKX / Deribit"),
            metric("ETH", "$3,480", "+0.82%", source="OKX"),
            metric("BTC 30D RV", "43.2%", "+1.1pp", source="日线计算"),
            metric("BTC/ETH", "19.37", "+0.4%", source="现货比值"),
        ],
        "analysis": "现货回升但杠杆未明显扩张，结构比高费率推动的上涨更健康。",
    },
    "rates": {
        "title": "利率",
        "eyebrow": "Rates Command Center",
        "description": "从政策利率、整条收益率曲线、实际利率与拍卖需求判断金融条件。",
        "snapshot_contract_version": 2,
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "metrics": [
            metric("有效联邦基金", "5.33%", "持平", source="NY Fed / FRED"),
            metric("2Y", "4.72%", "+2bp", source="Treasury / FRED"),
            metric("10Y", "4.31%", "+3bp", source="Treasury / FRED"),
            metric("2s10s", "-41bp", "+1bp", source="曲线计算"),
        ],
        "analysis": "曲线仍倒挂，长端小幅上行反映期限溢价而非增长加速。",
    },
    "fed-funds": {
        "title": "联邦基金利率",
        "eyebrow": "Policy Corridor",
        "description": (
            "EFFR、SOFR、IORB、目标区间、成交分位与成交量严格按最新共同"
            "有效日展示，避免把未来政策利率与尚未发布的市场利率混算。"
        ),
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
        ],
        "default_period": "1y",
        "tab_options": [
            {"value": "overview", "label": "总览", "chart_keys": []},
            {
                "value": "corridor",
                "label": "政策走廊",
                "chart_keys": ["policy-corridor"],
            },
            {
                "value": "effr",
                "label": "EFFR 分布",
                "chart_keys": ["effr-distribution"],
            },
            {
                "value": "sofr",
                "label": "SOFR 分布",
                "chart_keys": ["sofr-distribution"],
            },
        ],
        "default_tab": "overview",
        "metrics": [
            metric("EFFR"),
            metric("SOFR"),
            metric("IORB"),
            metric("目标区间下限"),
            metric("目标区间上限"),
            metric("SOFR−EFFR"),
            metric("SOFR−IORB"),
            metric("EFFR−IORB"),
            metric("EFFR 成交量"),
            metric("SOFR 成交量"),
            metric("EFFR 1P−99P 宽度"),
            metric("SOFR 1P−99P 宽度"),
            metric("EFFR 走廊位置"),
        ],
        "analysis": (
            "页面只陈述官方走廊和交易分布；任何压力判断必须引用同一有效日"
            "数据，缺少必需数据集时保留上一完整快照并明确 stale。"
        ),
    },
    "yield-curve": {
        "title": "收益率曲线",
        "eyebrow": "Treasury Curve",
        "description": "比较当前、1 周、1 月与 3 月前曲线，并拆分名义、实际和盈亏平衡通胀。",
        "snapshot_contract_version": 2,
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {"value": "curve", "label": "曲线对比", "chart_keys": ["nominal-curve-comparison"]},
            {"value": "spreads", "label": "曲线利差", "chart_keys": ["curve-spreads-history"]},
        ],
        "default_tab": "curve",
        "metrics": [
            metric("曲线形态", "熊平", "长端+3bp"),
            metric("2s10s", "-41bp", "+1bp"),
            metric("3m10y", "-109bp", "+4bp"),
            metric("5s30s", "+18bp", "-2bp"),
        ],
        "analysis": "长端上行由实际利率驱动，金融条件边际收紧。10Y 若突破 4.45%，长久期资产风险上升。",
    },
    "auctions": {
        "title": "国债拍卖",
        "eyebrow": "Auction Monitor",
        "description": (
            "未来 14 天正式拍卖与发行/结算公告面值，以及近 90 天官方结果；"
            "金额不是实际现金、TGA 变动、净融资或净流动性预测。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [],
        "analysis": (
            "页面仅发布当前 ET 日、双窗口完整且许可可公开的 FiscalData 批次；"
            "官方源不含 when-issued 收益率，因此不展示真实 Tail。"
        ),
    },
    "real-rates": {
        "title": "实际利率",
        "eyebrow": "TIPS & Inflation",
        "description": "名义收益率拆分为实际利率与盈亏平衡通胀。",
        "snapshot_contract_version": 2,
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {
                "value": "decomposition",
                "label": "名义 / 实际 / BEI",
                "chart_keys": ["nominal-real-breakeven-history"],
            }
        ],
        "default_tab": "decomposition",
        "metrics": [
            metric("5Y 实际利率", "2.08%", "+3bp"),
            metric("10Y 实际利率", "2.01%", "+2bp"),
            metric("10Y BEI", "2.30%", "+1bp"),
            metric("5Y5Y", "2.25%", "持平"),
        ],
        "analysis": "实际利率维持高位，成长股估值扩张空间受限；通胀预期仍大致锚定。",
    },
    "expectations": {
        "title": "利率预期",
        "eyebrow": "Policy Path",
        "description": "基于联邦基金期货的近似概率，不冒充 CME 官方 FedWatch。",
        "metrics": [
            metric("下次会议维持", "78%", "+4pp", source="ZQ 期货近似"),
            metric("降息 25bp", "22%", "-4pp", source="ZQ 期货近似"),
            metric("年末隐含利率", "4.86%", "+5bp"),
            metric("预期降息次数", "1.9", "-0.2"),
        ],
        "analysis": "市场削减短期降息定价，前端利率对弱数据的敏感度将上升。",
    },
    "fed-hawkish-dovish": {
        "title": "鹰鸽追踪",
        "eyebrow": "Fed Language Index",
        "description": "对官方声明和官员演讲进行可追溯的语言倾向分类。",
        "metrics": [
            metric("综合得分", "+0.4", "温和偏鹰", source="官方文本+模型分类"),
            metric("近 20 条鹰派", "7", "+2"),
            metric("近 20 条鸽派", "4", "-1"),
            metric("中性", "9", "持平"),
        ],
        "analysis": "政策沟通仍强调通胀风险，但劳动力市场降温使委员会内部差异扩大。",
    },
    "liquidity": {
        "title": "流动性",
        "eyebrow": "Dollar Liquidity",
        "description": (
            "H.4.1、ON RRP 与 TGA 严格按共同有效日计算净流动性代理，"
            "政策利率继承已验证的 Fed Funds 快照。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [
            metric("净流动性代理"),
            metric("联储总资产（共同日）"),
            metric("准备金（共同日）"),
            metric("ON RRP（共同日）"),
            metric("TGA（共同日）"),
            metric("SOFR"),
            metric("IORB"),
            metric("SOFR−EFFR"),
            metric("SOFR−IORB"),
        ],
        "analysis": (
            "净流动性是 Atlas Macro 的透明代理计算，不是美联储官方 LPI；"
            "任何必需组件失败时保留上一版完整快照。"
        ),
    },
    "transmission-chain": {
        "title": "六层官方证据传导链",
        "eyebrow": "Six-Layer Official Evidence Chain",
        "description": (
            "Federal Reserve、New York Fed 与 Treasury 官方输入"
            "按六个独立时点组件原子组合；这是可追溯证据链，"
            "不是压力指数、完整金融条件指数或交易信号。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [
            metric("净流动性透明代理"),
            metric("美联储总资产"),
            metric("ON RRP 常规操作"),
            metric("SRF 常规操作"),
            metric("SOFR−IORB"),
            metric("SOFR−13 周 T-bill"),
            metric("SOFR 99P−IORB"),
            metric("SOFR 成交量 Z60"),
            metric("准备金覆盖代理"),
            metric("准备金覆盖 8 周变化"),
            metric("H.10 广义美元 5 日变化"),
            metric("常规美元互换余额"),
        ],
        "analysis": (
            "页面只展示六个已审计组件的直接值、透明公式、"
            "共享输入对账与数据/许可缺口；解释严格限于各组件的证据边界。"
        ),
    },
    "fed-balance-sheet": {
        "title": "美联储资产负债表",
        "eyebrow": "H.4.1",
        "description": (
            "H.4.1 四项直接值与 ON RRP、TGA 只在精确共同周三"
            "原子发布，不做前值填充。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [
            metric("总资产"),
            metric("美债持有"),
            metric("MBS 持有"),
            metric("准备金"),
            metric("净流动性代理"),
        ],
        "analysis": (
            "净流动性是 Atlas Macro 以 WALCL - ON RRP - TGA 计算的"
            "透明代理，不是 Federal Reserve 官方指标，也不是 LPI 综合分。"
        ),
    },
    "operations": {
        "title": "公开市场操作",
        "eyebrow": "Open Market Operations",
        "description": (
            "纽约联储国债二级市场购买、ON RRP、SRF 与 SOMA "
            "只在同刷新周期的四个精确批次齐备时发布。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [],
        "analysis": (
            "国债购买结果同时覆盖 RMP、本金再投资与可能未分类的"
            "操作演练；官方 feed 没有稳定用途或 small-value 字段，"
            "因此不发布伪精确 RMP-only 数据。"
        ),
    },
    "rrp-tga": {
        "title": "RRP 与 TGA",
        "eyebrow": "Fiscal Liquidity",
        "description": (
            "并列展示 ON RRP、TGA 官方余额及未来 14 天发行/结算公告总面值；"
            "不把公告面值冒充实际现金、未来 TGA 方向或净流动性冲击。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [],
        "analysis": (
            "三个组件只在同一完整刷新周期发布；不同有效日的余额不强行"
            "相减，发行/结算日历也不用于生成未来净抽水预测。"
        ),
    },
    "reserves": {
        "title": "银行准备金",
        "eyebrow": "Reserves Coverage Proxy",
        "description": (
            "H.4.1 准备金余额与 H.8 美国商业银行总资产"
            "只在共同周三对齐。比率为覆盖近似，不是 Federal Reserve "
            "官方指标或监管结论。"
        ),
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {
                "value": "levels",
                "label": "规模对齐",
                "chart_keys": ["reserves-assets-history"],
            },
            {
                "value": "ratio",
                "label": "覆盖近似",
                "chart_keys": ["reserve-ratio-history"],
            },
            {
                "value": "funding",
                "label": "资金利差",
                "chart_keys": [
                    "reserves-funding-levels",
                    "reserves-sofr-tbill-spread-history",
                    "reserves-sofr-iorb-spread-history",
                ],
            },
        ],
        "default_tab": "levels",
        "metrics": [
            metric("准备金余额"),
            metric("美国商业银行总资产"),
            metric("准备金 / 商业银行资产覆盖近似"),
            metric("覆盖近似 8 周变化"),
            metric("覆盖近似 8 周变化 Z-score"),
            metric("SOFR"),
            metric("13-week T-bill Coupon Equivalent"),
            metric("IORB"),
            metric("SOFR−13-week T-bill"),
            metric("SOFR−IORB"),
        ],
        "analysis": (
            "分子覆盖所有存款机构，分母覆盖美国商业银行，"
            "机构宇宙不一致。页面只发布直接值和可复算统计，"
            "不生成自动状态或交易建议。"
        ),
    },
    "global-dollar": {
        "title": "全球美元",
        "eyebrow": "Official USD Reference & Swap Backstop",
        "description": (
            "Federal Reserve H.10 日频参考序列与 New York Fed 央行"
            "美元流动性互换按两个异频 exact batch 发布。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [
            metric("Nominal Broad Dollar Index"),
            metric("Broad Dollar 5D Change"),
            metric("EUR/USD H.10 Reference"),
            metric("USD/CNY H.10 Reference"),
            metric("USD/JPY H.10 Reference"),
            metric("USD Liquidity Swaps Outstanding"),
            metric("Regular USD Swaps Outstanding"),
            metric("Technical-Test USD Swaps Outstanding"),
            metric("Active Regular Counterparties"),
        ],
        "analysis": (
            "本页不是 ICE DXY、可交易现货/远期或跨币种基差，"
            "也不合成离岸美元压力分数、交易信号或行动建议。"
        ),
    },
    "subsurface": {
        "title": "次表层资金流",
        "eyebrow": "Repo Microstructure",
        "description": (
            "SOFR 尾部与成交量、IORB、非技术测试 SRF 和美元央行互换"
            "按四个官方精确批次原子发布。"
        ),
        "snapshot_contract_version": 1,
        "metrics": [
            metric("SOFR"),
            metric("SOFR 99P"),
            metric("IORB"),
            metric("SOFR 99P−SOFR"),
            metric("SOFR 99P−IORB"),
            metric("SOFR 成交量"),
            metric("SOFR 成交量 Z60"),
            metric("SRF 非测试接受额"),
            metric("SRF 操作利率"),
            metric("SRF 30D 非测试激活天数"),
            metric("美元互换非测试在途"),
        ],
        "analysis": (
            "Z60、SRF 激活天数和剔除 small-value 的互换在途是 Atlas "
            "Macro 透明代理，不是不透明综合压力分或官方压力指标。"
        ),
    },
    "economy": {
        "title": "经济数据",
        "eyebrow": "US Macro Pulse",
        "description": (
            "增长、就业、通胀与消费四个官方子页原子组合；每项分别保留"
            "有效日、抓取时间、输入批次、公式、许可与质量。"
        ),
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {"value": "overview", "label": "总览", "chart_keys": []},
            {
                "value": "growth",
                "label": "增长",
                "chart_keys": ["gdp-growth-history"],
            },
            {
                "value": "labor",
                "label": "就业",
                "chart_keys": ["labor-slack"],
            },
            {
                "value": "inflation",
                "label": "通胀",
                "chart_keys": ["core-cpi-rates"],
            },
            {
                "value": "consumer",
                "label": "消费",
                "chart_keys": ["real-consumption-income-momentum"],
            },
        ],
        "default_tab": "overview",
        "metrics": [
            metric("实际 GDP 季调年化增速"),
            metric("失业率"),
            metric("核心 CPI 同比"),
            metric("实际 PCE 环比"),
        ],
        "analysis": "只有四个必需组件同时通过契约检查时才发布完整总览。",
    },
    "gdp": {
        "title": "GDP",
        "eyebrow": "Growth Decomposition",
        "description": "实际 GDP、消费、投资、政府和净出口贡献。",
        "metrics": [
            metric("实际 GDP", "+2.1%", "环比年化", source="BEA / GDPC1"),
            metric("消费贡献", "+1.4pp", "主引擎"),
            metric("投资贡献", "+0.4pp", "改善"),
            metric("净出口", "-0.2pp", "拖累"),
        ],
        "analysis": "消费仍支撑增长，但投资和库存的波动增加下一季度不确定性。",
    },
    "employment": {
        "title": "就业",
        "eyebrow": "Labor Market",
        "description": "非农、失业率、时薪、职位空缺与初请共同判断供需平衡。",
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {"value": "overview", "label": "总览", "chart_keys": []},
            {
                "value": "payroll",
                "label": "非农与工资",
                "chart_keys": ["payroll-change", "average-hourly-earnings-yoy"],
            },
            {
                "value": "slack",
                "label": "劳动闲置",
                "chart_keys": ["labor-slack"],
            },
            {
                "value": "turnover",
                "label": "JOLTS 周转",
                "chart_keys": ["jolts-rates"],
            },
            {
                "value": "claims",
                "label": "失业申领",
                "chart_keys": ["initial-claims", "continued-claims"],
            },
        ],
        "default_tab": "overview",
        "metrics": [
            metric("非农新增", "+176K", "低于 3M 均值", source="BLS"),
            metric("失业率", "4.1%", "+0.1pp"),
            metric("时薪", "+3.9%", "同比 -0.2pp"),
            metric("职位空缺", "8.14M", "-0.22M"),
        ],
        "analysis": "劳动力需求正常化但未断裂，工资降温给通胀继续回落提供空间。",
    },
    "inflation": {
        "title": "通胀",
        "eyebrow": "Inflation Stack",
        "description": (
            "使用 BLS 季调与未季调配对指数展示总体 CPI、核心 CPI 与最终需求"
            "PPI 的环比、同比及 3M/6M 年化动能，并加入 Shelter、核心商品与"
            "不含能源服务的服务 CPI 官方分项；BEA PIO Section 2 展示 PCE"
            " 与核心 PCE 价格指数；市场预期复用 Treasury/TIPS 官方曲线派生"
            " 5Y/10Y BEI 代理；历史 vintage 缺口在数据台账中单列。"
        ),
        "period_options": [
            {"value": "1y", "label": "1 年", "months": 12},
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
        ],
        "default_period": "3y",
        "tab_options": [
            {"value": "overview", "label": "总览", "chart_keys": []},
            {
                "value": "headline",
                "label": "总体 CPI",
                "chart_keys": ["headline-cpi-rates"],
            },
            {
                "value": "core",
                "label": "核心 CPI",
                "chart_keys": ["core-cpi-rates"],
            },
            {
                "value": "components",
                "label": "CPI 分项",
                "chart_keys": [
                    "shelter-cpi-rates",
                    "core-goods-cpi-rates",
                    "services-less-energy-cpi-rates",
                ],
            },
            {
                "value": "producer",
                "label": "生产者价格",
                "chart_keys": ["final-demand-ppi-rates"],
            },
            {
                "value": "pce",
                "label": "PCE",
                "chart_keys": ["pce-price-rates", "core-pce-price-rates"],
            },
            {
                "value": "expectations",
                "label": "预期",
                "chart_keys": ["market-breakeven-inflation"],
            },
        ],
        "default_tab": "overview",
        "metrics": [
            metric("CPI 环比"),
            metric("CPI 同比"),
            metric("CPI 3M 年化"),
            metric("CPI 6M 年化"),
            metric("核心 CPI 环比"),
            metric("核心 CPI 同比"),
            metric("核心 CPI 3M 年化"),
            metric("核心 CPI 6M 年化"),
            metric("住房成本 CPI（Shelter） 环比"),
            metric("住房成本 CPI（Shelter） 同比"),
            metric("核心商品 CPI 环比"),
            metric("核心商品 CPI 同比"),
            metric("服务 CPI（不含能源服务） 环比"),
            metric("服务 CPI（不含能源服务） 同比"),
            metric("最终需求 PPI 环比"),
            metric("最终需求 PPI 同比"),
            metric("最终需求 PPI 3M 年化"),
            metric("最终需求 PPI 6M 年化"),
            metric("PCE 价格指数 环比"),
            metric("PCE 价格指数 同比"),
            metric("核心 PCE 价格指数 环比"),
            metric("核心 PCE 价格指数 同比"),
            metric("5Y 盈亏平衡通胀（Treasury 曲线代理）"),
            metric("10Y 盈亏平衡通胀（Treasury 曲线代理）"),
        ],
        "analysis": (
            "当前只对通过同批完整性检查的 BLS、BEA PIO 与 Treasury/TIPS "
            "官方曲线通胀层作可复算展示；BLS 分项不使用残差估算；"
            "未接入层保持空缺，不用指数水平或演示数值替代。"
        ),
    },
    "consumer": {
        "title": "消费",
        "eyebrow": "Household Demand",
        "description": "零售、实际消费、储蓄率、信贷与消费者信心。",
        "metrics": [
            metric("零售销售", "+0.4%", "环比", source="Census"),
            metric("实际 PCE", "+0.3%", "环比", source="BEA"),
            metric("储蓄率", "3.9%", "-0.2pp"),
            metric("消费者信心", "68.2", "+2.1"),
        ],
        "analysis": "消费仍有韧性但缓冲下降，低收入家庭的信用压力是领先风险。",
    },
    "volatility": {
        "title": "波动率数据覆盖",
        "eyebrow": "Audited Coverage Ledger",
        "description": "区分可复算的实现波动率、已就绪但尚未启用的严格输入，以及必须采购的专有隐含波动率。",
        "metrics": [],
        "prose_only_contract": True,
        "suppress_empty_chart": True,
        "analysis": "在至少两个异资产严格子快照可独立重验前，不发布跨资产风险分、状态判断或交易信号。",
        "sections": [
            contract_table(
                "volatility-coverage-ledger",
                "当前数据覆盖",
                "CONTRACT_READY 表示严格合同已实现；是否有当前可发布快照由 FX 子页的 selector 独立判断。",
                (
                    ("component", "组件"),
                    ("status", "状态"),
                    ("public-output", "公开输出"),
                    ("next-action", "下一步"),
                ),
                (
                    ("H.10 FX realized volatility", "CONTRACT_READY", "/volatility/fx-vol/", "子页只在严格 selector 通过时发布 20D/60D 数字"),
                    ("Treasury yield-change realized volatility", "INPUT_READY", "—", "另立 requirement 定义 Atlas realized yield-vol 公式与发布合同"),
                    ("Cboe VIX family / CFE VX", "PURCHASE_REQUIRED", "—", "采购历史存储与网站展示权"),
                    ("ICE MOVE", "PURCHASE_REQUIRED", "—", "采购 ICE Data Indices 展示权"),
                    ("FX / cross-asset implied volatility", "PURCHASE_REQUIRED", "—", "采购期权面与标的同批次数据"),
                ),
            )
        ],
    },
    "volatility-dashboard": {
        "title": "波动率全景前置合同",
        "eyebrow": "Cross-Asset Preconditions",
        "description": "跨资产全景只有在各子组件使用严格、可重放且许可一致的数据合同后才发布。",
        "metrics": [],
        "prose_only_contract": True,
        "suppress_empty_chart": True,
        "analysis": "不恢复 30 指标热力图、升温/降温计数或高低风险判断；当前只公开数据可用性。",
        "sections": [
            contract_table(
                "volatility-dashboard-preconditions",
                "跨资产发布前置条件",
                "任一输入缺失时页面保持无数字，不以 ETF、新闻或终端可见值补齐。",
                (
                    ("layer", "层"),
                    ("required-contract", "必需合同"),
                    ("current-state", "当前状态"),
                    ("failure-policy", "失败策略"),
                ),
                (
                    ("FX realized volatility", "H.10 ZIP exact replay", "CONTRACT_READY", "子页 selector 独立判断是否有可发布快照"),
                    ("Rates realized volatility", "Treasury XML replay + append-only annual batches", "INPUT_READY", "另立公式与 selector 前不发布数字或 MOVE 代理"),
                    ("Equity implied volatility", "Cboe index and CFE display licence", "PURCHASE_REQUIRED", "零指标、零图表"),
                    ("Cross-asset parent", "At least two independently valid children", "NOT_READY", "不生成分数或状态"),
                ),
            )
        ],
    },
    "vix": {
        "title": "VIX 数据采购合同",
        "eyebrow": "Cboe / CFE Licence Boundary",
        "description": "VIX family 指数、VX 期货期限结构与 SPX/SPY 实现波动率是不同口径。",
        "metrics": [],
        "prose_only_contract": True,
        "suppress_empty_chart": True,
        "analysis": "取得覆盖历史存储、网站展示和派生展示的 Cboe/CFE 许可前，本页不发布任何数值或替代指标。",
        "sections": [
            contract_table(
                "vix-data-boundary",
                "不可替代的数据口径",
                "公开网页可见值或 SPY 实现波动率不能替代授权 VIX/VX 数据。",
                (
                    ("dataset", "数据集"),
                    ("what-it-is", "含义"),
                    ("why-not-substitute", "为何不可替代"),
                    ("required-licence", "所需许可"),
                ),
                (
                    ("VIX family index close", "Cboe methodology indices", "不是单只 ETF 或普通期权链字段", "Cboe Global Indices Feed public display + history"),
                    ("CFE VX futures term structure", "逐到期月 futures settlement/close", "不能由 VIX spot 外推", "CFE market data storage + website display"),
                    ("SPX/SPY realized volatility", "标的历史收益率标准差", "是 realized vol，不是 option-implied VIX", "授权 underlying bars + derived display"),
                ),
            ),
            contract_table(
                "vix-post-purchase-fields",
                "采购后最低字段合同",
                "字段不足或许可不完整时仍不进入公开 snapshot。",
                (("field", "字段"), ("requirement", "要求"), ("reason", "原因")),
                (
                    ("family / symbol", "VIX/VIX9D/VIX3M/VXTLT 或 VX contract", "防止指数族混淆"),
                    ("value timestamp", "带时区的 observation/settlement time", "区分盘中、收盘与结算"),
                    ("close / settlement", "保留 vendor 原始字段语义", "不能把 settlement 冒充 close"),
                    ("expiry", "VX 合约必须有到期日", "构建期限结构"),
                    ("unit / method version", "指数单位和方法版本", "支持历史方法变更"),
                    ("source licence", "存储、网站与派生展示范围", "控制公开发布"),
                    ("batch / quality / fallback", "逐点血缘与失败状态", "支持重验与 stale 管理"),
                ),
            ),
        ],
    },
    "credit": {
        "title": "美国信用市场",
        "eyebrow": "Credit Official v1",
        "description": "原子组合同一刷新周期的 Treasury HQM 企业债收益率代理与 Federal Reserve SLOOS 银行信贷调查。",
        "metrics": [],
        "snapshot_contract_version": 1,
        "tab_options": [
            {"value": "hqm", "label": "HQM", "chart_keys": ["credit-overview-hqm-history"]},
            {"value": "sloos", "label": "SLOOS", "chart_keys": ["credit-overview-sloos-standards-history"]},
        ],
        "default_tab": "hqm",
        "analysis": "只陈述两个官方子页可支持的事实，不把月频 HQM 与季频 SLOOS 合成信用分数。",
    },
    "credit-spreads": {
        "title": "Treasury HQM 企业债收益率代理",
        "eyebrow": "Official HQM Par Yields",
        "description": "U.S. Treasury 高质量企业债月均 par yield；不是国债利差、ICE BofA OAS 或评级桶。",
        "metrics": [],
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "3y", "label": "3 年", "months": 36},
            {"value": "5y", "label": "5 年", "months": 60},
            {"value": "10y", "label": "10 年", "months": 120},
        ],
        "tab_options": [
            {"value": "curve", "label": "最新曲线", "chart_keys": ["hqm-latest-par-yield-curve"]},
            {"value": "history", "label": "历史", "chart_keys": ["hqm-par-yield-history"]},
        ],
        "default_period": "10y",
        "default_tab": "curve",
        "analysis": "主值为 Treasury 官方直接观测；变化为 Atlas Macro 对前一共同有效月份的透明 bp 计算。",
    },
    "credit-cds": {
        "title": "CDX / CDS 采购边界",
        "eyebrow": "No Numeric CDS Proxy",
        "description": "未取得 composite 历史、存储与公开展示权前，不发布任何 CDX/CDS 数字或伪代理。",
        "metrics": [],
        "prose_only_contract": True,
        "suppress_empty_chart": True,
        "sections": [
            {
                "key": "cds-market-data-boundary",
                "title": "Composite、成交与代理不是同一口径",
                "body": "S&P Global/Markit 等 composite 估值不等于单笔 SEF/SDR 成交。ETF、银行股或国债变化只能另行命名为方向代理，不能标为 CDX/CDS。",
                "columns": [
                    {"key": "quote-type", "label": "报价类型"},
                    {"key": "what-it-is", "label": "它是什么"},
                    {"key": "why-not-substitute", "label": "为何不能互相替代"},
                    {"key": "required-licence", "label": "所需许可"},
                ],
                "rows": [
                    {
                        "cells_list": [
                            {"key": "quote-type", "cell": {"kind": "text", "value": "Composite / index quote"}},
                            {"key": "what-it-is", "cell": {"kind": "text", "value": "多贡献商或模型合成的指数、单名估值与期限曲线"}},
                            {"key": "why-not-substitute", "cell": {"kind": "text", "value": "不是单笔成交，也不能由 ETF、银行股或国债变动反推"}},
                            {"key": "required-licence", "cell": {"kind": "text", "value": "须明确授予历史存储、公开或 derived display 权"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "quote-type", "cell": {"kind": "text", "value": "SEF / SDR transaction"}},
                            {"key": "what-it-is", "cell": {"kind": "text", "value": "带成交时间与申报字段的单笔交易记录"}},
                            {"key": "why-not-substitute", "cell": {"kind": "text", "value": "单笔成交不能代表连续 composite 中间价或完整横截面"}},
                            {"key": "required-licence", "cell": {"kind": "text", "value": "只能按申报源条款展示，并明确标为成交而非 composite"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "quote-type", "cell": {"kind": "text", "value": "Directional proxy"}},
                            {"key": "what-it-is", "cell": {"kind": "text", "value": "ETF、股票、国债或其他市场变量的方向信号"}},
                            {"key": "why-not-substitute", "cell": {"kind": "text", "value": "可另页命名为代理，但语义上不是 CDX/CDS 报价"}},
                            {"key": "required-licence", "cell": {"kind": "text", "value": "遵守各代理源许可；任何许可都不能改变其指标名称"}},
                        ]
                    },
                ],
            },
            {
                "key": "cds-purchase-candidates",
                "title": "采购候选",
                "body": "优先评估 S&P Global/Markit、ICE settlement、LSEG/Bloomberg 中明确授予 public/derived display 与历史存储权的产品。FRED 页面可见的 ICE 受限序列不等于可再分发。",
            },
            {
                "key": "cds-post-purchase-fields",
                "title": "采购后最低字段合同",
                "body": "reference entity/index、series/version、tenor、currency、restructuring clause、bid/mid/ask、timestamp、contributor/composite method、source licence、value date、fetch time、batch、quality、fallback。",
                "columns": [
                    {"key": "field", "label": "字段"},
                    {"key": "requirement", "label": "最低要求"},
                    {"key": "reason", "label": "原因"},
                ],
                "rows": [
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "reference entity / index"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "稳定标识、正式名称与 index family"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "防止指数与单名、不同家族之间误配"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "series / version"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "指数系列、版本与生效区间"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "滚动换券后仍能还原当时合约"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "tenor / currency / restructuring clause"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "期限、币种与重组条款不可缺省"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "这些条款直接决定报价是否可比较"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "bid / mid / ask"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "保存原始三边报价及缺失状态"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "不得把估算中间价伪装为可交易报价"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "timestamp / value date"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "带时区的观测时间与明确 value date"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "区分盘中报价、日终估值与数据修订"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "contributor / composite method"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "贡献商范围、合成方法与方法版本"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "支持跨供应商比较与方法变更审计"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "source licence"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "保存产品、账户、地域与展示范围"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "页面发布必须能证明公开展示权"}},
                        ]
                    },
                    {
                        "cells_list": [
                            {"key": "field", "cell": {"kind": "text", "value": "fetch / batch / quality / fallback"}},
                            {"key": "requirement", "cell": {"kind": "text", "value": "抓取时间、批次、质量状态与 fallback 明示"}},
                            {"key": "reason", "cell": {"kind": "text", "value": "保证每个发布值可追溯且失败状态不被掩盖"}},
                        ]
                    },
                ],
            },
        ],
        "analysis": "当前页面只解释采购原因、口径边界和上线后的字段契约，不提供交易或对冲报价。",
        "source_notes": [
            "Composite 报价与单笔 SEF/SDR 成交不同口径。",
            "无授权时不使用 ETF、银行股、国债或 OAS 数字冒充 CDX/CDS。",
        ],
    },
    "credit-stress": {
        "title": "银行信贷压力代理",
        "eyebrow": "Federal Reserve SLOOS",
        "description": "季度银行贷款标准与需求净百分比；不是市场报价、NFCI 或综合压力分。",
        "metrics": [],
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "10y", "label": "10 年", "months": 120},
            {"value": "20y", "label": "20 年", "months": 240},
        ],
        "tab_options": [
            {"value": "standards", "label": "贷款标准", "chart_keys": ["sloos-lending-standards-history"]},
            {"value": "demand", "label": "贷款需求", "chart_keys": ["sloos-loan-demand-history"]},
        ],
        "default_period": "20y",
        "default_tab": "standards",
        "analysis": "正值标准项表示净收紧；正值需求项表示净需求增强。变化为前一共同有效季度的百分点变化。",
    },
    "trade-map": {
        "title": "今日 Trade Map",
        "eyebrow": "Cross-Asset Decision Map",
        "description": "将已审核的宏观主线映射到受益资产、回避资产、触发器、证伪条件与风险预算。",
        "metrics": [],
        "chart_data": [],
        "analysis": "当前尚无通过证据完整性检查的 Trade Map 批次，页面不展示估算或演示结论。",
        "sections": [
            {
                "title": "数据缺口",
                "body": "需要已发布 Thesis、可追溯 EvidenceItem、Trigger、Invalidation、Outcome 以及同批次跨资产快照。",
            },
            {
                "title": "目标字段",
                "body": "as_of、regime、confidence、beneficiary_assets、avoid_assets、risk_budget、triggers、invalidations、confirmation_matrix、divergences、source_ids、batch_id、review_status。",
            },
        ],
        "source_notes": ["仅在当日必需数据完整且研判通过审核后发布。"],
    },
    "volatility-move": {
        "title": "MOVE 数据采购合同",
        "eyebrow": "ICE Index Licence Boundary",
        "description": "ICE MOVE 是美债期权隐含波动率指数；财政部收益率变化实现波动率不是 MOVE。",
        "metrics": [],
        "prose_only_contract": True,
        "suppress_empty_chart": True,
        "analysis": "MOVE 是授权指数；在取得可公开展示的许可之前，不生成或填充代理数值。",
        "sections": [
            contract_table(
                "move-data-boundary",
                "MOVE 与可计算代理的边界",
                "即使未来发布 Treasury yield-change RV，也必须明确是 Atlas 代理而非 MOVE。",
                (
                    ("dataset", "数据集"),
                    ("what-it-is", "含义"),
                    ("why-not-substitute", "为何不可替代"),
                    ("required-licence", "所需许可"),
                ),
                (
                    ("ICE MOVE index", "Treasury option implied-volatility index", "不能由现券收益率变化精确复原", "ICE Data Indices history + public display"),
                    ("Treasury yield-change RV", "官方 par-yield 日变动样本标准差", "是 realized yield volatility，不是 option IV 或债券价格 vol", "Treasury raw replay + Atlas derived display"),
                    ("Swaption / futures option surface", "期限与执行价维度隐含波动率", "单一 MOVE 点位也不能替代完整 surface", "授权 derivatives surface"),
                ),
            ),
            contract_table(
                "move-post-purchase-fields",
                "采购后最低字段合同",
                "必须保留指数版本、时间与许可血缘。",
                (("field", "字段"), ("requirement", "要求"), ("reason", "原因")),
                (
                    ("index family / version", "MOVE family 与方法版本", "避免历史口径漂移"),
                    ("close / timestamp", "带时区收盘值和时间", "区分不同 cut"),
                    ("term components", "若产品提供则保留期限分量", "支持分解而非反推"),
                    ("method version", "vendor methodology revision", "支持重算边界"),
                    ("source licence", "历史存储、网站与派生展示", "许可闸门"),
                    ("batch / quality / fallback", "逐批次状态", "失败可见并可重验"),
                ),
            ),
        ],
        "source_notes": ["建议向 ICE Data Indices 采购 MOVE 延迟、收盘或历史数据权限。"],
    },
    "fx-vol": {
        "title": "H.10 外汇实现波动率",
        "eyebrow": "Official Reference-Level RV",
        "description": "以 Federal Reserve H.10 官方参考序列计算 20D/60D 年化实现波动率；不是 FX option IV。",
        "metrics": [],
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "3m", "label": "3 个月", "months": 3},
            {"value": "1y", "label": "1 年", "months": 12},
        ],
        "tab_options": [
            {"value": "20d", "label": "20D RV", "chart_keys": ["h10-fx-realized-volatility-20d"]},
            {"value": "60d", "label": "60D RV", "chart_keys": ["h10-fx-realized-volatility-60d"]},
        ],
        "default_period": "1y",
        "default_tab": "20d",
        "analysis": "只发布相邻有效 H.10 observation 的样本标准差，不插值、不前值填充；ATM IV、risk reversal 与 butterfly 保持采购缺口。",
    },
    "implied-vs-realized": {
        "title": "隐含 vs 实现波动率",
        "eyebrow": "Implied / Realized Volatility",
        "description": "按资产和时间窗口对比期权隐含波动率、实现波动率与波动率风险溢价。",
        "metrics": [],
        "prose_only_contract": True,
        "suppress_empty_chart": True,
        "analysis": "期权链和可追溯日线尚未形成同批次数据，因此不发布 IV-RV 差值。",
        "sections": [
            contract_table(
                "iv-rv-input-boundary",
                "IV-RV 输入边界",
                "任一必需输入缺失、过期或不同批次时不计算差值或风险溢价。",
                (
                    ("input", "输入"),
                    ("required-contract", "必需合同"),
                    ("failure-policy", "失败策略"),
                    ("licence", "许可"),
                ),
                (
                    ("Option quote / chain", "bid/mid/ask、expiry、strike、call/put、timestamp", "零 IV 输出", "OPRA/Cboe 或授权 consolidated feed"),
                    ("Underlying prices", "同标的、统一 calendar 的授权历史 bars", "零 RV/IV-RV 输出", "缓存、历史与 derived display"),
                    ("Rates and dividends", "与 valuation timestamp 对齐的输入", "不静默用零", "可公开派生展示"),
                    ("IV selection method", "ATM/moneyness/delta 与插值规则版本", "方法不完整则 fail closed", "Atlas method + vendor field rights"),
                ),
            ),
            contract_table(
                "iv-rv-post-purchase-fields",
                "采购后最低字段合同",
                "支持同批次复算、方法版本和许可审计。",
                (("field", "字段"), ("requirement", "要求"), ("reason", "原因")),
                (
                    ("instrument", "唯一标的与市场标识", "绑定期权和现货"),
                    ("expiry / tenor", "到期日与剩余期限", "统一比较窗口"),
                    ("strike / moneyness / delta", "原始执行价及选择维度", "复算 ATM/25Δ"),
                    ("call / put", "期权类型", "正确选择与 parity 检查"),
                    ("bid / mid / ask", "保留报价口径", "避免用 last 冒充 composite"),
                    ("IV method", "模型、插值与版本", "可复算隐含波动率"),
                    ("rates / dividends", "估值输入及时间", "避免静默假设"),
                    ("RV window", "5D/20D/60D 等精确观察数", "防止自然日混用"),
                    ("timestamps", "quote、value 与 fetch 时点", "同批次对齐"),
                    ("batch / source / licence / quality / fallback", "完整血缘", "发布安全"),
                ),
            ),
        ],
        "source_notes": ["期权与现货数据必须同批次对齐；缺任一输入时不生成结果。"],
    },
    "supply-chain": {
        "title": "AI 算力供应链",
        "eyebrow": "AI Compute Supply Chain",
        "description": "连接晶圆代工、先进封装、HBM、加速器与下游需求的公开证据链。",
        "metrics": [],
        "chart_data": [],
        "analysis": "五环节实际供需数据尚未完成授权与人工校验，页面不使用合成覆盖率或产能数值。",
        "sections": [
            {
                "title": "数据缺口",
                "body": "需要五环节快照、供需状态、产能事件、产品路线图、上下游依赖及每条证据的审核状态。",
            },
            {
                "title": "目标字段",
                "body": "node、period、supply_capacity、demand_capacity、coverage_ratio、lead_time、status、event_type、evidence_url、confidence、reviewed_at、source_id、license_scope。",
            },
        ],
        "source_notes": ["免费层使用公司 IR、SEC 和交易所公告；专有供需估算需单独采购授权。"],
    },
    "supply-chain-foundry": {
        "title": "晶圆代工",
        "eyebrow": "Foundry Capacity",
        "description": "跟踪先进制程产能、利用率、市占率、工厂爬坡与主要客户需求。",
        "metrics": [],
        "chart_data": [],
        "analysis": "晶圆厂月度先进制程利用率通常不公开；在没有授权估算时必须显示数据缺口。",
        "sections": [
            {
                "title": "可免费补齐",
                "body": "公司季报与法说会、月度营收、CapEx、节点路线图、新厂投产时间及公司指引。",
            },
            {
                "title": "目标字段",
                "body": "company、fab、location、process_node、wafer_capacity_monthly、utilization_rate、market_share、revenue、capex、guidance_period、status、source_id、confidence。",
            },
        ],
        "source_notes": [
            "利用率与市占率建议询价 TrendForce、TechInsights 或 Omdia，并单独确认网页展示权。"
        ],
    },
    "supply-chain-packaging": {
        "title": "先进封装",
        "eyebrow": "Advanced Packaging",
        "description": "跟踪 CoWoS、SoIC 及其他 AI 加速器封装产能、供需缺口与扩产进度。",
        "metrics": [],
        "chart_data": [],
        "analysis": "尚无经许可的封装月产能和需求拆分，不发布供需缺口百分比。",
        "sections": [
            {
                "title": "数据缺口",
                "body": "需要各封装平台月产能、在建产能、良率、需求分配、交付周期和主要 OSAT 参与者。",
            },
            {
                "title": "目标字段",
                "body": "company、platform、period、capacity_monthly、planned_capacity、yield_rate、demand_monthly、coverage_ratio、lead_time_weeks、customer_mix、source_id、confidence。",
            },
        ],
        "source_notes": [
            "免费证据优先来自公司 IR；精确产能与客户分配建议购买 SemiAnalysis、TechInsights 或 TrendForce。"
        ],
    },
    "supply-chain-hbm": {
        "title": "HBM 内存",
        "eyebrow": "High-Bandwidth Memory",
        "description": "跟踪 HBM 代际、位产出、供需覆盖、客户认证、合约价与产能爬坡。",
        "metrics": [],
        "chart_data": [],
        "analysis": "HBM 位产出、合约价和客户分配多为专有数据；未取得来源时不用新闻传言填充。",
        "sections": [
            {
                "title": "可免费补齐",
                "body": "SK Hynix、Micron、Samsung 的法说会、财报、路线图、量产/送样/认证里程碑和 CapEx 指引。",
            },
            {
                "title": "目标字段",
                "body": "vendor、generation、stack_height、period、bit_output、capacity、demand、coverage_ratio、contract_price_change、qualification_status、customer、milestone_date、source_id、confidence。",
            },
        ],
        "source_notes": [
            "位产出、供需和价格建议向 TrendForce、TechInsights、Omdia 或 SemiAnalysis 采购。"
        ],
    },
    "supply-chain-gpu": {
        "title": "AI 加速器",
        "eyebrow": "GPU and Accelerator Supply",
        "description": "跟踪 GPU、ASIC 与系统级产品路线图、出货、交付周期、库存和单位经济性。",
        "metrics": [],
        "chart_data": [],
        "analysis": "产品出货和客户分配尚无可公开展示的稳定数据源，页面不展示估算出货量。",
        "sections": [
            {
                "title": "可免费补齐",
                "body": "厂商路线图、发布日、产品规格、数据中心收入、公司指引、出口限制与已公告的云厂商部署。",
            },
            {
                "title": "目标字段",
                "body": "vendor、product、architecture、release_date、shipment_period、units_shipped、asp、lead_time_weeks、installed_base、customer、revenue、export_scope、source_id、confidence。",
            },
        ],
        "source_notes": [
            "出货、ASP、客户分配和安装基数建议购买 SemiAnalysis Accelerator Model、TechInsights 或 Omdia。"
        ],
    },
    "supply-chain-demand": {
        "title": "AI 下游需求",
        "eyebrow": "Hyperscaler and Enterprise Demand",
        "description": "跟踪四家云服务商披露的公司层面现金资本开支与财务承载能力。",
        "snapshot_contract_version": 1,
        "period_options": [
            {"value": "3y", "label": "3 年", "months": 36, "fiscal_years": 3},
            {"value": "5y", "label": "5 年", "months": 60, "fiscal_years": 5},
        ],
        "default_period": "5y",
        "tab_options": [
            {"value": "reported-capex", "label": "披露资本开支", "chart_keys": ["reported-capex"]},
            {"value": "capex-intensity", "label": "资本开支强度", "chart_keys": ["capex-intensity"]},
            {"value": "financial-capacity", "label": "财务承载能力", "chart_keys": ["financial-capacity"]},
        ],
        "default_tab": "reported-capex",
        "metrics": [],
        "chart_data": [],
        "analysis": "下游需求拆分尚未形成可比口径，在公司披露与研究估计未分层前不发布总需求数值。",
        "sections": [
            {
            "title": "披露边界",
            "body": "页面展示公司层面的 SEC 现金资本开支事实/代理指标，不是 AI-only CapEx。Amazon 使用更宽的 productive-assets 标签；GPU 数量、租赁和项目级 AI 拆分不作推断。",
            },
            {
                "title": "目标字段",
            "body": "company、period、capital_expenditures、capex_definition、revenue、gross_profit、net_income、operating_cash_flow、source_fact_id、accession_number、fetched_at、quality_status。",
            },
        ],
        "source_notes": [
            "公司披露可免费入库；客户级 GPU 部署与需求预测可询价 SemiAnalysis、Omdia 或 TechInsights。"
        ],
    },
}


for config in PAGE_CONFIGS.values():
    for key, value in COMMON.items():
        config.setdefault(key, deepcopy(value))
    config.setdefault(
        "sections",
        [
            {
                "title": "如何解读",
                "body": "先看水位，再看变化速度，最后用跨资产信号确认。单一指标不直接生成交易指令。",
            },
            {
                "title": "验证与失效",
                "body": "所有结论必须能被下一批官方数据验证，并明确显示失效条件与时间框架。",
            },
        ],
    )
    if config.get("metrics"):
        config["analysis"] = (
            "本页尚无通过来源许可与质量校验的可发布快照；"
            "不使用注册表中的原型数值或市场结论填充。"
        )


def get_page_config(key: str) -> dict:
    return deepcopy(PAGE_CONFIGS[key])
