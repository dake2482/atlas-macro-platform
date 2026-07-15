"""Auditable data-source coverage and procurement catalogue.

This file is intentionally explicit: a missing licensed feed must appear as a
product requirement, never as a plausible-looking synthetic number.
"""

from __future__ import annotations

from .models import DataRequirement

LIVE = DataRequirement.Status.LIVE
PROXY = DataRequirement.Status.PROXY
NEEDS_SOURCE = DataRequirement.Status.NEEDS_SOURCE
LICENSE_REVIEW = DataRequirement.Status.LICENSE_REVIEW
PURCHASE_REQUIRED = DataRequirement.Status.PURCHASE_REQUIRED


DATA_REQUIREMENTS = [
    {
        "key": "market-us-prices",
        "page_key": "assets-equities",
        "metric_name": "美股、ETF 与指数行情",
        "status": PURCHASE_REQUIRED,
        "vendor": "Databento / Intrinio / CTA-UTP licensed distributor",
        "product": "US Equities Mini or consolidated SIP feed with public-web display rights",
        "reason": (
            "公开网站需要外部显示/再分发权；完整行情还涉及 CTA 与 UTP "
            "Vendor Agreement。Yahoo 官方明确禁止再分发，yfinance 仅可用于开发对照。"
        ),
        "proxy_description": (
            "可使用获准公开显示的 EOD 或单交易所行情，但必须标明延迟和"
            "市场覆盖；未获权前不回退到 Yahoo 或合成价格。"
        ),
        "priority": 1,
    },
    {
        "key": "market-breadth-constituents",
        "page_key": "assets-equities",
        "metric_name": "成分股广度与 200 日均线占比",
        "status": PURCHASE_REQUIRED,
        "vendor": "S&P DJI / Nasdaq GIDS-GIW / FTSE Russell",
        "product": "Historical constituents, weights and corporate actions with derived-display rights",
        "reason": (
            "准确历史广度需要防存续者偏差的历史成分与复权行情；S&P、Nasdaq "
            "和 Russell 的成分、权重与派生展示是单独授权产品。"
        ),
        "proxy_description": (
            "改为自建的美国上市股票池广度并明确命名，不得称为 S&P 500 或 Nasdaq-100 广度。"
        ),
        "priority": 2,
    },
    {
        "key": "branded-index-data",
        "page_key": "assets-equities",
        "metric_name": "SPX、NDX、Russell 及其他品牌指数点位与历史",
        "status": PURCHASE_REQUIRED,
        "vendor": "S&P DJI / Nasdaq GIDS / FTSE Russell / Cboe Global Indices Feed",
        "product": "Index-level EOD, historical or real-time website-display licence",
        "reason": (
            "指数官网可查或有延迟图表不等于允许在 Atlas 重新发布；指数点位、"
            "历史、成分和商标使用通常需分别授权。"
        ),
        "proxy_description": "使用获许可的 SPY/QQQ/IWM 等 ETF 行情，并明确标记为代理资产。",
        "priority": 1,
    },
    {
        "key": "options-us-chain",
        "page_key": "options",
        "metric_name": "美股期权链、OI、IV 与 Greeks",
        "status": PURCHASE_REQUIRED,
        "vendor": "Cboe LiveVol-DataShop / Intrinio Enterprise / Massive Options / ORATS",
        "product": "OPRA chain plus public display, historical storage and derived-data rights",
        "reason": (
            "GEX/DEX/Vanna/Charm 需要完整期权链。当前 OPRA 数据对外展示原则上需 "
            "Vendor Agreement 或合规 Hosted Solution；业务 API 套餐不自动包含再分发权。"
        ),
        "proxy_description": (
            "可基于获许可的延迟/EOD 期权链用 BSM 重算 Greeks，所有暴露"
            "指标标记为模型估算；底层链仍必须授权。"
        ),
        "priority": 1,
    },
    {
        "key": "cftc-cot",
        "page_key": "positioning",
        "metric_name": "CFTC COT 净仓与历史百分位",
        "status": LIVE,
        "source_name": "CFTC Public Reporting Environment",
        "source_url": "https://publicreporting.cftc.gov/",
        "reason": "官方周频数据，保留周二持仓日和周五发布日。",
        "priority": 2,
    },
    {
        "key": "treasury-yield-curve",
        "page_key": "yield-curve",
        "metric_name": "美债收益率曲线与 2s10s/3m10s/5s30s",
        "status": LIVE,
        "source_name": "U.S. Treasury Daily Treasury Par Yield Curve Rates",
        "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/",
        "reason": (
            "名义与实际曲线按年度精确批次回填五年，当前、1 周、1 月、3 月"
            "曲线和关键利差只用完整共同日；任一年度组件失败时保留上一完整快照。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-yield-curve-bond-proxy",
        "page_key": "assets-bonds",
        "metric_name": "官方 Treasury Par Yield 与曲线利差",
        "status": LIVE,
        "source_name": "U.S. Treasury Daily Treasury Par Yield Curve Rates",
        "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/",
        "reason": (
            "债券入口复用 yield-curve contract v1，展示官方收益率与 Atlas 透明"
            "利差计算；明确不是债券或 ETF 价格、久期、信用利差或总回报。"
        ),
        "priority": 1,
    },
    {
        "key": "nyfed-policy-rates",
        "page_key": "fed-funds",
        "metric_name": "SOFR、EFFR、IORB 与政策走廊",
        "status": LIVE,
        "source_name": "New York Fed Markets API and Federal Reserve PRATES DDP",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": (
            "SOFR/EFFR、目标区间、1P/25P/75P/99P 与成交量取纽约联储，"
            "IORB 直接解析 Federal Reserve PRATES；页面只取三个数据集的"
            "最新非未来共同有效日，所有差值与走廊位置均保留输入日期和批次。"
            " IORB 第二官方来源为 Federal Reserve PRATES Data Download Program。"
        ),
        "priority": 1,
    },
    {
        "key": "fed-funds-futures",
        "page_key": "expectations",
        "metric_name": "Fed Funds 期货会议概率",
        "status": PURCHASE_REQUIRED,
        "vendor": "CME Group / CME-authorized distributor",
        "product": "FedWatch API licence or ZQ futures settlements under a CME ILA",
        "reason": (
            "不抓取或冒充 CME FedWatch；精确会议概率需授权 ZQ 期货价格，"
            "网站展示与派生概率必须纳入 CME Information License Agreement。"
        ),
        "proxy_description": (
            "可用获许可 ZQ 结算价按 CME 公开方法自行计算；无行情许可时"
            "改用纽约联储调查或自有情景，且标明非市场概率。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-real-rates",
        "page_key": "real-rates",
        "metric_name": "TIPS 实际利率与盈亏平衡通胀",
        "status": LIVE,
        "source_name": "U.S. Treasury real and nominal yield curve data",
        "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/",
        "reason": (
            "已直连 Treasury 名义与实际收益率曲线并自行派生盈亏平衡通胀；"
            "不再经 FRED 缓存第三方序列。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-auctions",
        "page_key": "auctions",
        "metric_name": "国债拍卖日历、结果与 Bid-to-Cover",
        "status": LIVE,
        "source_name": "U.S. Treasury FiscalData auctions_query",
        "source_url": "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/",
        "reason": (
            "官方 auction_date 近 90 天/未来 14 天与 issue_date 未来 14 天"
            "双窗口均在 meta 完整、一页覆盖且同批次时发布；公告总面值、"
            "Bid-to-Cover 和 high yield 保留组件血缘。投标与 dealer/direct/"
            "indirect 分配字段已规范化存储，但不属于当前 v1 公开展示合同。"
            "本条不包含需 when-issued 市场报价的真实 Tail。"
        ),
        "priority": 2,
    },
    {
        "key": "treasury-gross-issue-settlement-calendar",
        "page_key": "rrp-tga",
        "metric_name": "未来 7/14 天国债发行/结算公告总面值",
        "status": LIVE,
        "source_name": "U.S. Treasury FiscalData auctions_query",
        "source_url": "https://fiscaldata.treasury.gov/datasets/treasury-securities-auctions-data/",
        "reason": (
            "按 issue_date 半开区间汇总 offering_amount，并保留拍卖已完成但"
            "尚未发行/结算的证券。该值是 gross announced face amount，"
            "不是实际现金流、净融资、TGA 预测或净流动性冲击。"
        ),
        "priority": 2,
    },
    {
        "key": "treasury-future-net-financing",
        "page_key": "rrp-tga",
        "metric_name": "未来实际净融资",
        "status": NEEDS_SOURCE,
        "source_name": (
            "Treasury FiscalData Daily Treasury Statement and Monthly Statement "
            "of the Public Debt"
        ),
        "source_url": "https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/",
        "reason": (
            "公告总面值未扣除到期偿还、增发置换、非市场融资及最终结算差异，"
            "不能替代未来实际净融资。"
        ),
        "proxy_description": (
            "先以 DTS 实际 cash/debt transactions 和月度债务存量做事后核验；"
            "未来值仍须补齐到期表、非市场项目与最终结算口径。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-future-tga-cash-flow",
        "page_key": "rrp-tga",
        "metric_name": "未来 TGA 实际现金流方向与金额",
        "status": NEEDS_SOURCE,
        "source_name": "Treasury FiscalData Daily Treasury Statement",
        "source_url": "https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/",
        "reason": (
            "需要可核验的税收、支出、到期偿付、非市场项目和实际结算流水；"
            "offering_amount 不代表 TGA 流入。"
        ),
        "proxy_description": (
            "DTS 可提供已发生的 operating cash balance、deposits 和 withdrawals；"
            "未来逐日方向需财政事件表与实际结算来源，不能由拍卖面值反推。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-future-net-liquidity-impact",
        "page_key": "rrp-tga",
        "metric_name": "未来财政净流动性影响",
        "status": NEEDS_SOURCE,
        "source_name": (
            "Federal Reserve H.4.1, New York Fed Markets and Treasury FiscalData"
        ),
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": (
            "需要资金来源、RRP/准备金承接、结算时点与其他财政现金流的"
            "同口径证据；公告发行总面值不得冒充净流动性抽离。"
        ),
        "proxy_description": (
            "可在事后共同有效日计算透明余额代理；未来冲击需授权资金流/"
            "when-issued 数据与完整财政事件模型，代理不得标成官方预测。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-auction-wi-tail",
        "page_key": "auctions",
        "metric_name": "拍卖前 When-Issued 收益率与真实 Tail",
        "status": PURCHASE_REQUIRED,
        "vendor": "CME BrokerTec / LSEG-Tradeweb / Bloomberg Enterprise",
        "product": "U.S. Treasury when-issued or on-the-run market data with display rights",
        "reason": (
            "TreasuryDirect 只能提供拍卖结果；真实 Tail 需要拍卖截止前的 when-issued 市场收益率。"
        ),
        "proxy_description": (
            "拍卖高收益率减前一营业日 Treasury 官方收益率，标明为 EOD 近似、"
            "不是 WI Tail；发行公告总面值也不能作为 WI Tail 的替代数据。"
        ),
        "priority": 2,
    },
    {
        "key": "cme-futures-market-data",
        "page_key": "assets-commodities",
        "metric_name": "CME 股指、国债、SOFR、商品与加密期货行情",
        "status": PURCHASE_REQUIRED,
        "vendor": "CME Group / Databento / Kaiko for CME crypto",
        "product": "CME ILA, DataMine and public website distribution or derived-data rights",
        "reason": (
            "实时、延迟、EOD、历史、非显示计算、派生与公开网站展示是不同的"
            " CME 许可用途；网页可查数值不代表可再发布。"
        ),
        "proxy_description": (
            "使用 EIA、USDA、CFTC 等官方现货/库存/持仓数据，但不得冒充 CME 价格、结算或期限结构。"
        ),
        "priority": 1,
    },
    {
        "key": "fed-h41-balance-sheet-inputs",
        "page_key": "fed-balance-sheet",
        "metric_name": "美联储总资产、美债、MBS 与准备金",
        "status": LIVE,
        "source_name": "Federal Reserve H.4.1",
        "source_url": "https://www.federalreserve.gov/releases/h41/",
        "reason": (
            "已流式解析 Federal Reserve H.4.1 DDP 固定 ZIP，保留 Board "
            "series ID、观察状态、原始文件 SHA-256 与每周修订。"
        ),
        "priority": 1,
    },
    {
        "key": "fed-balance-sheet-public-contract",
        "page_key": "fed-balance-sheet",
        "metric_name": "资产负债表五指标、历史图与净流动性透明代理",
        "status": PROXY,
        "source_name": (
            "Federal Reserve H.4.1, New York Fed Markets API, "
            "U.S. Treasury FiscalData and Atlas Macro"
        ),
        "source_url": "https://www.federalreserve.gov/releases/h41/",
        "reason": (
            "fed-balance-sheet v1 只使用同一 H.4.1 精确批次的 WALCL、"
            "WSHOTSL、WSHOMCB、WRBWFRBL，以及同一刷新周期的最新 ON RRP "
            "和 TGA 精确批次；六序列仅在共同非未来周三发布且不做前值填充。"
            "净流动性按 WALCL − ON RRP − TGA 透明计算，是 Atlas Macro "
            "代理而非 Federal Reserve 官方指标或 LPI。"
        ),
        "priority": 1,
    },
    {
        "key": "treasury-tga",
        "page_key": "rrp-tga",
        "metric_name": "Treasury General Account",
        "status": LIVE,
        "source_name": "U.S. Treasury FiscalData Daily Treasury Statement",
        "source_url": "https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/operating-cash-balance",
        "priority": 1,
    },
    {
        "key": "nyfed-onrrp",
        "page_key": "rrp-tga",
        "metric_name": "ON RRP 接受额、利率与交易对手数",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York Markets API",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "使用 last 最近有效结果而非周末可能为空的 latest；缺失的交易对手分类不补 0。",
        "priority": 1,
    },
    {
        "key": "fed-h10-global-dollar-reference",
        "page_key": "global-dollar",
        "metric_name": "广义美元指数与 EUR/USD、USD/CNY、USD/JPY 日频参考值",
        "status": LIVE,
        "source_name": "Federal Reserve H.10 Data Download Program",
        "source_url": "https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H10",
        "reason": (
            "global-dollar v1 保存并复核完整 H.10 ZIP、唯一根目录 XML member、"
            "Prepared 发布时间、Board series 属性和四条序列最新有效日；"
            "广义美元是官方日频参考指数，不是 ICE DXY 或可交易实时现货。"
        ),
        "priority": 1,
    },
    {
        "key": "cross-currency-basis",
        "page_key": "global-dollar",
        "metric_name": "1M/3M/1Y 跨币种基差",
        "status": PURCHASE_REQUIRED,
        "vendor": "LSEG Data Platform-IPA / Bloomberg Enterprise Data",
        "product": "OTC cross-currency swap curves and FX forward points with public-display rights",
        "reason": (
            "可靠的离岸美元基差是 OTC 曲线数据；CME FX 期货或 DataMine 不能直接"
            "代替真实 cross-currency basis。"
        ),
        "proxy_description": (
            "Fed H.10/ECB 日度现货与 BIS 季度全球流动性只作美元压力代理；"
            "必须明确标注不是实时跨币种基差。"
        ),
        "priority": 1,
    },
    {
        "key": "central-bank-liquidity-swaps",
        "page_key": "global-dollar",
        "metric_name": "央行美元流动性互换在途余额",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York FX Swaps API",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": (
            "生产只接受 2007-01-01 至采集日、dateType=trade 的官方"
            "search 完整响应，并用最早交易日、历史 BOJ/ECB 记录和"
            "保守行数下限防止 last/N 被伪装成全量。在途余额按 "
            "settlementDate ≤ as_of < maturityDate 计算，small-value 技术测试"
            "单列且不进入压力解读。"
        ),
        "priority": 2,
    },
    {
        "key": "fed-h41-usd-swap-witness",
        "page_key": "global-dollar",
        "metric_name": "H.4.1 SWPT 央行流动性互换余额交叉见证",
        "status": NEEDS_SOURCE,
        "source_name": "Federal Reserve H.4.1 Data Download Program",
        "source_url": "https://www.federalreserve.gov/releases/h41/",
        "reason": (
            "SWPT 可作为 NY Fed 操作明细派生在途余额的独立官方"
            "总量见证；完成精确日期对齐、单位和修订政策审核前"
            "不进入 v1 发布公式。"
        ),
        "priority": 2,
    },
    {
        "key": "bis-global-liquidity-structural",
        "page_key": "global-dollar",
        "metric_name": "BIS 全球流动性指标的结构性美元信用",
        "status": NEEDS_SOURCE,
        "source_name": "Bank for International Settlements Global Liquidity Indicators",
        "source_url": "https://www.bis.org/statistics/gli.htm",
        "reason": (
            "季度跨境美元信用适合作为中长期结构背景，需完成"
            "SDMX 版本、修订、地区和工具口径契约；它不是实时"
            "cross-currency basis 或交易信号。"
        ),
        "priority": 3,
    },
    {
        "key": "imf-cofer-dollar-reserves-structural",
        "page_key": "global-dollar",
        "metric_name": "IMF COFER 官方外汇储备的美元份额",
        "status": NEEDS_SOURCE,
        "source_name": "International Monetary Fund COFER",
        "source_url": "https://data.imf.org/COFER",
        "reason": (
            "季度储备币种构成是美元地位的结构性背景，需审核"
            "SDMX 接口、币值/份额口径与修订标记；不得用来"
            "填充缺失的离岸美元基差。"
        ),
        "priority": 3,
    },
    {
        "key": "treasury-tic-dollar-flows-structural",
        "page_key": "global-dollar",
        "metric_name": "U.S. Treasury TIC 跨境证券与银行资金流",
        "status": NEEDS_SOURCE,
        "source_name": "U.S. Department of the Treasury TIC System",
        "source_url": "https://home.treasury.gov/data/treasury-international-capital-tic-system",
        "reason": (
            "TIC 月度流量与头寸是跨境美元需求的滞后结构证据，"
            "需完成表间净额、修订、发布日与观察期契约；它不是"
            "跨币种掉期曲线。"
        ),
        "priority": 3,
    },
    {
        "key": "economy-official-component-composite",
        "page_key": "economy",
        "metric_name": "实际 GDP 增速、失业率、核心 CPI 同比与实际 PCE 环比",
        "status": LIVE,
        "source_name": "BEA GDP/PIO releases and BLS Public Data API",
        "source_url": "https://www.bea.gov/data/gdp/gross-domestic-product",
        "reason": (
            "总览不再直接读取 CES 就业总水平或 CPI 指数；只继承 GDP、就业、"
            "通胀与消费四个已发布子页中通过许可、质量、批次和新鲜度门禁的"
            "变化率指标及对应趋势图。"
        ),
        "priority": 1,
    },
    {
        "key": "bls-employment-official",
        "page_key": "employment",
        "metric_name": "非农、失业率、劳动参与率与平均时薪",
        "status": LIVE,
        "source_name": "U.S. Bureau of Labor Statistics Public Data API",
        "source_url": "https://www.bls.gov/developers/",
        "reason": (
            "官方 CES/CPS 月度季调序列；非农新增、3M 均值与时薪同比"
            "由 Atlas Macro 按精确自然月透明派生，保留输入批次与 preliminary 标记。"
        ),
        "priority": 1,
    },
    {
        "key": "bls-jolts-official",
        "page_key": "employment",
        "metric_name": "JOLTS 职位空缺、招聘、主动离职与裁员解雇",
        "status": LIVE,
        "source_name": "U.S. Bureau of Labor Statistics JOLTS",
        "source_url": "https://www.bls.gov/jlt/",
        "reason": (
            "水平与 rate 均直接取官方季调序列；openings 是月末存量，"
            "hires/quits/layoffs 是整月流量，不混合求和或用四舍五入水平重算 rate。"
        ),
        "priority": 1,
    },
    {
        "key": "dol-weekly-claims",
        "page_key": "employment",
        "metric_name": "全国季调初请、续请与官方 4 周均值",
        "status": LIVE,
        "source_name": (
            "U.S. Department of Labor, Employment and Training Administration"
        ),
        "source_url": "https://oui.doleta.gov/unemploy/claims.asp",
        "reason": (
            "ETA 539 全国 XML 负责长期历史，当周不可变新闻稿 PDF 负责"
            "advance 值及修订尾部；两份原始响应均保留 SHA-256。续请表示"
            "continued weeks claimed，不代表唯一领取人数。"
        ),
        "priority": 1,
    },
    {
        "key": "employment-vintage-trail",
        "page_key": "employment",
        "metric_name": "CES/CPS/JOLTS/DOL 可查询发布 vintage 与历次修订路径",
        "status": NEEDS_SOURCE,
        "vendor": "BLS public-use vintage tables / ALFRED where applicable / internal archive",
        "product": "Release-vintage observations with revision-round identifiers",
        "reason": (
            "当前已保留抓取批次和 DOL XML/PDF 指纹，但通用 Observation 仍会以"
            "最新官方值覆盖同一经济期；尚未建立可查询的 CES/CPS/JOLTS 完整"
            "发布轮次层，因此不宣称已完成 vintage 复现。"
        ),
        "proxy_description": (
            "每次抓取保留响应哈希和当前批次；在可查询 vintage 层完成前，"
            "页面只展示当前官方 vintage 及 preliminary/advance 状态。"
        ),
        "priority": 2,
    },
    {
        "key": "bls-inflation-official",
        "page_key": "inflation",
        "metric_name": (
            "CPI、核心 CPI 与最终需求 PPI 的环比、同比及 3M/6M 年化"
        ),
        "status": LIVE,
        "source_name": "U.S. Bureau of Labor Statistics Public Data API",
        "source_url": "https://www.bls.gov/developers/",
        "reason": (
            "环比和短期动能使用季调指数，同比使用对应未季调指数；"
            "全部按精确自然月透明派生并绑定同一 BLS 抓取批次，保留"
            "输入序列、许可、preliminary 与 fallback 血缘。"
        ),
        "priority": 1,
    },
    {
        "key": "bea-pce-inflation",
        "page_key": "inflation",
        "metric_name": "PCE 与核心 PCE 价格指数通胀率",
        "status": LIVE,
        "source_name": "U.S. Bureau of Economic Analysis Personal Income and Outlays",
        "source_url": "https://www.bea.gov/data/income-saving/personal-income",
        "reason": (
            "BEA PIO Section 2 T20804-M 工作簿解析 PCE 与核心 PCE chain-type "
            "price index；环比、同比和 3M/6M 年化均按精确自然月透明派生，"
            "并与 BLS CPI/PPI 一起进入通胀页同批发布门。"
        ),
        "proxy_description": "不以 CPI、实际 PCE 增速或演示值替代 PCE 价格通胀。",
        "priority": 2,
    },
    {
        "key": "bls-inflation-components",
        "page_key": "inflation",
        "metric_name": "住房、商品与服务通胀分项",
        "status": LIVE,
        "source_name": "U.S. Bureau of Labor Statistics CPI detailed indexes",
        "source_url": "https://www.bls.gov/cpi/data.htm",
        "reason": (
            "已冻结 BLS Shelter（SAH1）、Commodities less food and energy "
            "commodities（SACL1E）与 Services less energy services（SASLE）"
            "的季调/未季调配对。环比和短周期动能用季调指数，"
            "同比用未季调指数，全部绑定同一 BLS 批次。服务口径仍"
            "包含 Shelter，因此不冒充“超级核心”。"
        ),
        "priority": 2,
    },
    {
        "key": "inflation-market-expectations",
        "page_key": "inflation",
        "metric_name": "5Y/10Y 盈亏平衡通胀与远期通胀预期",
        "status": LIVE,
        "source_name": "U.S. Treasury nominal and real yield curves",
        "source_url": (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        ),
        "reason": (
            "通胀页复用 real-rates 同一 Treasury 名义与 TIPS par curve 快照，"
            "发布 5Y/10Y 名义减实际的 BEI 代理，并明确不是实时可交易"
            " breakeven 或 5Y5Y。"
        ),
        "proxy_description": (
            "已接入同日 5Y/10Y Treasury−TIPS 历史代理；5Y5Y 远期口径"
            "仍需另行验证 FRED/授权市场源。"
        ),
        "priority": 3,
    },
    {
        "key": "inflation-vintage-trail",
        "page_key": "inflation",
        "metric_name": "CPI/PPI 发布 vintage 与历次修订路径",
        "status": NEEDS_SOURCE,
        "vendor": "BLS release archive / ALFRED where applicable / internal archive",
        "product": "Release-vintage observations with revision-round identifiers",
        "reason": (
            "当前通用 Observation 会以本次官方值更新同一经济月份；"
            "批次、preliminary 标记和当前响应证据不能替代可查询的完整"
            "发布修订轨迹，因此本页只声明最新官方 vintage。"
        ),
        "priority": 3,
    },
    {
        "key": "bea-gdp-pce",
        "page_key": "gdp",
        "metric_name": "GDP、GDI、PCE 与分项的最新修订口径",
        "status": LIVE,
        "source_name": "U.S. Bureau of Economic Analysis GDP release workbooks",
        "source_url": "https://www.bea.gov/data/gdp/gross-domestic-product",
        "reason": (
            "已直接解析 BEA 官方 GDP vintage-history 与 Historical Comparisons XLSX，"
            "发布最新官方 vintage，保留当前估算轮次、发布日和原文件哈希。"
        ),
        "priority": 1,
    },
    {
        "key": "bea-gdp-contributions",
        "page_key": "gdp",
        "metric_name": "PCE、投资、净出口与政府对 GDP 增长的贡献",
        "status": LIVE,
        "source_name": "U.S. Bureau of Economic Analysis GDP Historical Comparisons",
        "source_url": "https://www.bea.gov/data/gdp/gross-domestic-product",
        "reason": (
            "直接解析官方工作簿的 Contributions to Percent Change 区块，"
            "将百分点贡献与分项增速分开存储，不由增速倒推。"
        ),
        "priority": 1,
    },
    {
        "key": "bea-gdp-vintage-trail",
        "page_key": "gdp",
        "metric_name": "GDP Advance→Second→Third 估算修订轨迹",
        "status": LIVE,
        "source_name": "U.S. Bureau of Economic Analysis GDP/GDI Vintage History",
        "source_url": "https://apps.bea.gov/national/xls/gdp-gdi-vintage-history.xlsx",
        "reason": (
            "独立 release-vintage 数据层按观察季度、官方发布日期和估算轮次保存"
            "全部有效 GDP/GDI 记录；当前指标仍只取每季度最新轮次，GDP 页面另行"
            "展示最近季度的完整修订路径，并保留原工作簿哈希和抓取批次。"
        ),
        "priority": 2,
    },
    {
        "key": "census-retail",
        "page_key": "consumer",
        "metric_name": "零售与餐饮服务销售（水平/环比/同比）",
        "status": LIVE,
        "source_name": "U.S. Census Bureau MARTS release workbook",
        "source_url": "https://www.census.gov/retail/sales.html",
        "reason": (
            "Census 发布工作簿以 append-only 精确批次保留原文件"
            "哈希、发布状态、当前三项指标和尾部；Census API 只在"
            "1992-01 起连续且三序列重叠逐项一致时扩展更早历史。"
        ),
        "priority": 1,
    },
    {
        "key": "bea-personal-income-outlays",
        "page_key": "consumer",
        "metric_name": "实际 PCE 环比、可支配个人收入与个人储蓄率",
        "status": LIVE,
        "source_name": "U.S. Bureau of Economic Analysis PIO and NIPA Section 2",
        "source_url": "https://www.bea.gov/data/income-saving/personal-income",
        "reason": (
            "已解析免密的 NIPA Section 2 月度完整工作簿，并用当月 Historical "
            "Comparisons 摘要交叉校验观测月、发布日期和三个核心值；发布当前官方 "
            "vintage，保留两个原始工作簿与产品页哈希。"
        ),
        "priority": 1,
    },
    {
        "key": "bea-pio-vintage-trail",
        "page_key": "consumer",
        "metric_name": "PIO 各发布日期之间的可查询修订轨迹",
        "status": NEEDS_SOURCE,
        "source_name": "U.S. Bureau of Economic Analysis archived PIO releases",
        "source_url": "https://www.bea.gov/news/current-releases",
        "reason": (
            "当前已按采集批次 append-only 保留 Observation 与精确"
            "RawArtifact，但尚无将不同批次规范化为可查询修订轨迹的"
            "跨批次 vintage 产品；因此不对外声明完整修订路径。"
        ),
        "priority": 2,
    },
    {
        "key": "census-retail-history",
        "page_key": "consumer",
        "metric_name": "MARTS 零售销售完整月度历史回填",
        "status": NEEDS_SOURCE,
        "source_name": "U.S. Census Bureau EITS MARTS API",
        "source_url": "https://api.census.gov/data/timeseries/eits/marts.html",
        "reason": (
            "代码已要求 1992-01 起连续完整月度水平，并从同批水平透明计算环比/同比；"
            "生产 CENSUS_API_KEY 配置、回填、数量核验和页面验收完成前维持 NEEDS_SOURCE。"
        ),
        "priority": 2,
    },
    {
        "key": "consumer-credit-official",
        "page_key": "consumer",
        "metric_name": "消费者信贷、家庭债务与偿债缓冲",
        "status": LIVE,
        "source_name": "Federal Reserve G.19 and New York Fed Household Debt and Credit",
        "source_url": "https://www.federalreserve.gov/releases/g19/current/",
        "reason": (
            "已接入联储 G.19 全历史季调 CSV，以及纽约联储季度家庭债务"
            "和 90+ 天逾期工作簿；保留文件哈希、原始序列 ID、发布日和"
            "New York Fed Consumer Credit Panel / Equifax 归因。G.19 不含以"
            "房地产抵押的信贷，总量数据不用于推断特定收入群体压力。"
        ),
        "priority": 1,
    },
    {
        "key": "consumer-credit-vintage-trail",
        "page_key": "consumer",
        "metric_name": "G.19 与家庭债务各发布批次的可查询修订轨迹",
        "status": NEEDS_SOURCE,
        "source_name": "Federal Reserve and New York Fed archived releases",
        "source_url": "https://www.federalreserve.gov/releases/g19/revisions.htm",
        "reason": (
            "G.19 与 HHDC 已按批次 append-only 保留完整 Observation "
            "和原文件哈希，但尚无规范化的跨批次 vintage 产品；"
            "批次重放能证明当时数值，不等于已发布官方修订轨迹。"
        ),
        "priority": 2,
    },
    {
        "key": "consumer-confidence",
        "page_key": "consumer",
        "metric_name": "消费者信心指数与调查分项",
        "status": PURCHASE_REQUIRED,
        "vendor": "University of Michigan Surveys of Consumers / The Conference Board",
        "product": "Consumer-confidence history and public website-display rights",
        "reason": (
            "这些调查指数不是美国政府开放数据；官网或新闻中可见的最新数值"
            "不等于可建库并向公众再发布历史序列。"
        ),
        "proxy_description": (
            "可用 Census 零售、BEA 实际 PCE 和储蓄率描述实际消费行为，"
            "但不命名为消费者信心。"
        ),
        "priority": 2,
    },
    {
        "key": "macro-consensus-private-surveys",
        "page_key": "economy",
        "metric_name": "宏观一致预期、经济学家调查与私营 PMI/信心指数",
        "status": PURCHASE_REQUIRED,
        "vendor": (
            "LSEG Reuters Economic Polls / Trading Economics Enterprise / "
            "S&P Global PMI / University of Michigan / The Conference Board"
        ),
        "product": "Consensus, survey and index data with website-display and archival rights",
        "reason": (
            "调查中值、历史预测、PMI、消费者信心等并非政府开放数据；"
            "终端可见或新闻引用不授予数据库存储和公开再发布权。"
        ),
        "proxy_description": (
            "免费阶段仅展示 BLS、BEA、Census 等官方实际值以及自有情景；"
            "不得将单篇新闻中的预测值拼成所谓市场一致预期。"
        ),
        "priority": 3,
    },
    {
        "key": "fx-vol-h10-realized",
        "page_key": "fx-vol",
        "metric_name": "H.10 Broad Dollar 与三条参考汇率 20D/60D 实现波动率",
        "status": PROXY,
        "source_name": "Federal Reserve H.10 plus Atlas Macro transparent calculation",
        "source_url": "https://www.federalreserve.gov/releases/h10/current/",
        "reason": (
            "使用可重放 H.10 官方参考 level 的相邻有效观察 log return、"
            "样本标准差和 sqrt(252) 年化；这是实现波动率，不是期权 IV。"
        ),
        "proxy_description": (
            "页面明确标为 H.10 reference-level realized volatility，"
            "不替代 ATM IV、risk reversal、butterfly 或可执行外汇报价。"
        ),
        "priority": 1,
    },
    {
        "key": "volatility-treasury-rv-precondition",
        "page_key": "volatility-dashboard",
        "metric_name": "Treasury 收益率变动实现波动率严格输入",
        "status": LIVE,
        "source_name": "U.S. Treasury nominal par-yield curve",
        "source_url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
        "reason": (
            "Treasury curve v2 已私有保存 exact XML、逐年度 append-only Observation，"
            "并由 retained selector 从原始文件重放；本项只表示输入合同就绪，"
            "尚未发布任何 Treasury 波动率数字。"
        ),
        "proxy_description": (
            "未来只能命名为 Treasury yield-change realized volatility，"
            "不得称为 MOVE、债券价格波动率或隐含波动率。"
        ),
        "priority": 1,
    },
    {
        "key": "volatility-cross-asset-parent",
        "page_key": "volatility",
        "metric_name": "跨资产严格波动率父级组合",
        "status": NEEDS_SOURCE,
        "source_name": "Atlas Macro strict child snapshots",
        "source_url": "https://www.federalreserve.gov/releases/h10/current/",
        "reason": (
            "至少需要两个异资产子快照可独立重验；当前仅 H.10 FX child 就绪，"
            "因此不发布综合分、状态或交易信号。"
        ),
        "priority": 1,
    },
    {
        "key": "vix-history",
        "page_key": "vix",
        "metric_name": "VIX 现货、分位与期限结构",
        "status": PURCHASE_REQUIRED,
        "vendor": "Cboe Global Indices Feed / Cboe DataShop-CFE",
        "product": "VIX-family index and VX futures data with public website-display rights",
        "reason": (
            "Cboe 官网的历史下载只用于查询，不等于 Atlas 可再发布；VIX、VIX9D、"
            "VXTLT 等指数与 VX 期限结构需分别覆盖指数和 CFE 行情许可。"
        ),
        "proxy_description": (
            "可用已授权 SPY 价格自行计算实现波动率，并明确命名为实现波动率；"
            "不得标注成 VIX 或仿造 VX 期限结构。"
        ),
        "priority": 1,
    },
    {
        "key": "move-index",
        "page_key": "volatility-move",
        "metric_name": "ICE BofA MOVE Index",
        "status": PURCHASE_REQUIRED,
        "vendor": "ICE Data Indices",
        "product": "MOVE Index licence covering history, storage and public website display",
        "reason": (
            "MOVE 是 ICE Data Indices 的品牌指数；终端权限、媒体引用或网页可见"
            "均不自动授予 Atlas 存储和公开展示历史点位的权利。"
        ),
        "proxy_description": (
            "可用 Treasury 官方收益率自行计算债券实现波动率并明确标注为自有代理，不得命名为 MOVE。"
        ),
        "priority": 1,
    },
    {
        "key": "credit-oas",
        "page_key": "credit-spreads",
        "metric_name": "IG/HY 分评级 OAS",
        "status": PURCHASE_REQUIRED,
        "vendor": "ICE Data Indices",
        "product": "ICE BofA bond-index OAS data with storage and external-display rights",
        "reason": (
            "ICE BofA OAS 序列受第三方指数许可约束；FRED API 和 FRED 页面"
            "不替代 ICE 的版权、入库或公开再分发授权。"
        ),
        "proxy_description": (
            "可展示 Treasury HQM 月度公司债曲线，以及获许可的 HYG/LQD 价格派生"
            "压力指标；必须标注为代理，不能称为 ICE BofA OAS。"
        ),
        "priority": 1,
    },
    {
        "key": "credit-hqm-proxy",
        "page_key": "credit-spreads",
        "metric_name": "Treasury HQM 高质量企业债月均 Par Yield 曲线",
        "status": PROXY,
        "source_name": "U.S. Treasury HQM Corporate Bond Yield Curve",
        "source_url": (
            "https://home.treasury.gov/data/treasury-coupon-issues-and-corporate-bond-"
            "yield-curve/corporate-bond-yield-curve"
        ),
        "reason": "已接入 2Y/5Y/10Y/30Y 月均 par yield；这是高质量企业债收益率代理，不是 OAS、不含国债利差。",
        "proxy_description": "仅用于观察高质量企业融资水平；ICE BofA 分评级 OAS 仍须采购。",
        "priority": 2,
    },
    {
        "key": "credit-sloos",
        "page_key": "credit-stress",
        "metric_name": "SLOOS 贷款标准与需求",
        "status": LIVE,
        "source_name": "Federal Reserve Senior Loan Officer Opinion Survey DDP",
        "source_url": "https://www.federalreserve.gov/data/sloos.htm",
        "reason": "直接解析 Board DDP 季度 SDMX，显示净收紧标准与贷款需求，保留调查口径和季度日期。",
        "priority": 1,
    },
    {
        "key": "credit-nfci-license",
        "page_key": "credit-stress",
        "metric_name": "Chicago Fed NFCI / ANFCI",
        "status": LICENSE_REVIEW,
        "source_name": "Federal Reserve Bank of Chicago NFCI",
        "source_url": "https://www.chicagofed.org/research/data/nfci/current-data",
        "vendor": "Federal Reserve Bank of Chicago permissions",
        "product": "Written permission for commercial public republication",
        "reason": "官方 CSV 技术可用，但现行 Legal Notices 仅明确允许署名的非商业复制；公开商业网站展示前需书面许可。",
        "priority": 2,
    },
    {
        "key": "cdx-cds",
        "page_key": "credit-cds",
        "metric_name": "CDX IG/HY 与单名 CDS",
        "status": PURCHASE_REQUIRED,
        "vendor": "S&P Global CDS Pricing-Markit ICE Settlement Prices / LSEG",
        "product": "Composite CDS and CDX pricing with derived-data and public-display rights",
        "reason": (
            "CDX 与单名 CDS 的可比 composite、曲线和历史为商业估值数据；"
            "单一 SEF 成交或新闻报价不等于完整市场序列。"
        ),
        "proxy_description": (
            "SEC SDR/SEF 单笔成交不等于 composite；HYG、LQD、银行 ETF "
            "或国债变化只能另行命名为方向代理，不得发布为 CDX/CDS 数字。"
        ),
        "priority": 1,
    },
    {
        "key": "credit-trace-pricing",
        "page_key": "credit",
        "metric_name": "FINRA TRACE 全量成交、定价与流动性派生",
        "status": PURCHASE_REQUIRED,
        "vendor": "FINRA TRACE / licensed fixed-income data distributor",
        "product": "Bulk history with storage, derived analytics and public-display rights",
        "reason": "公开查询界面不等于可批量建库、派生计算并向公众再分发。",
        "proxy_description": "Treasury HQM 只描述高质量企业债 par yield，不替代 TRACE 成交或流动性。",
        "priority": 2,
    },
    {
        "key": "credit-licensed-market-proxies",
        "page_key": "credit",
        "metric_name": "HYG/LQD、银行 ETF 与授权跨资产行情",
        "status": PURCHASE_REQUIRED,
        "vendor": "Exchange-authorized / LSEG / FactSet / Bloomberg market data",
        "product": "Price caching, derived analytics and public website-display rights",
        "reason": "即使只作方向代理，行情缓存、历史存储和网站派生展示仍需明确授权。",
        "proxy_description": "未授权前不用 Yahoo/FRED 镜像回退，也不用 ETF 数值冒充 OAS 或 CDS。",
        "priority": 2,
    },
    {
        "key": "credit-issuance-ratings-defaults",
        "page_key": "credit",
        "metric_name": "信用发行、评级变化与违约事件",
        "status": PURCHASE_REQUIRED,
        "vendor": "Bloomberg / LSEG / Dealogic / Moody's / S&P / Fitch",
        "product": "Structured issuance and credit-event history with public-display rights",
        "reason": "发行、评级与违约页面继续返回 410，直到取得可建库与公开展示的结构化许可。",
        "priority": 2,
    },
    {
        "key": "crypto-spot-perps",
        "page_key": "crypto-derivatives",
        "metric_name": "BTC 现货、永续 Funding 与 OI",
        "status": LICENSE_REVIEW,
        "vendor": "OKX data licensing / Kaiko",
        "product": "Spot and derivatives feed with written public-display and redistribution rights",
        "reason": (
            "OKX 公共 API 的技术可访问性不等于允许商业公开再展示、长期缓存或"
            "跨交易所聚合；上线前需书面确认，或采购 Kaiko 等授权再分销源。"
        ),
        "proxy_description": (
            "法律审核通过前仅用于开发对照；如按交易所单独展示，仍需保留来源、"
            "延迟和条款版本，不得宣称全市场聚合。"
        ),
        "priority": 1,
    },
    {
        "key": "crypto-options",
        "page_key": "crypto-derivatives",
        "metric_name": "BTC 期权 IV、Skew、PCR 与 Max Pain",
        "status": LICENSE_REVIEW,
        "vendor": "Deribit data licensing / Kaiko",
        "product": "Options chain with written public-display, storage and derived-data rights",
        "reason": (
            "Deribit 公共 API 文档不单独构成 Atlas 的公开再分发许可；期权链、"
            "历史存储及 IV/Skew/Max Pain 等派生展示均需确认权利。"
        ),
        "proxy_description": (
            "取得底层链许可后可自行计算并标注模型假设；未获权前不发布由公开 API "
            "批量缓存得到的历史期权面。"
        ),
        "priority": 1,
    },
    {
        "key": "btc-etf-flows",
        "page_key": "crypto-derivatives",
        "metric_name": "美国现货 BTC ETF 资金流",
        "status": PURCHASE_REQUIRED,
        "vendor": "FactSet Funds / LSEG Lipper / CoinGlass Enterprise; Farside by written permission",
        "product": "Daily primary-market fund flows with public-display and archival rights",
        "reason": (
            "精确日流量是商业基金流数据；Farside 网页可读不等于可复制历史表，"
            "CoinGlass 也需 Enterprise/定制再分发授权。"
        ),
        "proxy_description": (
            "可用发行人官方 shares outstanding 与 NAV 的日变动估算申赎并标注"
            "估算口径、缺失日和价格效应，不称为精确净流入。"
        ),
        "priority": 2,
    },
    {
        "key": "coinglass-liquidations",
        "page_key": "crypto-derivatives",
        "metric_name": "全市场清算、聚合 OI 与多空比",
        "status": PURCHASE_REQUIRED,
        "vendor": "CoinGlass Enterprise / Kaiko / Coin Metrics",
        "product": "Aggregated derivatives feed with explicit external-redistribution rights",
        "reason": (
            "CoinGlass Standard/Professional 的商业使用不等于可向公众再分发；"
            "公开产品需 Enterprise 或书面定制授权，并核对交易所上游权利。"
        ),
        "proxy_description": (
            "可分别接入已通过条款审核的交易所数据并展示逐场所指标；"
            "不能据此声称覆盖全市场，也不合成不存在的清算数据。"
        ),
        "priority": 2,
    },
    {
        "key": "official-fed-documents",
        "page_key": "fed",
        "metric_name": "FOMC 声明、演讲与公告",
        "status": LIVE,
        "source_name": "Federal Reserve official RSS and documents",
        "source_url": "https://www.federalreserve.gov/feeds/feeds.htm",
        "reason": "官方 RSS 元数据、原文链接和去重已接入；鹰鸽评分仍须引用原文并经审核。",
        "priority": 2,
    },
    {
        "key": "fed-hawkish-dovish",
        "page_key": "fed-hawkish-dovish",
        "metric_name": "美联储文本证据绑定、鹰鸽分类与人工审核",
        "status": NEEDS_SOURCE,
        "source_name": "Atlas Macro analysis pipeline (not yet scheduled)",
        "reason": (
            "Federal Reserve 官方 RSS 元数据与原文链接已接入，但证据绑定分类、"
            "模型与提示词版本记录、鹰鸽评分和人工审核尚未形成定时生产闭环。"
            "未满足完整 provenance 的旧摘要与分数不会公开。"
        ),
        "proxy_description": (
            "在闭环完成前仅展示明确标注的官方 RSS 描述；完整 AI 生成记录可标为"
            "未人工审核，只有人工审核记录进入综合平均分。"
        ),
        "priority": 2,
    },
    {
        "key": "licensed-news",
        "page_key": "news",
        "metric_name": "宏观与市场新闻流",
        "status": PURCHASE_REQUIRED,
        "vendor": "Reuters Connect / LSEG News / Dow Jones Newswires-Factiva",
        "product": "Publication or external-portal licence with display, archive and excerpt rights",
        "reason": (
            "标准桌面、终端或内部 feed 通常不允许把新闻发布到公开网站；"
            "Reuters 需 Connect/出版授权，Dow Jones 需明确公开数字产品用途。"
        ),
        "proxy_description": (
            "免费阶段仅接政府、央行、公司等官方 RSS，保存标题、时间、来源、外链"
            "和自有摘要；Google News 只作发现入口，不抓取或托管媒体正文。"
        ),
        "priority": 2,
    },
    {
        "key": "official-government-news",
        "page_key": "news",
        "metric_name": "SEC、U.S. Treasury 与 BLS 官方发布元数据",
        "status": LIVE,
        "source_name": "SEC Press Releases, Treasury GovDelivery and BLS official feeds",
        "source_url": "https://www.sec.gov/news/pressreleases.rss",
        "reason": (
            "只保存标题、时间、固定来源、HTTPS 白名单原文链接和类别；"
            "忽略 feed description/content/summary，不托管正文。"
        ),
        "priority": 1,
    },
    {
        "key": "sellside-research",
        "page_key": "research",
        "metric_name": "投行研报元数据与机构观点",
        "status": PURCHASE_REQUIRED,
        "vendor": "FactSet Research Connect-Aftermarket Research / S&P Investment Research / AlphaSense",
        "product": "Contributor entitlements plus rights for the intended user and display surface",
        "reason": (
            "研报权限通常按贡献机构、公司和命名用户授予，并不允许公开网页展示；"
            "即使企业订阅可检索，也不能批量托管正文或 PDF。"
        ),
        "proxy_description": (
            "公开版只保存机构、标题、日期、资产、立场、原文链接和 Atlas 原创摘要；"
            "正文仅可在供应商授权的登录环境中按用户权限打开。"
        ),
        "priority": 2,
    },
    {
        "key": "berkshire-shareholder-letter-index",
        "page_key": "fund-letters",
        "metric_name": "Berkshire Hathaway 1977–2024 股东信官方链接元数据",
        "status": LIVE,
        "source_name": "Berkshire Hathaway official shareholder-letter index",
        "source_url": "https://www.berkshirehathaway.com/letters/letters.html",
        "reason": "仅保存报告年度、第一方链接和核验哈希；不抓取、托管或生成信函正文摘要，未知发布日期保持空缺。",
        "priority": 3,
    },
    {
        "key": "fund-letters",
        "page_key": "fund-letters",
        "metric_name": "基金信函元数据与原创中文摘要",
        "status": NEEDS_SOURCE,
        "source_name": "Fund official websites",
        "reason": (
            "Berkshire 官方链接元数据已接入；其他基金仍需官网白名单、许可记录和删除机制。"
            "公开可下载不等于允许 Atlas 再次托管，尤其不能默认镜像 PDF。"
        ),
        "proxy_description": "仅保存元数据、官方外链和基于合法阅读的原创摘要，不托管原文或 PDF。",
        "priority": 3,
    },
    {
        "key": "sec-company-fundamentals",
        "page_key": "ai-company",
        "metric_name": "公司财务与申报（reviewed SEC 四家公司 / 219 家合同公司）",
        "status": LIVE,
        "source_name": "SEC EDGAR submissions and company facts",
        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "reason": "LIVE 仅覆盖 reviewed 的 4/219 家公开公司合同；其余 215 家由 remaining-company-fundamentals 独立保持 NEEDS_SOURCE。",
        "priority": 2,
    },
    {
        "key": "ai-company-prices",
        "page_key": "ai-company",
        "metric_name": "AI 产业公司 K 线与估值",
        "status": PURCHASE_REQUIRED,
        "vendor": "LSEG / FactSet / S&P Capital IQ / exchange-authorized distributors",
        "product": "Global equities prices with exchange-level public-display rights",
        "reason": (
            "219 家公司跨美国、欧洲和亚洲交易所；全球供应商合同还须覆盖各交易所"
            "延迟/实时展示、历史存储、公司行为及币种统一，不以 Yahoo 作回退。"
        ),
        "proxy_description": (
            "免费阶段仅纳入已取得公开展示权的市场；其余公司显示财务和官方 IR "
            "链接，不生成合成 K 线或估值。"
        ),
        "priority": 1,
    },
    {
        "key": "global-company-fundamentals",
        "page_key": "ai-company",
        "metric_name": "非美公司标准化财务、公司行为与估值口径",
        "status": PURCHASE_REQUIRED,
        "vendor": "S&P Capital IQ-Compustat / FactSet Fundamentals / LSEG Fundamentals",
        "product": "Global fundamentals with storage, derived analytics and website-display rights",
        "reason": (
            "SEC Company Facts 只覆盖美国申报且标签口径不统一；全球横向比较需要"
            "标准化财务、拆股分红、币种和报告期映射的商业数据权利。"
        ),
        "proxy_description": (
            "人工解析公司 IR、交易所公告和当地官方申报，只展示已核验字段并保留"
            "原文链接；缺失字段留空，不用推测值补齐。"
        ),
        "priority": 2,
    },
    {
        "key": "analyst-estimates-ratings",
        "page_key": "ai-company",
        "metric_name": "一致预期、目标价与分析师评级",
        "status": PURCHASE_REQUIRED,
        "vendor": "S&P Capital IQ Estimates / FactSet Consensus / LSEG I-B-E-S",
        "product": "Consensus estimates and recommendations with external-display rights",
        "reason": (
            "一致预期、目标价和评级为贡献者及聚合商授权数据；终端查询权不等于"
            "可将逐公司历史和分析师明细发布到公共网站。"
        ),
        "proxy_description": (
            "只展示公司官方 guidance、实际财报和 Atlas 自有情景，明确标注非市场"
            "一致预期，不从媒体报道拼接共识。"
        ),
        "priority": 2,
    },
    {
        "key": "supply-chain-relationships",
        "page_key": "ai-industry-graph",
        "metric_name": "AI 供应链客户、供应商、产能与依赖关系",
        "status": PURCHASE_REQUIRED,
        "vendor": "FactSet Revere Supply Chain / S&P Business Relationships-Panjiva / Bloomberg Supply Chain",
        "product": "Entity-resolved supply-chain relationships with storage and public-derived-display rights",
        "reason": (
            "完整客户供应商网络、关系强度与历史变化是商业实体解析数据；采购时须确认"
            "是否可在公开图谱展示原始边、派生分数及供应商命名。"
        ),
        "proxy_description": (
            "自建关系库仅录入 SEC、公司 IR、交易所公告等明确披露的边，每条保存"
            "证据 URL、披露日期、关系类型、置信度和人工审核状态。"
        ),
        "priority": 1,
    },
    {
        "key": "model-benchmarks-pricing",
        "page_key": "model-evolution",
        "metric_name": "模型能力、价格与发布时间线",
        "status": PURCHASE_REQUIRED,
        "vendor": "Artificial Analysis Commercial / LMArena by written permission",
        "product": "Benchmark scores and history with API, archival and external-display rights",
        "reason": (
            "第三方模型榜的网页可见分数、排名和历史不等于可复制数据库；公开产品应"
            "采购 Artificial Analysis 商用授权，并就 LMArena 数据取得书面许可。"
        ),
        "proxy_description": (
            "可自行运行 SWE-bench、Terminal-Bench 等开放基准并公开方法、提交、环境"
            "和时间；模型价格与发布时间只取厂商官方文档，不混成第三方综合榜。"
        ),
        "priority": 2,
    },
    {
        "key": "model-vendor-metadata",
        "page_key": "model-evolution",
        "metric_name": "模型官方价格、上下文长度、发布日期与退役时间",
        "status": LIVE,
        "source_name": "Model-provider official pricing, release and deprecation documentation",
        "reason": (
            "12 个模型与 11 个 Coding Agent 的合同路由已使用厂商一手文档建立资料目录；"
            "仅录入能按单一口径核验的发布日、上下文和 API 标价，未核验字段留空。"
        ),
        "priority": 1,
    },
    {
        "key": "ai-supply-chain-node-taxonomy",
        "page_key": "ai-industry-graph",
        "metric_name": "AI 产业链 45 节点、9 层资料目录",
        "status": LIVE,
        "source_name": "Official company, standards-body and government documentation",
        "reason": (
            "45 个公开路由已以 9 层原创分类和一手来源链接建立资料目录；"
            "未取得完整公司关系证据前，不填叙事分、财务汇总、估值或投资 Thesis。"
        ),
        "priority": 1,
    },
    {
        "key": "github-project-radar",
        "page_key": "applications",
        "metric_name": "GitHub stars、forks、issues 与周增量",
        "status": LIVE,
        "source_name": "GitHub REST API",
        "source_url": "https://docs.github.com/en/rest",
        "reason": (
            "已接入 45 个经审核真实仓库的低频公开元数据并保存每日快照；"
            "依据 2026-04-27 生效的 GitHub API Terms，仅用于署名研究展示，不做高吞吐、"
            "转售、垃圾信息或个人数据销售。仓库描述和内容仍归属各权利人及项目许可。"
        ),
        "priority": 2,
    },
    {
        "key": "daily-judgment-evidence",
        "page_key": "home",
        "metric_name": "今日判断、三项证据、触发器与证伪闭环",
        "status": NEEDS_SOURCE,
        "source_name": "Atlas Macro reviewed analysis over complete official/licensed batches",
        "reason": "真实研判必须引用可追溯 Observation/MetricSnapshot；离线种子内容已停止公开。",
        "priority": 1,
    },
    {
        "key": "trade-map-inputs",
        "page_key": "trade-map",
        "metric_name": "受益/回避资产、风险预算和跨资产确认矩阵",
        "status": PURCHASE_REQUIRED,
        "vendor": "Databento / Intrinio / licensed multi-asset provider",
        "product": "Cross-asset delayed/EOD display and derived-analytics licence",
        "reason": "交易地图依赖同批次股票、ETF、利率、商品和外汇价格，不能由静态观点填充。",
        "priority": 1,
    },
    {
        "key": "etf-holdings-flows",
        "page_key": "assets-etfs",
        "metric_name": "ETF 行情、AUM、持仓与申赎资金流",
        "status": PURCHASE_REQUIRED,
        "vendor": "FactSet Funds / LSEG Lipper / licensed issuer feeds",
        "product": "ETF prices, holdings, AUM and flows with public-display rights",
        "reason": "行情、持仓、AUM 与 Flow 是不同授权范围，需逐项确认。",
        "priority": 1,
    },
    {
        "key": "bond-market-prices",
        "page_key": "assets-bonds",
        "metric_name": "国债与信用债价格、久期和总回报",
        "status": PURCHASE_REQUIRED,
        "vendor": "FINRA TRACE / LSEG / Bloomberg / FactSet",
        "product": "Fixed-income pricing and public-display licence",
        "reason": "财政部收益率曲线可免费使用，但不能替代债券/ETF 可交易价格与总回报。",
        "priority": 2,
    },
    {
        "key": "fed-h10-fx-reference",
        "page_key": "assets-fx",
        "metric_name": "广义美元指数与主要货币日频参考汇率",
        "status": LIVE,
        "source_name": "Federal Reserve H.10 Data Download Program",
        "source_url": "https://www.federalreserve.gov/datadownload/Choose.aspx?rel=H10",
        "reason": "直接解析 H.10 广义美元、EUR/USD、USD/CNY、USD/JPY；明确标为日频官方参考值，不冒充 DXY 或可交易实时现货。",
        "priority": 2,
    },
    {
        "key": "fx-ice-dxy",
        "page_key": "assets-fx",
        "metric_name": "ICE U.S. Dollar Index (DXY)",
        "status": PURCHASE_REQUIRED,
        "vendor": "ICE Data Services",
        "product": "ICE DXY public display and derived-data licence",
        "reason": "H.10 Broad Dollar 不是 ICE DXY；品牌指数数值和派生展示权需单独授权。",
        "priority": 1,
    },
    {
        "key": "fx-executable-spot",
        "page_key": "assets-fx",
        "metric_name": "可执行机构外汇现货",
        "status": PURCHASE_REQUIRED,
        "vendor": "CME EBS / Cboe FX / LSEG / Bloomberg Enterprise",
        "product": "Executable institutional spot with public-display rights",
        "reason": "H.10 是日频参考值，不是可成交 bid/ask 或实时现货报价。",
        "priority": 1,
    },
    {
        "key": "fx-offshore-cnh",
        "page_key": "assets-fx",
        "metric_name": "离岸 USD/CNH",
        "status": PURCHASE_REQUIRED,
        "vendor": "CME EBS / Cboe FX / LSEG / Bloomberg Enterprise",
        "product": "Licensed offshore CNH spot and historical storage",
        "reason": "H.10 的 USD/CNY 不是离岸 CNH，不得用 CNY 冒充 CNH。",
        "priority": 1,
    },
    {
        "key": "fx-forwards-ndf",
        "page_key": "assets-fx",
        "metric_name": "外汇远期点与 NDF",
        "status": PURCHASE_REQUIRED,
        "vendor": "CME EBS / Cboe FX / LSEG / Bloomberg Enterprise",
        "product": "FX forwards and NDF curves with derived-display rights",
        "reason": "远期与 NDF 不能从 H.10 现期参考值伪造。",
        "priority": 1,
    },
    {
        "key": "fx-cross-currency-basis",
        "page_key": "assets-fx",
        "metric_name": "Cross-currency basis / FX swap implied funding",
        "status": LICENSE_REVIEW,
        "vendor": "LSEG / Bloomberg / authorised derived-data provider",
        "product": "Cross-currency basis with public derived-display permission",
        "reason": "基差和隐含美元融资需独立数据与公开派生展示权。",
        "priority": 1,
    },
    {
        "key": "fx-order-book-dealer",
        "page_key": "assets-fx",
        "metric_name": "外汇订单簿与 dealer 微观结构",
        "status": LICENSE_REVIEW,
        "vendor": "CME EBS / Cboe FX / licensed dealer composite",
        "product": "Order-book depth or dealer composite redistribution rights",
        "reason": "深度、点差与 dealer 流量不在 H.10 合同中，需逐场所审核。",
        "priority": 2,
    },
    {
        "key": "transmission-official-evidence-v1",
        "page_key": "transmission-chain",
        "metric_name": "六层官方证据链 v1 直接输入",
        "status": LIVE,
        "source_name": (
            "Federal Reserve H.4.1, H.8, PRATES and H.10; New York Fed "
            "Markets API; Treasury FiscalData and Treasury rates"
        ),
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": (
            "父页只原子组合六个已审核子合同，并对账 H.4.1、ON RRP、SOFR、"
            "IORB、SRF 与美元互换的 exact source/dataset/run/batch；LIVE 不代表"
            "完整金融条件指数或官方压力指标。"
        ),
        "priority": 1,
    },
    {
        "key": "transmission-transparent-proxies-v1",
        "page_key": "transmission-chain",
        "metric_name": "净流动性、准备金覆盖与资金市场透明派生值",
        "status": PROXY,
        "source_name": "Atlas Macro transparent calculations over official inputs",
        "source_url": "https://www.federalreserve.gov/releases/h41/",
        "reason": (
            "净流动性、准备金覆盖、8 周变化、SOFR 利差、60 日成交量 Z-score "
            "与 H.10 五日变化均保留公式和 exact input lineage；这些数值不是"
            "Federal Reserve 官方指标，也不生成分数或交易信号。"
        ),
        "priority": 1,
    },
    {
        "key": "transmission-legacy-score-methodology",
        "page_key": "transmission-chain",
        "metric_name": "总压力分、六层分、阈值与共振方法",
        "status": NEEDS_SOURCE,
        "source_name": "Reviewed independent methodology required",
        "reason": (
            "没有可复算且经审核的方法、校准样本和版本合同；v1 不反推或发布"
            " normal/tight/stress、共振结论、行动建议或伪精确分数。"
        ),
        "priority": 1,
    },
    {
        "key": "transmission-energy-official-method",
        "page_key": "transmission-chain",
        "metric_name": "能源层 EIA exact series 与转换方法",
        "status": NEEDS_SOURCE,
        "source_name": "U.S. Energy Information Administration",
        "source_url": "https://www.eia.gov/opendata/",
        "reason": (
            "必须先确定 exact series、频率对齐、单位转换和可复算方法，不能用"
            "任意油气价格拼成能源压力层。"
        ),
        "priority": 2,
    },
    {
        "key": "transmission-energy-display-rights",
        "page_key": "transmission-chain",
        "metric_name": "能源官方数据再展示许可",
        "status": LICENSE_REVIEW,
        "source_name": "U.S. Energy Information Administration",
        "source_url": "https://www.eia.gov/about/copyrights_reuse.php",
        "reason": "exact series 与公开再展示边界完成审核前，不进入父页 LIVE 合同。",
        "priority": 2,
    },
    {
        "key": "transmission-cross-currency-basis",
        "page_key": "transmission-chain",
        "metric_name": "1M/3M/1Y cross-currency basis 与 dealer implied funding",
        "status": PURCHASE_REQUIRED,
        "vendor": "LSEG / Bloomberg / CME EBS",
        "product": "Cross-currency basis, forward points and public-derived-display rights",
        "reason": "商业 OTC/交易场所数据不能由 H.10 参考汇率或自造数值替代。",
        "priority": 1,
    },
    {
        "key": "transmission-bank-capacity-official",
        "page_key": "transmission-chain",
        "metric_name": "AOCI、HTM/AFS、资本、LCR 与放贷能力解释",
        "status": NEEDS_SOURCE,
        "source_name": "FFIEC aggregate regulatory reports",
        "source_url": "https://cdr.ffiec.gov/public/",
        "reason": (
            "需建立 FFIEC 聚合口径、修订、银行范围和可解释性合同；准备金覆盖"
            "代理不能升级成银行放贷能力或资本充足结论。"
        ),
        "priority": 1,
    },
    {
        "key": "transmission-intermediary-candidate-evidence",
        "page_key": "transmission-chain",
        "metric_name": "交易商活动与结算摩擦候选代理",
        "status": NEEDS_SOURCE,
        "source_name": "NY Fed Primary Dealer, SCOOS and OFR repo candidates",
        "source_url": "https://www.newyorkfed.org/markets/primarydealer_statistics",
        "reason": (
            "需独立 provider、suppression/null 处理、不可变 artifact、exact-set 和"
            "失败保留合同；即使接入也只能称交易商活动与结算摩擦。"
        ),
        "priority": 1,
    },
    {
        "key": "transmission-intermediary-candidate-licence",
        "page_key": "transmission-chain",
        "metric_name": "交易商候选代理再展示许可",
        "status": LICENSE_REVIEW,
        "source_name": "NY Fed, Federal Reserve and Office of Financial Research",
        "source_url": "https://www.financialresearch.gov/data/",
        "reason": "来源、派生展示和历史存储边界审核完成前不并入 v1 父合同。",
        "priority": 2,
    },
    {
        "key": "transmission-market-microstructure",
        "page_key": "transmission-chain",
        "metric_name": "dealer books、haircuts、specials、bid/ask 与 basis inventory",
        "status": PURCHASE_REQUIRED,
        "vendor": "DTCC / LSEG / Bloomberg / licensed dealer-market provider",
        "product": "Repo and Treasury microstructure with external-display rights",
        "reason": "官方聚合统计不提供交易商账簿、实时 haircut、specials 或市场冲击。",
        "priority": 1,
    },
    {
        "key": "transmission-complete-asset-response",
        "page_key": "transmission-chain",
        "metric_name": "完整信用、波动率、权益、商品与可交易 FX 资产反应",
        "status": PURCHASE_REQUIRED,
        "vendor": "ICE / Cboe / LSEG / licensed multi-asset provider",
        "product": "OAS/CDX, VIX/MOVE and cross-asset market data with display rights",
        "reason": (
            "Treasury 与 H.10 只覆盖部分官方参考序列；完整资产反应不能用静态"
            "代理、无授权指数或单一官方参考值冒充。"
        ),
        "priority": 1,
    },
    {
        "key": "liquidity-official-core",
        "page_key": "liquidity",
        "metric_name": "联储总资产、准备金、ON RRP、TGA 与净流动性",
        "status": LIVE,
        "source_name": "Federal Reserve H.4.1, NY Fed Markets API and Treasury FiscalData",
        "reason": "净流动性以 USD millions 统一口径透明计算 WALCL − ON RRP − TGA。",
        "priority": 1,
    },
    {
        "key": "liquidity-lpi-composite",
        "page_key": "liquidity",
        "metric_name": "LPI 综合总分与六层分数",
        "status": NEEDS_SOURCE,
        "source_name": "Atlas Macro reviewed methodology over complete official/licensed inputs",
        "reason": "离岸基差、中介能力、信用与资产反应层未齐备前不发布伪精确综合分。",
        "priority": 1,
    },
    {
        "key": "operations-treasury-purchases-official",
        "page_key": "operations",
        "metric_name": "国债二级市场购买结果",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York Treasury Purchase Results",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": (
            "保留 direction=P、Results、提交/接受额、结算日和期限范围；"
            "feed 不披露稳定用途或 small-value 标志。"
        ),
        "priority": 1,
    },
    {
        "key": "operations-onrrp-official",
        "page_key": "operations",
        "metric_name": "ON RRP 固定利率操作结果",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York Reverse Repo Results",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "按官方 note/SVE 证据分离正常操作与 small-value 技术测试。",
        "priority": 1,
    },
    {
        "key": "operations-srf-official",
        "page_key": "operations",
        "metric_name": "Standing Repo Facility 操作结果",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York Standing Repo Results",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "合并当日操作场次，保留抵押品、利率及 SVE 分区。",
        "priority": 1,
    },
    {
        "key": "operations-soma-official",
        "page_key": "operations",
        "metric_name": "SOMA 国内证券持仓及分项",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York SOMA Holdings",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "保留周度总额、Bills、Notes/Bonds、TIPS、FRN、MBS 等官方分项。",
        "priority": 1,
    },
    {
        "key": "operations-transparent-formulas",
        "page_key": "operations",
        "metric_name": "30D 购买、SRF 激活日与 SOMA 周变化",
        "status": LIVE,
        "source_name": "Atlas Macro transparent calculations over NY Fed operations",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "只使用完整精确批次与自然日闭区间，不填充周末、节假日或无操作日。",
        "priority": 1,
    },
    {
        "key": "operations-rmp-only-purpose",
        "page_key": "operations",
        "metric_name": "精确 RMP-only 购买接受额",
        "status": NEEDS_SOURCE,
        "source_name": "NY Fed purpose field or reviewed schedule mapping",
        "source_url": "https://www.newyorkfed.org/markets/desk-operations/treasury-securities",
        "reason": "结果行混合 RMP、本金再投资及可能的操作演练，当前无可靠逐场用途字段。",
        "priority": 1,
    },
    {
        "key": "operations-dealer-order-book",
        "page_key": "operations",
        "metric_name": "交易商报价、订单簿、相对价值与市场冲击",
        "status": PURCHASE_REQUIRED,
        "vendor": "Bloomberg / LSEG / Tradeweb / CME BrokerTec",
        "product": "Licensed dealer and Treasury-market microstructure data with public-display rights",
        "reason": "免费官方结果不包含交易商级订单簿、低延迟成交或市场冲击数据。",
        "priority": 1,
    },
    {
        "key": "fed-reserve-balances",
        "page_key": "reserves",
        "metric_name": "H.4.1 准备金余额",
        "status": LIVE,
        "source_name": "Federal Reserve H.4.1 Data Download Program",
        "source_url": "https://www.federalreserve.gov/releases/h41/",
        "reason": (
            "已直接流式规范化 Board series RESH4R_N.WW / "
            "WRBWFRBL，保留观察状态、精确批次和 ZIP SHA-256。"
        ),
        "priority": 1,
    },
    {
        "key": "fed-h8-commercial-bank-assets",
        "page_key": "reserves",
        "metric_name": "H.8 美国商业银行总资产",
        "status": LIVE,
        "source_name": "Federal Reserve H.8 Data Download Program",
        "source_url": "https://www.federalreserve.gov/releases/h8/current/default.htm",
        "reason": (
            "已直接流式规范化季调 Board series B1151NCBA，"
            "保留单位、状态、原始值、精确批次和 ZIP SHA-256。"
        ),
        "priority": 1,
    },
    {
        "key": "reserves-coverage-proxy",
        "page_key": "reserves",
        "metric_name": "准备金 / 商业银行资产覆盖近似与 8 周统计",
        "status": LIVE,
        "source_name": "Federal Reserve H.4.1 and H.8",
        "source_url": "https://www.federalreserve.gov/releases/h8/current/default.htm",
        "reason": (
            "只在 WRBWFRBL 与 B1151NCBA 的最新非未来共同周三"
            "发布，双 exact batch 可复算。分子与分母机构宇宙"
            "不同，因此只是 coverage proxy，不是 Fed 官方指标。"
        ),
        "priority": 1,
    },
    {
        "key": "reserves-like-for-like-adequacy-method",
        "page_key": "reserves",
        "metric_name": "严格同口径的准备金充裕度/稀缺阈值方法",
        "status": NEEDS_SOURCE,
        "source_name": "Reviewed Federal Reserve H.4.1/H.8 methodology and like-for-like inputs",
        "source_url": "https://www.federalreserve.gov/releases/h41/",
        "reason": (
            "当前 coverage proxy 的分子覆盖所有存款机构，分母仅覆盖"
            "美国商业银行，不得用作监管资本、LCR 或严格同口径"
            "结论；阈值与方法未经独立审核前保持未接入。"
        ),
        "priority": 1,
    },
    {
        "key": "reserves-sofr-tbill-spreads",
        "page_key": "reserves",
        "metric_name": "SOFR、13-week T-bill、SOFR−T-bill 与 SOFR−IORB 历史",
        "status": LIVE,
        "source_name": (
            "NY Fed SOFR + Federal Reserve PRATES IORB + U.S. Treasury "
            "treasury-bill-rates:13w-coupon-equivalent"
        ),
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": (
            "独立日频组件绑定三条最新 exact batches；SOFR 与 Treasury "
            "必须同一刷新周期，历史只取 60 个自然日闭区间内的精确共同日。"
        ),
        "priority": 1,
    },
    {
        "key": "reserves-intermediation-status-method",
        "page_key": "reserves",
        "metric_name": "银行中介意愿/资金顺畅状态阈值方法",
        "status": NEEDS_SOURCE,
        "source_name": "Reviewed Federal Reserve and New York Fed ample-reserves research",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/policy-normalization.htm",
        "reason": (
            "原页面的状态阈值与方法不透明，必须独立审核后才能实现；"
            "当前只发布可复算原始利差，不臆造“正常”或“顺畅”状态。"
        ),
        "priority": 1,
    },
    {
        "key": "subsurface-sofr-official-input",
        "page_key": "subsurface",
        "metric_name": "SOFR、99P 与成交量官方输入",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York Markets API",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "reference-rate:sofr exact batch 保留原始 JSON 指纹、有效日、99P 与 volumeInBillions。",
        "priority": 1,
    },
    {
        "key": "subsurface-iorb-official-input",
        "page_key": "subsurface",
        "metric_name": "IORB 官方输入",
        "status": LIVE,
        "source_name": "Federal Reserve PRATES DDP",
        "source_url": "https://www.federalreserve.gov/datadownload/Choose.aspx?rel=PRATES",
        "reason": "prates:iorb 使用最新成功且最新 attempt 的 ZIP exact batch，并保留 archive hash。",
        "priority": 1,
    },
    {
        "key": "subsurface-srf-official-input",
        "page_key": "subsurface",
        "metric_name": "Standing Repo 操作与抵押品分项",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York Standing Repo Results",
        "source_url": "https://www.newyorkfed.org/markets/repo-agreement-ops-faq.html",
        "reason": "正常操作与 small-value 技术测试按显式序列和逐场 metadata 分离。",
        "priority": 1,
    },
    {
        "key": "subsurface-swaps-official-input",
        "page_key": "subsurface",
        "metric_name": "美元央行互换操作",
        "status": LIVE,
        "source_name": "Federal Reserve Bank of New York USD Liquidity Swaps",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "保留 settlement、maturity、amount、技术测试标记与 immutable JSON artifact。",
        "priority": 1,
    },
    {
        "key": "subsurface-sofr-tail-proxy",
        "page_key": "subsurface",
        "metric_name": "SOFR 99P 尾差透明代理",
        "status": LIVE,
        "source_name": "Atlas Macro transparent calculation",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "仅计算 100×(99P−SOFR) 与 100×(99P−IORB)，不生成阈值标签。",
        "priority": 1,
    },
    {
        "key": "subsurface-volume-z60-proxy",
        "page_key": "subsurface",
        "metric_name": "SOFR 成交量 Z60 透明代理",
        "status": LIVE,
        "source_name": "Atlas Macro transparent calculation",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "每点使用同一 SOFR exact batch 的 60 个官方观察日和总体标准差。",
        "priority": 1,
    },
    {
        "key": "subsurface-srf-swap-proxies",
        "page_key": "subsurface",
        "metric_name": "SRF 30D 激活与非测试互换在途代理",
        "status": LIVE,
        "source_name": "Atlas Macro transparent calculation over NY Fed operations",
        "source_url": "https://markets.newyorkfed.org/static/docs/markets-api.html",
        "reason": "small-value 技术测试不计入激活天数或非测试在途余额。",
        "priority": 1,
    },
    {
        "key": "subsurface-opaque-composite-score",
        "page_key": "subsurface",
        "metric_name": "不透明综合资金压力分与状态阈值",
        "status": NEEDS_SOURCE,
        "source_name": "Reviewed independent methodology required",
        "source_url": "https://www.newyorkfed.org/markets/reference-rates/sofr",
        "reason": "原站综合分与正常/尾部温和阈值不可复算，不反推、不发布。",
        "priority": 1,
    },
    {
        "key": "subsurface-commercial-repo-microdata",
        "page_key": "subsurface",
        "metric_name": "交易商级 repo、haircut、specials 与完整双边/sponsored 市场",
        "status": PURCHASE_REQUIRED,
        "vendor": "DTCC / BNY / Bloomberg / LSEG",
        "product": "Licensed repo microstructure and external-display rights",
        "reason": "官方免费数据不覆盖交易商账簿、实时 haircut、specials 与完整市场分层。",
        "priority": 1,
    },
    {
        "key": "fx-vol-surface",
        "page_key": "fx-vol",
        "metric_name": "FX ATM IV、25Δ Risk Reversal 与 Butterfly",
        "status": PURCHASE_REQUIRED,
        "vendor": "LSEG / Bloomberg / CME FX-CVOL",
        "product": "FX option volatility surface and external display",
        "reason": "主要货币期权波动率面为 OTC/交易所商业数据。",
        "priority": 1,
    },
    {
        "key": "iv-rv-cross-asset",
        "page_key": "implied-vs-realized",
        "metric_name": "跨资产隐含与实现波动率风险溢价",
        "status": PURCHASE_REQUIRED,
        "vendor": "Cboe/OPRA plus licensed underlying bars",
        "product": "Options IV and underlying history with derived-display rights",
        "reason": "IV 与标的历史必须同批次并具备派生公开展示许可。",
        "priority": 1,
    },
    {
        "key": "credit-stress-inputs",
        "page_key": "credit-stress",
        "metric_name": "信用压力五分量与历史回测",
        "status": PURCHASE_REQUIRED,
        "vendor": "ICE Data Indices / licensed TRACE provider plus official SLOOS",
        "product": "Credit spread history and derived stress-score display rights",
        "reason": "无 ICE/TRACE 授权时不能发布静态 OAS 水位或压力分数。",
        "priority": 2,
    },
    {
        "key": "supply-chain-public-evidence",
        "page_key": "supply-chain",
        "metric_name": "五环节供应链事件、产能与证据链",
        "status": NEEDS_SOURCE,
        "source_name": "Company IR, SEC, exchange filings and manually reviewed evidence",
        "reason": "先建立可追溯公开披露库；完整产能与客户分配再采购专业研究源。",
        "priority": 1,
    },
    {
        "key": "foundry-capacity",
        "page_key": "supply-chain-foundry",
        "metric_name": "先进制程产能、利用率和工厂爬坡",
        "status": PURCHASE_REQUIRED,
        "vendor": "TrendForce / TechInsights / Omdia",
        "product": "Foundry capacity estimates with public-derived-display rights",
        "reason": "公司披露可补路线图和 CapEx，月度利用率和市占率需专业授权。",
        "priority": 1,
    },
    {
        "key": "advanced-packaging-capacity",
        "page_key": "supply-chain-packaging",
        "metric_name": "CoWoS/SoIC/OSAT 产能、良率与供需缺口",
        "status": PURCHASE_REQUIRED,
        "vendor": "SemiAnalysis / TrendForce / TechInsights",
        "product": "Advanced-packaging capacity model and display licence",
        "reason": "精确月产能、良率和客户分配通常不由公司完整披露。",
        "priority": 1,
    },
    {
        "key": "hbm-supply-demand",
        "page_key": "supply-chain-hbm",
        "metric_name": "HBM 位产出、认证、价格与供需覆盖",
        "status": PURCHASE_REQUIRED,
        "vendor": "TrendForce / Omdia / TechInsights / SemiAnalysis",
        "product": "HBM supply-demand and pricing with public-display rights",
        "reason": "厂商公告可补里程碑，位产出、合约价和客户分配需专业授权。",
        "priority": 1,
    },
    {
        "key": "accelerator-shipments",
        "page_key": "supply-chain-gpu",
        "metric_name": "GPU/ASIC 出货、ASP、交付周期与客户部署",
        "status": PURCHASE_REQUIRED,
        "vendor": "SemiAnalysis Accelerator Model / Omdia / TechInsights",
        "product": "Accelerator shipments and installed-base display rights",
        "reason": "产品规格可来自厂商，出货和客户级安装基数属于估算数据。",
        "priority": 1,
    },
    {
        "key": "sec-four-company-fundamentals-capex",
        "page_key": "supply-chain-demand",
        "metric_name": "Microsoft、Alphabet、Amazon、Meta 五年基础面与披露现金资本开支",
        "status": LIVE,
        "source_name": "U.S. SEC EDGAR companyfacts and submissions",
        "source_url": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "reason": "仅覆盖四家 reviewed 公司；Amazon 的 productive-assets 标签较宽，四家公司层面的现金资本开支不等同于 AI 项目拆分。",
        "priority": 1,
    },
    {
        "key": "remaining-company-fundamentals",
        "page_key": "ai-company",
        "metric_name": "其余公司基础面",
        "status": NEEDS_SOURCE,
        "source_name": "",
        "reason": "SEC 四家公司 LIVE 接入不代表 219 家合同公司均已覆盖。",
        "priority": 2,
    },
    {
        "key": "ai-only-capex-breakout",
        "page_key": "supply-chain-demand",
        "metric_name": "AI-only CapEx 拆分",
        "status": NEEDS_SOURCE,
        "source_name": "",
        "reason": "公司年报通常不提供可比的 AI-only 项目拆分；缺失数据保持缺失。",
        "priority": 2,
    },
    {
        "key": "company-capex-guidance",
        "page_key": "supply-chain-demand",
        "metric_name": "公司资本开支指引抽取",
        "status": NEEDS_SOURCE,
        "source_name": "Official investor relations disclosures",
        "reason": "需要逐条核验官方 IR 公告、口径、期间与修订状态。",
        "priority": 2,
    },
    {
        "key": "deployment-customer-estimates",
        "page_key": "supply-chain-demand",
        "metric_name": "部署量与客户级需求估计",
        "status": PURCHASE_REQUIRED,
        "vendor": "SemiAnalysis / Omdia / TechInsights",
        "product": "Deployment and customer estimates with public-derived-display rights",
        "reason": "GPU 数量、客户分配和项目级部署不是 SEC 公司层面现金资本开支事实；需要明确公开衍生展示权。",
        "priority": 3,
    },
    {
        "key": "ai-teardown-bom",
        "page_key": "ai-teardown",
        "metric_name": "AI 系统 BOM、价值量假设和版本化情景",
        "status": PURCHASE_REQUIRED,
        "vendor": "TechInsights / SemiAnalysis plus vendor BOM evidence",
        "product": "System teardown/BOM model with derived public-display rights",
        "reason": "原有百分比为演示值，已停止发布；每项成本和占比必须带来源及假设版本。",
        "priority": 1,
    },
]
