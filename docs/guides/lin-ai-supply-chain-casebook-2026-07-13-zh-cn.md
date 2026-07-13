# AI 产业链数据与十公司案例册

> - 数据截止：2026-07-13，北京时间
> - 行情观察日：2026-07-10 收盘
> - 文档性质：带日期的研究快照，不是永久结论
> - 发布状态：`local_research_only`；含许可范围为本地研究的行情快照，公开发布前必须替换或取得再分发授权

本案例册是[《从零研究 AI 产业链股票》](./lin-ai-supply-chain-investing-tutorial-zh-cn.md)的动态配套材料。主教程只保存长期可复用的方法；这里保存会随财报、价格、股本、产品认证和政策变化而失效的事实。

使用顺序：先完成主教程第 1-20 章，尤其是收入模型、CapEx、三情景和估值，再使用本案例册。案例册不产生买卖或仓位建议。

## 数据治理

- 本文件是 `2026-07-13` 这一 vintage 的唯一叙事快照；主教程不重复保存相同公司数字。
- 更新时新建新日期版本，不覆盖旧 vintage，以便复盘当时可见信息。
- 每项数字必须保留观察期、发布日期、获取日、来源、质量、许可范围和 fallback 状态。
- Futu 与 TVRemix 行情只允许本地研究；公开版本必须改用可再分发数据或删除精确报价。
- 单季年化倍数只用于发现问题，不用于公司排名；正式结论必须补 TTM、中周期和下行情景。

---

<a id="casebook-a"></a>
<a id="casebook-data-atlas"></a>

## 案例 A　日期化财报与市场份额数据底图（截至 2026-07-13）

前面的章节已经先教会你制造阶段、证据强弱、监管文件、数据质量、财务报表和九层产业链。现在再读这张数据底图，数字才不会变成无法判断真假的信息堆。

> **阅读层级：日期化参考。**第一次阅读请跳过本章，先完成第 17–21 章的收入、资本效率、情景、估值和 Deep Dive，再回来使用。任何财报、行情或份额在下一次正式披露后都会降级为 `stale`；本章不得作为“最新推荐名单”。

前面九层地图回答“应该研究什么”，本节用最新可获得的企业财报和统一口径的第三方市场估计，把地图锚定到真实数字。它是一张**有截止日期的快照**，不是永久结论。

### 阅读前提醒：方法可以复用，赛道结论必须更新

原分享对存储、HBM、设备、光互联、CPO、InP、Intel 制程、电力、液冷和 SiC 都给过阶段性观察。这些内容应分成两类：

- **可以长期保留的方法：**看良率、认证、设备订单、长约、预付款、实际交付、供给纪律和上下游验证。
- **必须按日期更新的判断：**哪个赛道“当前最强”、某家公司是否已经量产、某代产品何时放量、某个 ticker 的公司结构、价格和份额。

因此，主教程没有把“存储当前最强”之类的旧判断固化成永久结论；本案例册只保存这一日期的重新计算结果。正确动作不是引用作者当年的结论，而是用作者的验证方法重新计算当期状态。

### A.1 先读口径，不要先看数字大小

本节遵守以下规则：

1. **数据截止日**为 2026-07-13，北京时间；当天尚未正式发布的财报不使用预测值代替。
2. “最新”指截至截止日最新已公开的完整报告期，不代表所有公司处在同一个自然季度。
3. 美元、欧元、日元、新台币和韩元保留原币种，不用临时汇率制造虚假可比性。
4. 财报表中的数字优先来自 SEC、公司 IR 或交易所披露；季度数据通常未经审计。
5. 市场份额来自同一研究机构在同一期间、同一分母下的估计，统一标为 `estimated`；不能冒充公司审计数据。
6. `guidance`、ARR、backlog、RPO、订单和实际收入分别展示，不能混成“已实现收入”。
7. 同比若不是公司直接列示而是用原始披露计算，必须写“据披露计算”。
8. 表格里的公司是产业证据样本，不是推荐名单。

本节使用的来源性质：

| 标签 | 含义 | 可以支持什么 | 不能支持什么 |
| --- | --- | --- | --- |
| `official_actual` | 监管申报或公司正式财务表中的历史实际值 | 收入、利润、现金流、分部数据 | 下一季度结果、长期趋势必然延续 |
| `official_guidance` | 公司正式给出的前瞻指引 | 管理层当前计划与假设 | 已实现收入或确定订单 |
| `official_operational_kpi` | 电话会、演示或官方博客中的运营指标 | ARR、用户数、token、订单、backlog 等 | GAAP 收入、统一行业份额 |
| `derived_from_official` | 只用官方输入做的加减乘除 | 利润率、占比、单季差额 | 来源没有披露的业务拆分 |
| `external_estimate` | 研究机构的统一市场模型 | 同一口径下的相对市场位置 | 公司审计事实、跨机构无缝趋势 |
| `unavailable` | 没有可靠公开数据 | 诚实显示缺口 | 用猜测、媒体转述或流量排名补值 |

本节共享元数据：获取/复核时间为 `2026-07-13 Asia/Shanghai`；许可范围为公开监管、公司 IR、公司官方页面和研究机构公开页面，仅保留链接、必要元数据与原创摘要；本次 `fallback_state = none`，若某项未来只能保留上一完整快照，将在该项旁单独标为 `stale/fallback`；公司正式历史数字为 `fresh/official_actual`，研究机构份额为 `fresh/estimated`。

### A.2 材料与前道设备：AI 投资正在进入工艺强度

| 公司 | 最新报告期与发布日期 | 最新财务实际值 | AI 产业证据 | 研究时怎么读 | 直接来源 |
| --- | --- | --- | --- | --- | --- |
| Shin-Etsu Chemical | FY2026，截止 2026-03-31；发布 2026-04-28 | 集团营收 ¥2.574 万亿，同比 +0.5%；营业利润 ¥6352 亿，同比 -14.4%；电子材料营收 ¥1.016 万亿，同比 +9% | 公司称 AI 需求带动硅晶圆、光刻胶和光掩模基板销售增长；电子材料营业利润 ¥3445 亿，同比 +6% | 集团营收增长很慢，不代表电子材料没有结构增长；也不能把整家公司都算成 AI 业务 | [Shin-Etsu FY2026 财报](https://www.shinetsu.co.jp/wp-content/uploads/2025/07/20260428_con_E.pdf) |
| ASML | 1Q26，截止 2026-03-29；发布 2026-04-15 | 净销售额 €87.67 亿，同比约 +13.2%；毛利率 53.0%；净利润 €27.57 亿 | 装机管理收入 €24.88 亿；售出 67 台新系统和 12 台二手系统；2026 年收入指引 €360亿–€400 亿 | 系统出货、服务收入和客户扩产共同验证需求；Q1 材料未披露季度净订单，不能拿 Q4 订单填入 Q1 | [ASML 1Q26](https://www.asml.com/en/news/press-releases/2026/q1-2026-financial-results) |
| Applied Materials | FY2Q26，截止 2026-04-26；发布 2026-05-14 | 营收 $79.10 亿，同比 +11%；GAAP 毛利率 49.9%；营业利润率 31.9% | 半导体系统收入 $59.65 亿；其中 Foundry/Logic/Other 67%、DRAM 29%、Flash 4%；服务收入 $16.65 亿 | DRAM 占比提高能支持 HBM/内存工艺强度，但不能把全部 DRAM 设备收入都归为 HBM | [Applied Materials FY2Q26](https://ir.appliedmaterials.com/news-releases/news-release-details/applied-materials-announces-second-quarter-2026-results) |
| Lam Research | FY3Q26，截止 2026-03-29；发布 2026-04-22 | 营收 $58.41 亿，环比 +9%、同比约 +23.8%；GAAP 毛利率 49.8%；营业利润率 35.0% | 系统收入 $37.31 亿；客户支持及其他收入 $21.11 亿；下一季度收入指引 $66 亿 ± $4 亿 | 服务收入提供装机基础证据，系统收入更敏感于新增工艺步骤；两者不能用同一倍数外推 | [Lam 2026 年 3 月季财报](https://investor.lamresearch.com/2026-04-22-Lam-Research-Corporation-Reports-Financial-Results-for-the-Quarter-Ended-March-29%2C-2026?asPDF=) |
| KLA | FY3Q26，截止 2026-03-31；发布 2026-04-29 | 营收 $34.15 亿，同比约 +11.5%；GAAP 毛利率约 61.1%；净利润 $12.01 亿 | 半导体制程控制收入 $30.84 亿，约占总收入 90%；10-Q 将同比增长与 HBM/DRAM 和 Foundry/Logic 投资联系起来 | 高毛利和高制程控制纯度值得研究，但没有统一公开分母时，不要自行填写检测/量测份额 | [KLA FY3Q26 财报](https://ir.kla.com/news-events/press-releases/detail/514/kla-corporation-reports-fiscal-2026-third-quarter-results)、[KLA 10-Q](https://ir.kla.com/sec-filings/all-sec-filings/content/0000319201-26-000016/klac-20260331.htm) |

这组数据支持的不是“设备股一定上涨”，而是三条可继续验证的链：

```text
先进逻辑/HBM 投资
→ 更多沉积、刻蚀、清洗、量测步骤
→ 新系统收入
→ 装机基础扩大
→ 后续服务与升级收入
```

下一步必须检查客户 CapEx、订单验收、地区收入、出口限制和库存。设备收入已经增长，不等于客户未来每个季度都继续加速。

### A.3 晶圆代工与封装：先进节点收入和可交付封装要分开

| 公司 | 最新报告期与发布日期 | 最新财务实际值 | 先进制造/封装证据 | 口径限制 | 直接来源 |
| --- | --- | --- | --- | --- | --- |
| TSMC | 1Q26，截止 2026-03-31；发布 2026-04-16 | 收入 NT$1.134 万亿，同比 +35.1%；美元收入 $359 亿，同比 +40.6%；毛利率 66.2%；营业利润率 58.1% | 7nm 及以下占晶圆收入 74%：3nm 25%、5nm 36%、7nm 13%；HPC 占总收入 61%，环比 +20%；单季 CapEx $111 亿 | HPC 不等于纯 AI；晶圆收入也不等于先进封装收入。TSMC 2Q26 完整财报定于 2026-07-16，因此截止日本节仍使用 1Q26 | [TSMC 1Q26 结果页](https://investor.tsmc.com/english/quarterly-results/2026/q1)、[管理报告](https://investor.tsmc.com/english/encrypt/files/encrypt_file/qr/phase4_reports/2026-04/9f060092ba29ff3630cfdaefd67774026195e135/1Q26ManagementReport.pdf) |
| ASE Technology | 1Q26，截止 2026-03-31；发布 2026-04-29 | 合并营收 NT$1736.62 亿，同比约 +17.2%；毛利率 20.1%；营业利润率 10.1% | ATM 封装测试收入 NT$1124.34 亿，同比约 +29.7%；Computing 占 ATM 收入 27%，上年同期 22%；Bumping、Flip Chip、WLP、SiP 合计占 ATM 收入 49% | ASE 合并口径还包含 EMS；Computing 也不全是 AI。不要把集团营收直接与纯 OSAT 公司比较 | [ASE 1Q26 SEC 6-K](https://www.sec.gov/Archives/edgar/data/1122411/000095010326006339/dp245865_6k.htm) |
| Amkor | 1Q26，截止 2026-03-31；发布 2026-04-27 | 营收 $16.85 亿，同比约 +27.5%；毛利率 14.2%；营业利润 $1.00 亿 | Advanced Products 收入 $13.72 亿，同比约 +28.9%，占总收入约 81.4%；2026 年 CapEx 指引 $25亿–$30 亿 | Advanced Products 包含 Flip Chip、Memory、WLP 与测试，不等于 AI 封装；Computing 还包括 PC、存储和普通基础设施 | [Amkor 1Q26](https://ir.amkor.com/news-releases/news-release-details/amkor-technology-reports-financial-results-first-quarter-2026) |

这里最重要的交叉验证是：TSMC 的先进节点与 HPC 收入、OSAT 的先进产品组合、设备公司的 HBM/先进逻辑需求同时改善。但这仍不能直接算出 CoWoS 或整个先进封装市场的统一份额，因为 foundry 自营封装与 OSAT 的收入边界不同。

### A.4 计算芯片：收入高速增长不等于份额数字可随意引用

| 公司 | 最新报告期与发布日期 | 总收入与盈利 | AI 相关实际值 | 关键解读 | 直接来源 |
| --- | --- | --- | --- | --- | --- |
| NVIDIA | FY2027 Q1，截止 2026-04-26；发布 2026-05-20 | 收入 $816.15 亿，同比 +85%；GAAP 毛利率 74.9%；GAAP 营业利润 $535.36 亿 | 数据中心收入 $752 亿，同比 +92%；旧口径下计算收入 $604 亿，同比 +77%，网络收入 $148 亿，同比 +199% | 计算与网络都在增长，说明系统瓶颈不只在 GPU；但 NVIDIA 没有在财报中披露统一“AI 加速器市场份额” | [NVIDIA FY2027 Q1](https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-Financial-Results-for-First-Quarter-Fiscal-2027/default.aspx) |
| AMD | 1Q26，截止 2026-03-28；发布 2026-05-05 | 收入 $102.53 亿，同比 +38%；GAAP 毛利率 53%；营业利润 $14.76 亿 | 数据中心收入 $57.75 亿，同比 +57%，由 EPYC CPU 与 Instinct GPU 需求推动；数据中心营业利润 $16 亿 | 数据中心分部同时包含 CPU、GPU、DPU、NIC、FPGA 等，不能把 $57.75 亿全部当成 AI GPU 收入 | [AMD 1Q26](https://ir.amd.com/news-events/press-releases/detail/1284/amd-reports-first-quarter-2026-financial-results)、[AMD 10-Q](https://www.sec.gov/Archives/edgar/data/2488/000000248826000076/amd-20260328.htm) |
| Broadcom | FY2026 Q2，截止 2026-05-03；发布 2026-06-03 | 收入 $221.87 亿，同比 +48%；GAAP 净利润 $93.10 亿；经营现金流 $104.93 亿 | 半导体收入 $150.09 亿，同比 +79%；公司自报 AI 半导体收入 $108 亿，同比 +143%，来自定制加速器与 AI 网络 | AI 半导体收入是公司定义的运营拆分，不是独立审计报告分部；下一季度 AI 指引 $160 亿仍是 guidance | [Broadcom FY2026 Q2](https://investors.broadcom.com/news-releases/news-release-details/broadcom-inc-announces-second-quarter-fiscal-year-2026-financial) |

正确结论是：三家公司的数据中心、AI 半导体或网络收入都在增长。错误结论是：把三家不同分部相加，算出一个“全球 AI 芯片市场规模”，再反推份额。分部边界、内部芯片、定制 ASIC、网络芯片和 GPU 都不同。

### A.5 存储：最能看见价格弹性，也最需要防周期外推

| 公司 | 最新报告期与发布日期 | 最新财务实际值 | AI/HBM 证据 | 关键限制 | 直接来源 |
| --- | --- | --- | --- | --- | --- |
| Samsung Electronics | 1Q26，截止 2026-03-31；发布 2026-04-30 | 合并营收 KRW133.9 万亿，环比 +43%；营业利润 KRW57.2 万亿；DS 部门收入 KRW81.7 万亿、营业利润 KRW53.7 万亿 | 公司称内存收入和利润创新高，并开始销售用于 Vera Rubin 平台的 HBM4 与 SOCAMM2 | DS 还包括 Foundry 和 System LSI；不能把 DS 全部当作内存，更不能把集团利润全部归于 HBM | [Samsung 1Q26](https://news.samsung.com/global/samsung-electronics-announces-first-quarter-2026-results) |
| SK hynix | 1Q26；发布 2026-04-23 | 收入 KRW52.5763 万亿；营业利润 KRW37.6103 万亿，营业利润率 72%；净利润 KRW40.3459 万亿 | 公司将增长归因于 HBM、高容量服务器 DRAM 和 eSSD 等高附加值产品；季度数字仍待独立审计完成 | 极高利润率是供需和价格共同作用的周期快照，不能当作永续利润率 | [SK hynix 1Q26](https://news.skhynix.com/q1-2026-business-results/) |
| Micron | FY2026 Q3，截止 2026-05-28；发布 2026-06-24 | 收入 $414.56 亿，同比 +346%；毛利额 $350.56 亿，毛利率约 85%；营业利润 $333.18 亿 | DRAM 销售环比 +67%，主要由低 60% 区间 ASP 上升推动；NAND 销售环比 +99%，主要由中 80% 区间 ASP 上升推动；HBM4 已向领先客户高量出货 | 本季度弹性主要来自 ASP 而非 bit shipment；完整研究见第 23 章，不能用峰值利润直接套低 P/E | [Micron FY2026 Q3 10-Q](https://www.sec.gov/Archives/edgar/data/723125/000072312526000015/mu-20260528.htm)、[财报新闻稿](https://investors.micron.com/node/50671/pdf) |

截至截止日，[Samsung 已于 2026-07-07 发布 2Q26 业绩预告](https://news.samsung.com/global/samsung-electronics-announces-earnings-guidance-for-second-quarter-2026)：预计合并收入约 KRW171 万亿、营业利润约 KRW89.4 万亿。它没有分部明细，仍可能随完整财报更新，因此本表把它归为 `official_guidance`，不拿它替代 1Q26 完整财报，也不据此反推 HBM 收入。

三家的当期收入、利润、ASP 和产品组合信号，与公司关于当前供给偏紧、价格走强和高价值产品组合改善的评论相互印证；但全球供需仍要用独立价格、库存、产能和客户采购数据继续验证。存储是典型经营杠杆行业：

```text
ASP 上升 + 利用率提高 + 高价值产品占比上升
→ 毛利率急升

ASP 回落 + 新产能折旧 + 库存增加
→ 毛利率也可能快速反转
```

所以“最新财报最强”不能自动推出“未来回报最好”。必须把 2026 年的现货/合约价格、长期协议、客户预付款和 2027–2028 年新增供给放进三情景模型。

### A.6 网络、服务器与供电散热：把芯片订单追到系统交付

| 公司 | 最新报告期与发布日期 | 最新实际值 | AI 基础设施证据 | 风险提示 | 直接来源 |
| --- | --- | --- | --- | --- | --- |
| Arista Networks | 1Q26，截止 2026-03-31；发布 2026-05-05 | 收入 $27.09 亿，同比 +35.1%；GAAP 毛利率 61.9%；GAAP 营业利润率 42.7%；经营现金流 $16.9 亿 | 产品收入 $23.11 亿；公司继续把业务定位于 AI、云和数据中心网络 | 没有分拆 AI 网络收入；不能把全公司收入都归于 AI 集群 | [Arista 1Q26](https://investors.arista.com/Communications/Press-Releases-and-Events/Press-Release-Detail/2026/Arista-Networks-Inc--Reports-First-Quarter-2026-Financial-Results/default.aspx) |
| Coherent | FY2026 Q3，截止 2026-03-31；发布 2026-05-06 | 收入 $18.06 亿，同比 +21%；剔除已出售业务后同比 +27%；GAAP 毛利率 37.7%；净利润 $1.914 亿 | 数据中心与通信收入 $13.62 亿，同比 +41%，约占总收入 75.4%（据披露计算）；公司计划 2026 年底前把内部 InP 产出翻倍，2027 年再翻倍 | 数据中心与通信还包含非 AI 业务；多年产能协议和扩产计划不是当期光模块收入 | [Coherent FY2026 Q3](https://www.coherent.com/news/press-releases/third-quarter-fiscal-year-2026-results)、[投资者演示](https://www.coherent.com/content/dam/coherent/site/en/documents/investors/investor-presentations/2026/may-6/investor-presentation-20260506.pdf)、[10-Q](https://www.sec.gov/Archives/edgar/data/820318/000082031826000013/iivi-20260331.htm) |
| Lumentum | FY2026 Q3，截止 2026-03-28；发布 2026-05-05 | 收入 $8.084 亿，同比 +90.1%；GAAP 毛利率 44.2%；GAAP 营业利润率 21.6% | Components 收入 $5.333 亿，同比 +77%；Systems 收入 $2.751 亿，同比 +121%；公司称 200G EML 收入环比翻倍以上、云光模块出货环比 +40% | 这些是公司自报产品/运营指标；1.6T “准备放量”仍是前瞻状态，不是已经确认的规模收入 | [Lumentum FY2026 Q3](https://investor.lumentum.com/financial-news-releases/news-details/2026/Lumentum-Announces-Third-Quarter-of-Fiscal-Year-2026-Financial-Results/default.aspx)、[财报演示](https://s21.q4cdn.com/377324469/files/doc_financials/2026/q3/Q3-FY26-Earnings-Presentation_final.pdf)、[10-Q](https://www.sec.gov/Archives/edgar/data/1633978/000162828026030777/lite-20260328.htm) |
| Dell Technologies | FY2027 Q1，截止 2026-05-01；发布 2026-05-28 | 总收入 $438.42 亿，同比 +88%；ISG 收入 $290.09 亿，同比 +181%；ISG 营业利润率 10.5% | AI 优化服务器收入 $161.32 亿，同比 +757%；公司自报 AI 订单 $244 亿，期末 AI backlog $513 亿 | 订单与 backlog 不是收入；服务器高收入还要检查低毛利、内存约束和营运资本 | [Dell FY2027 Q1](https://investors.delltechnologies.com/news-releases/news-release-details/dell-technologies-delivers-first-quarter-fiscal-2027-financial)、[电话会](https://investors.delltechnologies.com/static-files/b63ffff9-b729-403b-a231-c6af05667759) |
| Supermicro | FY2026 Q3，截止 2026-03-31；发布 2026-05-05 | 净销售额 $102 亿，去年同期 $46 亿；GAAP 毛利率 9.9%；净利润 $4.83 亿 | 公司把增长与 AI、云和数据中心基础设施联系起来 | 单季经营现金流流出 $66 亿；高增长、低毛利和现金占用必须一起看 | [Supermicro FY2026 Q3](https://ir.supermicro.com/news/news-details/2026/Supermicro-Announces-Third-Quarter-Fiscal-Year-2026-Financial-Results/default.aspx) |
| Vertiv | 1Q26，截止 2026-03-31；发布 2026-04-22 | 净销售额 $26.50 亿，同比 +30%；营业利润 $4.40 亿；调整后营业利润率 20.8%；经营现金流 $7.67 亿 | 美洲有机销售增长 44%，公司归因于强劲数据中心需求；全年收入指引 $135亿–$140 亿 | 电力和散热需求强，但公司没有在本表来源中披露统一 AI 收入；调整后指标要与 GAAP 分开 | [Vertiv 1Q26](https://investors.vertiv.com/news/news-details/2026/Vertiv-Reports-Strong-First-Quarter-with-Diluted-EPS-Growth-of-136-Adjusted-Diluted-EPS-Growth-of-83-Raises-Full-Year-Guidance/default.aspx) |

这些公司展示了同一需求在不同环节的经济结果：Arista 的网络利润率、Coherent/Lumentum 的光学放量、Dell 的 AI 系统订单、Supermicro 的现金占用和 Vertiv 的供电散热增长非常不同。研究时不能只比较收入增速；要同时比较毛利、现金流、取消条款、库存和交付能力。

### A.7 云与应用：最终付钱端正在增长，也在吞噬巨额资本

金额均为美元。CapEx 口径差异很大，不能把表中五个数字直接相加。

| 公司 | 最新报告期与发布日期 | 总收入 | 云/AI 相关实际值 | CapEx 实际值与指引 | 直接来源 |
| --- | --- | ---: | --- | --- | --- |
| Microsoft | FY2026 Q3，截止 2026-03-31；发布 2026-04-29 | $829 亿，同比 +18% | Microsoft Cloud $545 亿，同比 +29%；Intelligent Cloud $347 亿，同比 +30%；Azure 及其他云服务同比 +40%，未披露绝对收入；公司自报 AI 业务年化收入运行速率超过 $370 亿（annual revenue run rate，不等于 annual recurring revenue） | 季度 CapEx $319 亿；现金购买 PP&E $309 亿；新增融资租赁 $47 亿，三者不能相加；CY2026 CapEx 指引约 $1900 亿 | [财报新闻稿](https://www.microsoft.com/en-us/investor/earnings/FY-2026-Q3/press-release-webcast)、[电话会](https://www.microsoft.com/en-us/investor/events/fy-2026/earnings-fy-2026-q3) |
| Alphabet | 1Q26，截止 2026-03-31；发布 2026-04-29 | $1098.96 亿，同比 +22% | Google Cloud $200.28 亿，同比 +63%；Cloud 营业利润 $65.98 亿；据官方输入计算营业利润率约 32.9% | 现金购买 PP&E $356.74 亿；CY2026 CapEx 指引 $1800亿–$1900 亿，绝大部分用于技术基础设施 | [Alphabet 1Q26 SEC Exhibit 99.1](https://www.sec.gov/Archives/edgar/data/1652044/000165204426000043/googexhibit991q12026.htm)、[官方投资者演示](https://blog.google/alphabet/investor-presentation-june-2026/) |
| Amazon | 1Q26，截止 2026-03-31；发布 2026-04-29 | $1815 亿，同比 +17% | AWS 收入 $375.87 亿，同比 +28%；AWS 营业利润约 $142 亿；公司自报 AWS AI 年化收入运行速率超过 $150 亿（AI revenue run rate，不等于 recurring revenue） | 公司口径 cash CapEx $432 亿，主要投向技术基础设施但也包含履约网络；CY2026 约 $2000 亿 | [Amazon 1Q26](https://ir.aboutamazon.com/news-release/news-release-details/2026/Amazon-com-Announces-First-Quarter-Results/)、[Amazon 10-Q](https://www.sec.gov/Archives/edgar/data/1018724/000101872426000014/amzn-20260331.htm)、[电话会](https://ir.aboutamazon.com/events/event-details/2026/Q1-2026-Amazoncom-Inc-Earnings-Conference-Call-/default.aspx)、[AWS AI KPI](https://www.aboutamazon.com/news/company-news/amazon-ceo-andy-jassy-aws-ai-q1-2026-earnings) |
| Meta | 1Q26，截止 2026-03-31；发布 2026-04-29 | $563.11 亿，同比 +33% | 没有对外云报告分部；广告收入 $550.24 亿；Family DAP 35.6 亿 | CapEx $198.4 亿，包含现金购买 PP&E $189.97 亿和融资租赁本金 $8.43 亿；CY2026 指引 $1250亿–$1450 亿 | [Meta 1Q26 财报](https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/Meta-03-31-2026-Exhibit-99-1_final.pdf) |
| Oracle | FY2026 Q4，截止 2026-05-31；发布 2026-06-10 | $191.84 亿，同比 +21% | 总云收入 $99.13 亿，同比 +47%；其中 IaaS $58 亿，同比 +93%，SaaS $41 亿，同比 +10%；RPO $6380 亿 | FY2026 GAAP 现金流口径 CapEx $556.63 亿；公司另披露 TTM 净现金 CapEx $477.26 亿，两者定义不同 | [Oracle FY2026 Q4](https://investor.oracle.com/investor-news/news-details/2026/Oracle-Announces-Record-Q4-and-FY-2026-Results-Driven-by-Cloud-Infrastructure--Cloud-Applications/) |

云收入不可直接横比：Microsoft Cloud 包含 Microsoft 365 等产品，Google Cloud 包含 GCP 和 Workspace，AWS 是 Amazon 报告分部，Oracle 云收入同时含 IaaS 与 SaaS，Meta 则没有对外云分部。**绝对不能把这几家财报中的“云收入”加总后自行计算市场份额。**

同样，这里公司使用的 `annual revenue run rate` 是当前运行速度的年化，不等于 `annual recurring revenue`，也不是过去十二个月 GAAP 收入；backlog 和 RPO 是尚待履约的合同义务，不是当期收入或自由现金流。

### A.8 六组能够公开核验的市场份额

市场份额必须写成：

```text
份额数字 + 观察期 + 地理范围 + 产品范围 + 分母 + 估计机构 + 发布时间
```

#### A. EUV 光刻设备的供给结构

[ASML 的 EUV 产品页](https://www.asml.com/en/products/euv-lithography-systems)将公司描述为目前唯一能够生产 EUV 光刻系统的供应商。因此可以写：

> 截至 2026-07-13，ASML 是当前可商用 EUV 光刻系统的唯一供应商。

这可理解为当前可商用 EUV 供给由 ASML 单一提供，但**不能写成“ASML 占全部光刻设备市场 100%”**。DUV、i-line、翻新设备和不同收入分母都不在这句话里。

#### B. 全球晶圆代工收入份额，1Q26

[TrendForce 于 2026-06-12 发布的 1Q26 估计](https://www.trendforce.com/presscenter/news/20260612-13095.html)以全球前十大晶圆代工收入约 $479.5 亿为背景：

| 厂商 | 1Q26 估计收入 | 估计份额 |
| --- | ---: | ---: |
| TSMC | 约 $358.6 亿 | 72.0% |
| Samsung Foundry | 约 $32 亿 | 6.5% |
| SMIC | $25.1 亿 | 5.1% |
| UMC | $19.3 亿 | 3.9% |
| GlobalFoundries | 略高于 $16.3 亿 | 3.3% |

数据性质：`external_estimate`。$479.5 亿是前十大厂商合计收入，**不是**上述份额的分母；份额分母是 TrendForce 模型定义的全球晶圆代工收入总额。它不是晶圆片数、先进节点产能、AI 芯片出货或先进封装。TrendForce 对 Samsung 排除 System LSI，对部分厂商也有业务剔除规则。

#### C. 全球 DRAM 与 HBM 收入份额，1Q26

[Counterpoint Research 于 2026-06-08 发布的季度追踪](https://counterpointresearch.com/en/insights/global-dram-and-hbm-market-share)提供同一页面下的收入份额估计。

全球 DRAM 收入份额：

| 厂商 | 1Q26 估计份额 |
| --- | ---: |
| Samsung | 38% |
| SK hynix | 29% |
| Micron | 22% |
| CXMT | 8% |
| Nanya | 2% |
| Others | 1% |

全球 HBM 收入份额：

| 厂商 | 1Q26 估计份额 |
| --- | ---: |
| SK hynix | 58% |
| Samsung | 21% |
| Micron | 21% |

数据性质：`external_estimate`；由于四舍五入，总和可能略有偏差。HBM 的收入份额不是出货颗数、bit 份额、客户认证份额或某一代 HBM4 的份额。不要把两个表的数字混用。

#### D. 全球 NAND Flash 收入份额，1Q26

[TrendForce 于 2026-05-25 发布的 1Q26 估计](https://www.trendforce.com/presscenter/news/20260525-13058.html)显示前五大供应商合计收入超过 $389 亿：

| 厂商 | 1Q26 估计收入 | 估计份额 |
| --- | ---: | ---: |
| Samsung | $135.1 亿 | 31.6% |
| SK hynix Group（含 Solidigm） | 约 $75.3 亿 | 17.6% |
| Kioxia | $59.6 亿 | 13.9% |
| Micron | $59.5 亿 | 13.9% |
| Sandisk | $59.5 亿 | 13.9% |

数据性质：`external_estimate`。$389 亿以上是前五大厂商合计收入，**不是**份额分母；份额分母是 TrendForce 模型定义的全球 NAND 供应商收入。这里不是企业级 SSD 品牌份额；SK hynix Group 包含 Solidigm，也不能与只看母公司品牌的表直接比较。

#### E. 全球云基础设施服务收入份额，1Q26

[Synergy Research Group 于 2026-04-29 发布的 Q1 估计](https://www.srgresearch.com/articles/cloud-market-annual-revenue-run-rate-topped-half-a-trillion-dollars-in-q1-as-growth-surge-continues)把全球云基础设施服务定义为 IaaS、PaaS 和托管私有云服务：

| 指标 | 1Q26 估计值 |
| --- | ---: |
| 全球季度收入 | $1286 亿 |
| 同比增速 | 35% |
| AWS | 28% |
| Microsoft | 21% |
| Google | 14% |
| 前三家合计 | 63%（自行计算） |
| 其他厂商合计 | 37%（自行计算） |

数据性质：`external_estimate`。Synergy 另称，在更窄的公共 IaaS/PaaS 范围内，前三家合计占 67%。这两个百分比的分母不同，不能互相替换。

#### F. 数据中心交换机与服务器收入份额，1Q26

[IDC 于 2026-06-17 发布的 1Q26 以太网交换机追踪](https://www.idc.com/resource-center/blog/nvidia-becomes-1-in-datacenter-ethernet-switching-as-1q26-market-surges-39-8-to-15-4-billion/)估计，全球数据中心以太网交换机市场收入约 $100 亿，同比 +61%；其中 800G 占数据中心交换机收入 35.8%。

| 厂商 | 1Q26 数据中心以太网交换机收入份额 |
| --- | ---: |
| NVIDIA | 21.5% |
| Arista | 20.7% |

这不是 AI GPU、交换 ASIC、端口数或全部企业网络份额。NVIDIA 的网络设备收入份额也不能与其数据中心计算收入相加。

[IDC 于 2026-06-15 发布的 1Q26 全球服务器追踪](https://www.idc.com/resource-center/press-releases/1q26-server-tracker/)估计，全球服务器厂商收入为 $1226 亿，同比 +30.4%；GPU 加速服务器收入 $689 亿，占 56.2%。

| 厂商 | 1Q26 全球服务器厂商收入份额 |
| --- | ---: |
| Dell | 16.5% |
| Supermicro | 7.6% |

数据性质均为 `external_estimate`。IDC 自然季度和公司的财政季度边界不同，所以 IDC 估计收入不能拿来逐美元勾稽 Dell 或 Supermicro 财报。

### A.9 哪些热门“份额”本节故意不填

| 热门说法 | 为什么不填精确数字 | 合格替代写法 |
| --- | --- | --- |
| “NVIDIA 占 AI GPU/加速器市场 X%” | 加速器、离散 GPU、云实例、出货量、收入和已安装算力分母不同；公开财报没有统一全球分母 | 列 NVIDIA 数据中心计算收入，再与 AMD、Broadcom 和云自研芯片做定性/合同交叉验证 |
| “某公司占先进封装 X%” | TSMC 自营封装、ASE/Amkor OSAT、不同封装技术和 foundry 绑定收入边界不同 | 分开记录 CoWoS/2.5D/3D 的产能、认证、公司封装收入和客户交付 |
| “某公司占 800G/1.6T 光模块 X%” | 端口、模块、收入、地区和客户范围差异大，公开数据库常只给付费摘要 | 使用客户认证、量产收入、客户集中和相同速率端口出货验证 |
| “Shin-Etsu/SUMCO 占硅片 X%” | 公司公开页只给市场领导描述，当前统一分母数值多来自付费或二手材料 | 写“公司称全球市场领导者；精确当前份额未公开” |
| “Vertiv 占 AI 数据中心供电/液冷 X%” | 产品跨 UPS、热管理、服务、地区和项目，缺少统一 AI 收入分母 | 使用订单、区域有机增长、利润率和现金流验证需求捕获 |
| “Oracle 云份额约 X%” | Synergy 免费页面没有披露 Oracle 精确份额 | 写“Synergy 将其列入高增长第二梯队，免费公开页未披露精确份额” |

缺一个精确数字不会让研究失败；用错误分母制造精确数字才会。

### A.10 把整条链连起来：哪些是事实，哪些仍是推断

先把数据放回“瓶颈—寡头—利润弹性—扩产纪律”四条铁律：

| 铁律 | 当前数字提供的支持 | 仍然缺什么 | 小白最容易犯的错 |
| --- | --- | --- | --- |
| 瓶颈 | 云 CapEx、数据中心交换机/服务器收入、HBM 与存储 ASP、TSMC 先进节点、设备系统/服务收入同步增长 | 交期、取消条款、客户库存、合格产能和利用率 | 看到收入增长就假定供给永远短缺 |
| 寡头 | 1Q26 foundry 收入中 TSMC 估计占 72%；HBM 收入估计由三家供应商构成；云基础设施前三家估计占 63% | 不同产品代际的认证份额、客户自研、第二供应商导入和地区替代 | 把“玩家少”直接等同于“永远有定价权” |
| 利润弹性 | Micron、SK hynix、Samsung 的价格与利润信号；TSMC、设备与网络公司的较高利润率 | 正常化 ASP、单位成本、良率、折旧和产品组合 | 把峰值毛利率当长期毛利率，把低峰值 P/E 当便宜 |
| 扩产纪律 | 云厂商、TSMC 与 Amkor 披露了高额 CapEx 实际值或指引 | 首次产出、认证、可售产能、良率爬坡及竞争者总供给 | 把 CapEx 公告当天当作新增供给已经上线 |

这张表只能确定“接下来要验证什么”，不能单独给出买入结论。四条铁律的证据越强，越要回到估值检查市场已经预期了多少。

截至数据截止日，可以直接观察到：

- 云端：AWS、Azure、Google Cloud 和 OCI 继续增长，多家公司显著提高 CapEx。
- 计算：NVIDIA 数据中心收入、AMD 数据中心分部和 Broadcom AI 半导体收入增长。
- 存储：Samsung、SK hynix 和 Micron 的收入、利润与 ASP 信号大幅走强。
- 制造：TSMC 先进节点占比和 HPC 收入较高，Foundry 份额集中。
- 设备：先进逻辑、DRAM/HBM 和制程控制相关收入上升。
- 系统：Dell AI 服务器收入和 backlog、Vertiv 数据中心需求、Arista 网络收入增长。

这些事实支持下列**推断**，但不能把箭头当作已经证明的事实：

```text
云收入和 AI 应用使用增长
→ 云厂商愿意继续部署基础设施
→ 加速器、网络、HBM、服务器需求增加
→ 先进晶圆、封装、设备和材料工艺强度增加
→ 部分供应商获得价格、组合和经营杠杆
```

需要继续反证的地方：

- CapEx 中有多少是 AI，有多少是普通云、替换、土地、电力和履约网络。
- AI 收入增长能否在折旧增加后保持合理回报。
- 当前存储价格和利润率能维持多久。
- 加速器客户是否加快自研或增加第二供应商。
- 服务器高增长是否被低毛利和营运资本吞噬。
- 新晶圆、封装、HBM 和数据中心供给何时真正通过认证并开始交付。
- 估值是否已经计入比这些实际数字更高的增长。

### A.11 每个季度怎样更新这张表

不要在旧表上直接覆盖。每次更新执行：

1. 锁定 `data_cutoff`，先列截止日前已经发布的财报。
2. 从 SEC/IR 录入总收入、分部收入、毛利、现金流、CapEx 和订单。
3. 把 actual、guidance、运营 KPI 和外部估计分成不同字段。
4. 对所有市场份额记录期间、地区、产品、分母和估计机构。
5. 重新计算同比、分部占比和利润率，并保存公式。
6. 与上一 vintage 比较，记录 revision，不静默改写旧判断。
7. 对未发布公司保留上一完整快照并标 `stale`，不得填分析师预测。
8. 更新“支持证据、反证、下一次验证日期”，再决定 thesis 是否变化。

一条可以直接复制的记录：

```text
metric_name: TSMC foundry revenue share
value: 72.0
unit: percent
observation_period: 2026 Q1
publication_date: 2026-06-12
fetched_at: 2026-07-13 Asia/Shanghai
source: TrendForce foundry revenue ranking
source_type: industry_research_estimate
data_status: external_estimate
denominator: global foundry revenue under TrendForce definitions
quality_state: estimated/fresh
fallback_state: none
notes: revenue share, not wafer capacity or advanced-packaging share
```

### A.12 三十分钟练习：用数据写一条可证伪判断

从本节任选一个环节，不选股票，完成下面六句：

1. 当前需求证据是：`[实际收入/出货/利用率]`，观察期为 `[日期]`，来源为 `[链接]`。
2. 当前最紧约束可能是：`[具体产品、认证、工艺、供电或网络]`。
3. 支持寡头/定价权的数据是：`[同一口径市场份额或替代难度]`。
4. 价格或产品组合进入利润的证据是：`[毛利率、营业利润、ASP/bit 拆分]`。
5. 未来供给可能在 `[日期/阶段]` 增加，但它还要经过 `[设备安装—良率—认证—量产]`。
6. 如果下季度出现 `[具体反证]`，我就降低或推翻判断。

示例只写到研究结论，不写股票动作：

> 1Q26 HBM 收入份额估计显示供应商高度集中，三家内存厂近期利润也明显改善，支持“当前合格供给紧、价格弹性强”。但多家公司正在扩产，且不同客户/代际认证状态不同；若后续出现 ASP 下行、库存上升和新产能提前通过认证，就要下调对瓶颈持续时间的判断。这个结论说明该环节值得继续研究，不代表当前股价具有吸引力。

---

### A.13 快照、预测和教学模型为什么不是历史事件

| 文中内容 | 正确分类 | 为什么不能写成已发生历史 |
| --- | --- | --- |
| 1Q26 Foundry、HBM、NAND、云和交换机份额 | 截止日横截面估计 | 依赖研究机构分母，不代表永久市场结构。 |
| 2026 hyperscaler CapEx 与全年指引 | 当前投入浪潮与前瞻目标 | 口径不同，且指引可能改变；尚不足以构成完整多年周期。 |
| 1.6T、CPO、未来 HBM4 客户、Boise 2027 首片晶圆 | 产品或项目路线图 | `计划、送样、认证、准备放量` 都不是已完成交付。 |
| LBNL 延至 2030 年的用电数据 | 情景预测 | 是模型范围，不是未来已发生事实。 |
| 台湾供应中断 | 压力测试情景 | 本文没有叙述某次具体冲突或灾害事件。 |
| 库存循环图、SOXL 两日算式、Bear/Base/Bull | 教学模型 | 用于理解机制，不是历史统计序列。 |

研究历史事件的正确目的，不是收集故事，而是找到**口径断点、周期来源和今天仍然有效的因果机制**。

---

<a id="casebook-b"></a>

## 案例 B　十家公司日期化横向实战：用同一问题清单，不强行统一倍数

本章以 **Sandisk（SNDK）、Micron（MU）、Samsung Electronics（005930）、SK hynix（000660）、Lumentum（LITE）、Coherent（COHR）、Applied Optoelectronics（AAOI）、AXT（AXTI）、Sivers Semiconductors（SIVE）和 Soitec（SOI）** 为贯穿案例。目的不是排出买入顺序，而是说明：同一句“受益于 AI”，在 NAND、HBM、光模块、激光器、InP 衬底和 Photonics-SOI 上代表完全不同的产品、证据、财务与风险。

> **阅读层级：日期化案例。**本章使用统一的问题清单，但报告期、资本结构和收入分母并不完全一致。所有倍数只能在明确相同口径后比较；单季年化、LTM/TTM、完整财年和正常化中周期数值必须分列，不能混排高低。

### B.1 数据边界：“最小完整数据包”在本案例册里指什么

“所有数据”不能理解成把行情终端的每个字段全部抄进来。合格的公司数据包必须覆盖七类能够改变判断的信息：

| 数据组 | 本章必须回答的问题 |
|---|---|
| 证券身份 | 法律主体、主上市地、ticker、币种、普通股/优先股/ADS 是否混在一起 |
| 产业位置 | 公司卖衬底、激光器、模块、内存还是企业 SSD；收入是否跨多个层级 |
| 最新实际财务 | 收入、同比、毛利、营业利润、净利润、CFO、CapEx/FCF、现金与债务 |
| AI 暴露 | 能直接报告的 AI/数据中心收入是多少；不能拆出的部分必须标 `unknown` |
| 产品与交付 | 产品处于开发、送样、认证、量产、交付、收入还是现金阶段 |
| 股本与估值 | 点时股数、期后增发/转股/权证、基本市值、简化 EV 和收入倍数 |
| 研究动作 | 四条铁律、六向验证、下一催化、证伪条件和组合共同风险 |

本章统一使用以下数据合同：

- **财务观察期：**每家公司截至 2026-07-13 最新已公开的完整财报期；指引不冒充实际值。
- **行情观察日：**2026-07-10 收盘；美股由本机 Futu OpenD 10.5.6508 只读获取，Samsung、SK hynix、SIVE 由 TVRemix 跨市场缓存获取，SOI 使用公开延迟行情并明确标记。
- **行情获取日：**2026-07-13；市场关闭期间不把最后成交价写成实时盘中价。
- **市值原则：**优先使用点时发行在外股数；若财报后发生增发、债转股或注销，单独做 pro forma 股本桥。
- **估值原则：**优先展示 LTM/TTM；单季年化只标为当前 `run-rate`，完整财年和正常化中周期另列。若本章暂时只有不同分母，就只演示计算，禁止跨组排序。
- **市场份额：**只使用同期间、同产品、同分母的第三方估计；没有可靠分母就写 `unknown`。
- **交易边界：**本章不产生买入、卖出或仓位建议。

#### B.1.1 同一观察日的证券快照

| 公司 | 主证券 | 2026-07-10 收盘 | 行情源 | 基本市值或股权价值口径 |
|---|---|---:|---|---:|
| Sandisk | Nasdaq: `SNDK` | US$1,915.92 | Futu | 约 US$283.7B |
| Micron | Nasdaq: `MU` | US$979.30 | Futu | 约 US$1.106T |
| Samsung Electronics | KRX: `005930` 普通股；另有 `005935` 优先股 | KRW 285,000（普通股） | TVRemix + 公司股本桥 | 普通股与优先股合计约 KRW 1,798.7T |
| SK hynix | KRX: `000660`；另有 Nasdaq: `SKHY` ADS | KRW 2.18M | TVRemix + 发行文件 | 新股完成后约 KRW 1,588.9T |
| Lumentum | Nasdaq: `LITE` | US$802.01 | Futu | 行情源约 US$62.4B；期后转股后可核验下限约 US$66.4B |
| Coherent | NYSE: `COHR` | US$324.50 | Futu | 约 US$63.5B |
| Applied Optoelectronics | Nasdaq: `AAOI` | US$119.92 | Futu | 约 US$9.62B |
| AXT | Nasdaq: `AXTI` | US$57.21 | Futu | 约 US$3.74B |
| Sivers Semiconductors | Nasdaq Stockholm: `SIVE` | SEK 47.28 | TVRemix | 行情源约 SEK 12.35B；期后融资/转股后 pro forma 约 SEK 16.79B |
| Soitec | Euronext Paris: `SOI` | EUR 98.00 | 公开延迟行情 | 约 EUR 3.50B；点时股数仍为近似 |

同一行出现两个市值不是错误，而是在教数据血缘：行情源可能尚未反映财报后增发或债转股。遇到冲突时，必须保存行情源原值，再用公司文件做股本桥，不能悄悄覆盖其中一个数字。

本章没有直接抄行情终端的 P/E 排名。原因是行情源的 TTM 盈利、期后股数和公司最新财报可能不同步，周期高点利润还会让“低 P/E”产生误导。正确顺序是先用下方财务表重建利润和股数，再使用正常化利润或反向 DCF；无法统一口径的 P/E 应标 `stale/unknown`，而不是为了表格完整强行展示。

### B.2 先把十家公司放进同一张产业链地图

先核对证券，再谈业务。这里有六只美国普通股、三只海外主上市股票，以及一家同时存在韩国普通股和美国 ADS 的公司；它们不是十只可以直接在同一券商、同一币种、同一税务和同一交易时段买到的“美股”。

| 公司 | 截止日可核验证券 | 产业链位置 | 当前收入载体 | 下一代期权 | 最容易犯的错 |
|---|---|---|---|---|---|
| SanDisk | Nasdaq: `SNDK`，USD | NAND 晶圆、企业 SSD、客户端与消费存储 | NAND、Data Center SSD | 更高密度 NAND、企业 SSD、HBF 探索 | 把它当成旧 WDC，或当成 HBM 公司 |
| Micron | Nasdaq: `MU`，USD | HBM、DRAM、NAND、企业 SSD | HBM4、服务器 DRAM、数据中心 SSD | HBM4E、1-gamma DRAM、G9 NAND | 用周期高点利润算“低 P/E”后永久外推 |
| Samsung Electronics | KRX: `005930` 普通股、`005935` 优先股，KRW | Memory、Foundry、手机、显示、家电 | HBM4、DRAM、NAND、Foundry base die | HBM4E、先进封装、2nm | 把 DS 或 Memory 以外的利润也全部算成 AI |
| SK hynix | KRX: `000660`；Nasdaq: `SKHY` ADS，KRW/USD | HBM、服务器 DRAM、Solidigm 企业 SSD | HBM4、DRAM、eSSD、SOCAMM2 | HBM4E、cHBM | 忘记 `1 ADS=0.1 普通股`，或忽略新股稀释与跨市场基差 |
| Lumentum | Nasdaq: `LITE`，USD | InP 激光芯片、EML、泵浦激光、模块、OCS | 激光器件、云收发器、OCS | UHP/CW 激光、CPO 外置光源 | 把 Components、Systems 全部当 AI，或用旧股数算市值 |
| Coherent | NYSE: `COHR`，USD | InP/VCSEL/SiPh、激光、模块、传输与 CPO | Datacenter & Communications | 400G/lane、3.2T、CPO | 把分部收入占比当市场份额或纯 AI 收入 |
| Applied Optoelectronics | Nasdaq: `AAOI`，USD | 自制激光芯片、组件与光模块；另有 CATV | 首个 hyperscaler 800G 批量出货 | 1.6T 与新增产能 | 把月产能当出货，把 Amazon 权证条件当 backlog |
| AXT | Nasdaq: `AXTI`，USD | InP/GaAs/Ge 化合物半导体衬底 | 数据中心/PON 所需 InP 衬底 | 6-inch InP、扩产 | 把全部 substrate revenue 当 AI，忽略出口许可与增发 |
| Sivers Semiconductors | Nasdaq Stockholm: `SIVE`，SEK | InP DFB/CW 激光与阵列；另有 Wireless | Photonics 开发收入、Wireless NRE/产品 | LRO、CPO/NPO/ELS | 把 US$799M opportunity pipeline 当 backlog，或忽略连续融资 |
| Soitec | Euronext Paris: `SOI`，EUR，ISIN `FR0013227113` | Photonics-SOI 等工程化衬底平台 | Photonics-SOI、RF/FD/Power-SOI、POI | CPO 所需 Photonics-SOI 新产能 | 把股票代码 `SOI` 与材料缩写混淆，或把整个 Edge & Cloud AI 当数据中心 AI |

把十家公司沿物理 BOM 排列，会看到资金大致这样传导：

```text
Hyperscaler / GPU / ASIC CapEx
    ├─ 内存带宽与容量
    │   ├─ HBM / DRAM：MU、Samsung、SK hynix
    │   └─ NAND / 企业 SSD：SNDK、MU、Samsung、SK hynix/Solidigm
    └─ 网络带宽与光互联
        ├─ 工程化衬底：SOI
        ├─ InP 衬底：AXTI
        ├─ 激光芯片/阵列：LITE、COHR、AAOI、SIVE
        └─ 模块/子系统/OCS：LITE、COHR、AAOI
```

这棵树不是收入归属表。上游衬底会进入激光器，激光器再进入模块，同一笔终端支出会在产业链多次变成不同公司的收入；把各层公司收入相加不会得到“AI 市场规模”。

### B.3 第一组实战：SNDK、MU、Samsung、SK hynix 的存储比较

#### B.3.1 最新完整季度：同样是存储，利润来源并不相同

| 公司与期间 | 收入 / YoY | 毛利率 / 营业利润率 | 净利润 | CFO | Gross cash PP&E / Simple FCF | 期末现金与投资 / 有息债务 |
|---|---:|---:|---:|---:|---:|---:|
| SNDK FY26 Q3，截至 2026-04-03 | US$5.950B / +251% | 78.4% / 69.1% | US$3.615B | US$3.038B | US$45M / US$2.993B；另有 JV 现金前端投资 US$38M | US$3.735B / US$0 |
| MU FY26 Q3，截至 2026-05-28 | US$41.456B / +345.7% | 84.6% / 80.4% | US$28.243B | US$25.388B | US$7.826B / US$17.562B；公司净 CapEx 口径对应 adjusted FCF 约 US$18.304B | US$30.130B / US$5.722B |
| Samsung 1Q26，截至 2026-03-31 | KRW 133.9T / +69.2% | 61.2% / 42.7% | KRW 47.2T | KRW 40.27T | KRW 17.13T / KRW 23.14T | KRW 147.38T / KRW 28.14T |
| SK hynix 1Q26，截至 2026-03-31 | KRW 52.576T / +198% | 79.3% / 71.5% | KRW 40.346T | KRW 26.330T | KRW 7.657T / KRW 18.673T | 现金及短期金融资产约 KRW 54.3T / KRW 19.3T |

来源：[SNDK FY26 Q3 业绩](https://investor.sandisk.com/news-releases/news-release-details/sandisk-reports-fiscal-third-quarter-2026-financial-results)、[SNDK 10-Q](https://www.sec.gov/Archives/edgar/data/2023554/000162828026029401/sndk-20260403.htm)、[SNDK CapEx 演示](https://investor.sandisk.com/static-files/8ea78860-f8e5-4f1c-ada3-c554437d6281)、[MU FY26 Q3 业绩](https://investors.micron.com/news-releases/news-release-details/micron-technology-inc-reports-record-results-third-quarter)、[MU 10-Q](https://www.sec.gov/Archives/edgar/data/723125/000072312526000015/mu-20260528.htm)、[Samsung 1Q26 演示](https://images.samsung.com/is/content/samsung/assets/global/ir/docs/2026_1Q_conference_eng.pdf)、[SK hynix 1Q26 业绩](https://news.skhynix.com/q1-2026-business-results/)、[SK hynix F-1/A](https://www.sec.gov/Archives/edgar/data/2120882/000119312526295501/d32785df1a.htm)。

四家公司都出现了异常高的毛利和利润，但不要把它理解成四家都形成永久垄断。存储利润大致可拆成：

```text
Revenue_t = Bits shipped_t × ASP per bit_t × Mix factor_t
Revenue_t / Revenue_(t-1) = (1 + g_bits) × (1 + g_ASP) × (1 + g_mix)
Revenue growth = 上述乘积 - 1
利润变化 ≈ 收入变化 - wafer、封装、折旧、良率、库存和期间费用变化
```

只有在各项变化都很小时，才可使用 `收入增速 ≈ bit 增速 + ASP 增速 + mix 影响` 的一阶近似。大幅涨价时交叉项不可忽略：MU DRAM 的低 60% 区间 ASP 增长乘以低个位数 bit 增长，能够解释约 67% 的销售增长，而不是简单把“价格”和“量”当彼此独立的百分点。

SNDK 当季全公司 exabytes 同比基本持平，而每 GB ASP 同比约 +248%，说明收入爆发主要来自价格和产品组合；MU 同时受 HBM、DRAM、NAND 和长期协议推动；Samsung 和 SK hynix 的利润也含普通 DRAM/NAND 周期。结构性 AI 需求可以抬高周期中枢，但不能取消周期。

#### B.3.2 AI 暴露：用可报告收入，不用“纯度感觉”

| 公司 | 可核验业务数据 | 本案例册的 AI 暴露判断 |
|---|---|---|
| SNDK | Data Center US$1.467B，+645%，占合并收入 24.7%；Edge US$3.663B；Consumer US$820M | 纯 NAND 表达，不是纯 AI；Data Center 是较强代理，但仍含不同服务器和存储工作负载 |
| MU | DRAM US$31.328B，占 75.6%；NAND US$9.943B，占 24.0%；Cloud Memory 与 Core Data Center 合计占 61.0% | 综合存储表达；HBM、服务器 DRAM 和 SSD 同时受益，但公司没有单列完整 HBM 收入 |
| Samsung | Memory KRW 74.8T，占合并收入 55.9%；DS 收入 KRW 81.7T | Memory 是强暴露；股票还包含手机、显示、家电、System LSI 和 Foundry，不能把 DS 全算 AI |
| SK hynix | DRAM KRW 40.659T，占 77.3%；NAND KRW 11.574T，占 22.0% | 四者中 HBM 暴露最直接，但公司仍不单列 HBM 审计收入，Solidigm 企业 SSD 和普通内存周期也重要 |

市场份额要回到案例 A 的单一来源口径。按 Counterpoint 2026 Q1 **收入份额**估计，DRAM 为 Samsung 38%、SK hynix 29%、Micron 22%，HBM 为 SK hynix 58%、Samsung 21%、Micron 21%；按 TrendForce 同期 NAND 收入口径，Samsung 31.6%、SK hynix Group 17.6%、Micron 与 Sandisk 各 13.9%。HBM 是 DRAM 子集，Counterpoint 与 TrendForce 又是不同机构、不同产品分母，不能相加或合成一个范围。

SK hynix 的发行文件还引述 IDC 等其他机构估计；这些只能在另表以“机构—期间—产品—分母”逐行展示，不能与 Counterpoint 端点拼成 `56.4%–58%` 后称为同口径。

#### B.3.3 合同、产能与产品：状态词比宣传词重要

| 公司 | 已验证状态 | 下一状态 | 不能怎样误读 |
|---|---|---|---|
| SNDK | 数据中心收入已经进入财报；Q3 末有 3 份 NBM 协议，Q4 后又签 2 份；RPO US$41.6B | RPO 转收入、exabytes 增长、JV 产能与新产品爬坡 | RPO 不是当期收入；价格上涨不能替代真实出货增长 |
| MU | HBM4 已在首个客户平台高量出货；累计 HBM4 收入超过 US$1B；多客户送样 | HBM4 客户扩大、HBM4E 在 2027 年量产、美国 fab 执行 | 客户预付款/存款不是利润；长协价格区间可能同时限制下行和上行 |
| Samsung | 已开始销售用于 NVIDIA Vera Rubin 的 HBM4 与 SOCAMM2；推进 HBM4E 样品与 PCIe Gen6 SSD | 更多客户认证、份额恢复、Foundry base-die 贡献 | “开始销售”不等于多数市场份额，也不等于合并利润全来自 HBM |
| SK hynix | HBM4 进入量产体系；HBM4E 已送样；192GB SOCAMM2 量产 | HBM4E 客户转量产、M15X 与 P&T7 按期投产 | 领先份额不是永久份额，扩产也可能造成下一轮供给过剩 |

产品来源：[MU 产品里程碑](https://investors.micron.com/node/50671)、[Samsung 1Q26 业绩](https://news.samsung.com/global/samsung-electronics-announces-first-quarter-2026-results)、[SK hynix HBM4 量产准备](https://news.skhynix.com/sk-hynix-completes-worlds-first-hbm4-development-and-readies-mass-production/)、[SK hynix 192GB SOCAMM2](https://news.skhynix.com/mass-production-socamm2-192gb/)。

#### B.3.4 股本桥和估值：高利润不等于低风险

估值统一使用下式：

```text
点时基本市值 = 2026-07-10 收盘价 × 截止日可核验发行在外股数
简化 EV = 基本市值 + 有息债务 - 非受限现金及短期投资
当前季度 run-rate P/S = 基本市值 ÷（最新季度收入 × 4）
当前季度 run-rate EV/Sales = 简化 EV ÷（最新季度收入 × 4）
```

这两个 run-rate 只描述最新季度经营速度，是诊断值，不是未来十二个月预测。由于本 vintage 尚未为四家公司统一补齐 TTM 与中周期收入，下表禁止形成估值高低排名。

| 公司 | 观察日价格与股数 | 点时股权价值 / 简化 EV | 当前季度 run-rate（诊断值） | 证券结构重点 |
|---|---|---:|---:|---|
| SNDK | US$1,915.92 × 148.090M | 约 US$283.7B / US$280.0B | P/S 11.9x；EV/Sales 11.8x | diluted 平均股数 157M 不能用于点时市值；PSU 极端绩效情景仍有额外稀释 |
| MU | US$979.30 × 1.129B | 约 US$1.106T / US$1.081T | P/S 6.7x；EV/Sales 6.5x | diluted 平均股数 1.145B；另有未来股权奖励授权，但授权不等于已经稀释 |
| Samsung | 普通股 KRW 285,000 × 约 5.764B；优先股 KRW 194,300 × 约 0.802B | 合计股权价值约 KRW 1,798.7T / KRW 1,679.5T | P/S 3.4x；EV/Sales 3.1x | 必须同时估普通与优先股；回购注销减分母，库存股发给员工会重新增分母 |
| SK hynix | KRX 普通股 KRW 2.18M × 728.866M（发行完成后）；SKHY ADS 首日 US$168.01 | KRX 口径股权价值约 KRW 1,588.9T；ADS 隐含约 US$1.225T | KRX 口径 P/S 约 7.6x；pro forma EV 不宜伪精确 | `1 ADS=0.1 普通股`；177.9M ADS 对应 17.79M 新普通股，旧股约稀释 2.5% |

美股价格来自本地 Futu OpenD 10.5.6508 只读快照（`observation=2026-07-10 close; fetch=2026-07-13; quality=licensed_market_data/local_snapshot; licence_scope=local research; fallback=no`）。Samsung 与 SK hynix 的 KRX 收盘来自 TVRemix 跨市场缓存，[Samsung IR](https://www.samsung.com/global/ir/)只用于股本桥和公司披露，不作为历史收盘来源。Samsung 普通股该日 O/H/L/C 为 KRW 291,000/298,000/282,000/285,000、成交量 20,088,811，可由 [Twelve Data 历史行情](https://twelvedata.com/markets/150300/stock/krx/005930/historical-data)复核；优先股收盘 KRW 194,300、成交量 3,730,862，可由 [StockInvest 历史行情](https://stockinvest.us/stock-price/005935.KS)复核（`quality=third_party_market_data; fetch=2026-07-13`）。SKHY 发行与比例来自 [最终 424B4](https://www.sec.gov/Archives/edgar/data/2120882/000119312526299963/d32785d424b4.htm)，美国首日收盘来自 [AP 市场报道](https://apnews.com/article/73f13a85ae00e30bad0540281bbe44f3)。本地/缓存行情对公开读者不可完全重放，因此只能标为本次研究快照，不能冒充永久可复现的公开行情源。

Samsung 股数不是把旧年报数字原样搬来：2026 年 4 月公司注销 73.359M 普通股和 13.603M 优先股，并在后续回购后持有约 82.087M 普通库存股；上表因此分别计算普通股和优先股的发行在外数量。来源：[股份注销公告](https://www.samsung.com/global/ir/reports-disclosures/public-disclosure-view.84615/)、[回购结果公告](https://www.samsung.com/global/ir/reports-disclosures/public-disclosure-view.84635/)。

SK hynix 特别值得完整走一次股本桥：

```text
发行前普通股 outstanding = 711.076M
+ 新发普通股                 = 17.790M
= 发行后普通股 outstanding   = 728.866M

1 韩国普通股 = 10 ADS
ADS 首日隐含普通股价格 = US$168.01 × 10 = US$1,680.10
```

KRX 与 Nasdaq 不同交易时段、币种和结算机制，加上 ADS 刚上市时的供需，可能产生明显基差。`SKHY 价格 × 10` 与 KRX 股价换汇后的差额不是免费套利承诺，还要考虑可转换/注销 ADS 的流程、费用、汇率、交收、借券和时区风险。

<a id="casebook-mu-cashflow"></a>

#### B.3.5 Micron 现金流与反向估值桥

MU FY26 Q3 的口径桥如下，所有数值均来自同一报告期：

```text
GAAP CFO                                  US$25.388B
- gross cash PP&E                         US$ 7.826B
= Simple FCF                              US$17.562B
+ 政府资本激励                            US$ 0.733B
+ 资产出售                                US$ 0.009B
= company-adjusted FCF                    US$18.304B

cash + marketable investments             US$30.130B
- debt                                    US$ 5.722B
= 简化净现金                              US$24.408B
```

来源：[MU FY26 Q3 10-Q](https://www.sec.gov/Archives/edgar/data/723125/000072312526000015/mu-20260528.htm)、[财报及 adjusted FCF reconciliation](https://investors.micron.com/node/50671/pdf)。Simple FCF 是本教程的统一分析口径；adjusted FCF 是公司口径，两者必须并列，不能混称为同一个“自由现金流”。

按 2026-07-10 收盘价 US$979.30 与 1,129,393,151 股点时普通股，基本股权价值约 US$1.106T；只调整上述净现金后的简化 EV 约 US$1.081T。若用最简单的可持续股权 FCF 收益率反推，现价要求的年度 FCF 为：

| 假设的可持续股权 FCF yield | 现价隐含年度 FCF |
| ---: | ---: |
| 3% | 约 US$33.18B |
| 4% | 约 US$44.24B |
| 5% | 约 US$55.30B |

这是阈值诊断，不是公平价值。FY26 Q3 Simple FCF 直接年化为 US$70.248B，看起来高于上述阈值，但该季度同时处于 ASP、利用率和毛利的高位，不能把峰值 run-rate 当正常化现金流。

再做一个简化反向股权 DCF：十年显式期、股权成本 10%、终值增长 3%，暂不假设未来回购或稀释。为解释约 US$1.106T 的基本股权价值，不同正常化起点需要的十年 FCF 路径约为：

| 起始正常化 Simple FCF | 隐含十年 CAGR | 第十年 FCF |
| ---: | ---: | ---: |
| US$40B | 约 11.5% | 约 US$118.9B |
| US$50B | 约 8.5% | 约 US$113.1B |
| US$60B | 约 6.0% | 约 US$107.9B |
| 错用 Q3 年化峰值 US$70.248B | 约 3.9% | 约 US$103.1B |

反向 DCF 不是目标价。它说明：对周期股，**正常化 FCF 起点**比小数点精度更重要；若用峰值季度作为起点，模型会机械地把所需增长压低，使估值看起来过于轻松。正式结论还需补完全稀释股权价值、TTM、资本开支周期、税率、终值敏感性和发布前市场预期桥。

#### B.3.6 四家公司如何跑琳姐方法论

| 公司 | 钱流、架构与瓶颈 | 证据、交付与证券结构 | 催化、反证与组合 |
|---|---|---|---|
| SNDK | 钱流向大容量企业 SSD；瓶颈在 NAND 有效产能、良率、Kioxia JV 与认证 | 纯 NAND，但数据中心收入仅 24.7%；零债务，仍需区分 basic 与 diluted 股数 | 看 RPO 转收入、exabytes 与 ASP；反证是量停价跌、JV 执行差；不能与 HBM 股票等同 |
| MU | AI 同时拉动 HBM、服务器 DRAM 与 SSD；瓶颈在先进 wafer、堆叠封装、良率和新 fab | 美国上市的综合存储表达；净现金约 US$24.4B，但 FY26 净 CapEx 指引很高 | 看 HBM4/HBM4E 与长协；反证是份额、ASP、CapEx 回报不达预期；与其他内存股高度相关 |
| Samsung | Memory、Foundry base die 和终端均承接资本流；瓶颈是 HBM 认证、良率、封装与 Foundry 执行 | AI 内存资产强，但股票纯度最低；普通/优先股与库存股必须合并分析 | 看 HBM 份额恢复、2nm 与资本回报；反证是认证再延迟和 Foundry 继续拖累 |
| SK hynix | Hyperscaler/GPU/ASIC 直接传导至 HBM、服务器 DRAM 和 eSSD；瓶颈在堆叠、散热、封装和扩产 | 四者中 HBM 暴露最纯；KRX 普通股与 SKHY ADS 双结构，新发股稀释 | 看 HBM4E、SOCAMM2 与扩产；反证是份额被侵蚀、极端利润率回归和 ADS 溢价收敛 |

这组案例的核心不是选出“最好的一家”，而是学会区分四种表达：`SNDK=纯 NAND/企业 SSD`，`MU=美国综合存储`，`Samsung=跨内存、代工和终端的平台`，`SK hynix=高 HBM 暴露加双市场证券结构`。

### B.4 第二组实战：LITE、COHR、AAOI 的光互联比较

#### B.4.1 最新完整季度：增长、利润和现金转换必须一起看

| 指标 | LITE FY26 Q3，截至 2026-03-28 | COHR FY26 Q3，截至 2026-03-31 | AAOI 1Q26，截至 2026-03-31 |
|---|---:|---:|---:|
| 收入 / YoY | US$808.4M / +90.1% | US$1.806B / +21.0% | US$151.1M / +51.4% |
| GAAP 毛利率 | 44.2% | 37.7% | 29.1% |
| GAAP 营业利润率 | 21.6% | 约 11.1%（计算） | -8.6% |
| GAAP 净利润 | US$144.2M；归普通股 US$142.5M | 归属 COHR US$191.4M | -US$14.3M |
| 单季 CFO | US$203.8M | -US$93.8M（累计差额还原） | -US$85.4M |
| 单季 CapEx | US$124.7M | US$289.7M（累计差额还原） | US$58.2M |
| Simple FCF（CFO - gross cash PP&E） | +US$79.1M | -US$383.5M | -US$143.6M |

来源：[LITE FY26 Q3 10-Q](https://www.sec.gov/Archives/edgar/data/1633978/000162828026030777/lite-20260328.htm)、[LITE 业绩](https://investor.lumentum.com/financial-news-releases/news-details/2026/Lumentum-Announces-Third-Quarter-of-Fiscal-Year-2026-Financial-Results/default.aspx)、[COHR FY26 Q3 10-Q](https://www.sec.gov/Archives/edgar/data/820318/000082031826000013/iivi-20260331.htm)、[COHR 业绩](https://www.coherent.com/news/press-releases/third-quarter-fiscal-year-2026-results)、[AAOI 1Q26 10-Q](https://investors.ao-inc.com/node/17021/html)、[AAOI 业绩](https://investors.ao-inc.com/node/17011)。

现金流的还原公式必须写出来：

```text
LITE Q3 CFO = 9M CFO 388.4 - 6M CFO 184.6 = US$203.8M
LITE Q3 CapEx = 9M 284.5 - 6M 159.8 = US$124.7M

COHR Q3 CFO = 9M 10.1 - 6M 103.9 = -US$93.8M
COHR Q3 CapEx = 9M 547.2 - 6M 257.5 = US$289.7M
```

这张表直接推翻“收入增长越快，普通股越安全”的直觉：LITE 已把增长转成营业利润和正 FCF；COHR 有利润但季度现金转换为负；AAOI 收入增长 51%，毛利率却下降，营业亏损和现金消耗扩大。

#### B.4.2 收入纯度、客户和量产状态

| 公司 | AI/数据中心证据 | 客户与合同 | 产能/下一状态 | 关键限定 |
|---|---|---|---|---|
| LITE | Components US$533.3M、Systems US$275.1M；云收发器增量明显；OCS 当季收入超过 US$25M | 两个客户分别占收入 26% 与 12%；NVIDIA 非独家多年协议含采购承诺与未来产能权 | Greensboro 约 24 万平方英尺 6-inch InP 厂预计 2028 年中开始爬坡 | 公司不披露纯 AI 收入；采购承诺不等于当期收入，新厂不等于当前合格产能 |
| COHR | Datacenter & Communications US$1.362B，占 75.4%，同比 +41%；分部利润 US$348M | NVIDIA 以 US$2B 购买普通股，协议延伸至多类激光与网络产品 | Sherman 6-inch InP 扩建；CHIPS 最高 US$50M 尚是意向；1.6T/CPO 继续推进 | D&C 还含传统通信和 DCI；技术展示、LOI 与产能权都不是已确认收入 |
| AAOI | Data Center US$81.4M，占 53.9%，同比 +154%；首次向大型 hyperscaler 批量交付 800G | 前十大客户占 98%；Digicomm 占收入 44.1%、占应收约 74.5%；客户多为短采购订单 | Q1 末 800G 月产能接近 10 万只；美国和台湾继续扩产 | 月产能不是利用率/出货；Amazon 最多 US$4B 采购归属条件不是 backlog |

LITE 的 NVIDIA 合作与新厂来源：[战略合作](https://investor.lumentum.com/financial-news-releases/news-details/2026/NVIDIA-Announces-Strategic-Partnership-With-Lumentum-to-Develop-State-of-the-Art-Optics-Technology/default.aspx)、[Greensboro 新厂](https://investor.lumentum.com/financial-news-releases/news-details/2026/Lumentum-Announces-New-U-S--Manufacturing-Facility-to-Produce-Advanced-Lasers-for-the-Worlds-Largest-AI-Data-Centers/default.aspx)。COHR 来源：[NVIDIA 合作](https://www.coherent.com/news/press-releases/nvidia-and-coherent-announce-strategic-partnership)、[CPO 展示](https://www.coherent.com/news/press-releases/coherent-co-packaged-optics-cpo-technologies-ofc-2026)、[CHIPS 意向书](https://www.coherent.com/news/press-releases/a-chip-letter-of-intent-for-50m-to-expand-world-leading-manufacturing-facility-for-ai-infrastructure)。

#### B.4.3 股数、稀释与估值：行情页市值也可能过时

| 公司 | 现金/投资与有息债务 | 股本桥 | 2026-07-10 价格与基本市值 | 估值诊断（禁止跨公司排序） |
|---|---|---|---:|---:|
| LITE | Q3 现金 US$2.618B + 短投 US$0.555B；报告期债务 US$3.282B | 4/30 普通股 77.8M；4 月债转股约 5.7M 已大致包含其中；6 月再发约 5.0M，故截止日已知普通股最低约 82.8M；另有优先股、RSU/PSU 与剩余可转债 | US$802.01 × 约 82.8M = 约 US$66.4B | 当前 run-rate P/S 约 20.5x；因股数、债务、优先股和现金不在同一时点，完整 pro forma EV 与 EV/Sales 均为 `unknown` |
| COHR | 非受限现金 US$1.593B + 短投 US$0.825B；债务 US$3.194B | 5/4 普通股 195.639M；GAAP diluted 平均股数 196.367M；原 Series B 已转换，NVIDIA 7.788M 股是已发行普通股 | US$324.50 × 195.639M = US$63.485B | 简化 EV US$64.261B；run-rate P/S 8.8x；EV/Sales 8.9x；TTM/正常化待补 |
| AAOI | 现金 US$439.7M；标准有息债务约 US$170.7M | 当前 80.243M；已知反稀释工具约 6.45M；若 Amazon 剩余 6.621M 权证全归属，毛额情景约 93.31M | US$119.92 × 80.243M = US$9.623B | 简化 EV US$9.354B；run-rate P/S 15.9x；EV/Sales 15.5x；TTM/正常化待补；亏损所以 P/E=N/M |

三只美股价格来自同一 Futu OpenD 只读快照。LITE 是跨时点资本结构风险最好的教材：行情源按 77.8M 股显示约 US$62.4B 市值，但 6 月债转股之后，截止日可核验普通股下限已约 82.8M，对应约 US$66.4B；与此同时，报告期债务已经被后续交换部分冲减，另有可转优先股、剩余可转债和无法完全同步的现金变化。不能把期后股数与期前债务、现金拼成一个精确 EV，故完整稀释股权价值、EV 与 EV/Sales 都保留 `unknown`。

Q3 10-Q 还披露约 2.9M 股 Series A preferred，按 1:1 可转换普通股。仅把这项加到上述已知普通股下限，as-converted 股本下限约 85.7M、对应股权价值约 US$68.73B、季度 run-rate P/S 约 21.3x；但深度价内可转债、RSU/PSU、capped call 结算及期后现金/债务仍未同日闭合，所以这仍不是完整稀释价值。

LITE 2026 年又用约 5.7M 股交换 US$474.6M 可转债、用约 5.0M 股交换 US$650.4M 可转债；债务下降的同时普通股增加，不能只写“去杠杆”。来源：[4 月交换](https://www.sec.gov/Archives/edgar/data/1633978/000119312526146256/d13152d8k.htm)、[6 月交换](https://www.sec.gov/Archives/edgar/data/1633978/000119312526249535/d112771d8k.htm)。

AAOI 的融资桥同样关键：2026 年 2 月设立、3 月扩大的 ATM 最终累计发行约 4.8M 股，净募资约 US$490M；Amazon 权证最多 7.945M 股，其中约 1.324M 已归属，其余取决于未来采购。ATM 让公司获得扩产现金，也让旧股分母变大；正确问题是新资本未来创造的每股价值能否超过稀释成本。

#### B.4.4 三家公司如何跑琳姐方法论

| 公司 | 钱流、架构与瓶颈 | 证据、交付与证券结构 | 催化、组合与反证 |
|---|---|---|---|
| LITE | 当前 800G/1.6T 可插拔，OCS 与激光器已产生收入；CPO/UHP/CW 激光是后续；瓶颈在 InP 产能、良率、可靠性与认证 | 器件加系统表达；现金充足但可转债、优先股和债转股使每股分析复杂 | 看 Q4 指引、OCS、CPO 与 2028 新产能；反证是客户集中、ASP 下降、爬坡延期和现金转换恶化 |
| COHR | 覆盖 InP、VCSEL、SiPh、模块和系统；当前 800G，1.6T/400G-lane 爬坡，CPO 是下一阶段 | 技术栈最宽、规模最大、AI 纯度最低；债务、库存和 CapEx 仍高 | 看 1.6T、CPO、Sherman 与 CHIPS 最终协议；反证是 CFO 持续为负、库存无法转销售和 Industrial 拖累 |
| AAOI | 400G 为存量、800G 刚批量、1.6T 为下一阶段；瓶颈在芯片、模块产能、良率、测试和营运资金 | AI 弹性高但规模小；ATM、可转债、RSU 和 Amazon 权证构成多层稀释 | 看 Q2/Q3 出货、毛利与 CFO；反证是产能利用不足、应收恶化、继续融资和订单未转收入 |

三家公司放在一个组合里也不是三种独立风险：它们共同暴露于 hyperscaler CapEx、800G/1.6T 迁移、InP 供给、客户认证和光通信估值因子。组合层（Portfolio）要按共同因子做压力测试，而不是只数 ticker。

### B.5 第三组实战：AXTI、SIVE、SOI 的上游材料与激光比较

#### B.5.1 先分清 substrate、laser 与 engineered substrate

AXTI、SIVE 和 SOI 都能出现在“AI 光子上游”叙事里，但并不处于同一 BOM 层：AXTI 生产 InP 等**块状衬底**，SIVE 设计/制造 InP **激光器与阵列**并经营 Wireless，SOI 用 Smart Cut 等工艺生产 Photonics-SOI 等**工程化衬底**。三者没有一个可以用另外两家的收入倍数直接替代。

| 指标 | AXTI 1Q26 | SIVE 1Q26 | SOI FY26 全年 |
|---|---:|---:|---:|
| 收入 / YoY | US$26.924M / +39.1% | SEK 61.9M / -22% | EUR 592M / -34% reported；-30% constant FX/scope |
| 毛利率 | 29.6% | 6.8% | 16.3% |
| IFRS/GAAP 营业利润率 | -5.9% | -67.0% | -22.1% |
| 净利润率 | -6.0% | -69.0% | -37.2% |
| CFO | -US$11.684M | -SEK 49.2M | EUR 202M |
| CapEx | PPE US$1.372M | PPE SEK 0.9M + 资本化无形资产 SEK 10.7M | EUR 135M |
| Simple FCF（CFO - gross cash PP&E） | -US$13.056M | -SEK 50.1M | EUR 67M |
| Company-adjusted / owner-oriented | `unknown` | 若再扣资本化开发与其他无形投资，约 -SEK 60.8M | 公司口径 EUR 63M（另扣净利息和其他财务支出 EUR 4M） |

来源：[AXTI 1Q26 10-Q](https://www.sec.gov/Archives/edgar/data/1051627/000143774926017054/axti20260331_10q.htm)、[AXTI 财报](https://investors.axt.com/Investors/news/news-details/2026/AXT-Inc--Announces-First-Quarter-2026-Financial-Results/default.aspx)、[SIVE 1Q26 报告](https://www.sivers-semiconductors.com/wp-content/uploads/2026/05/Sivers-Interim-report-Q126_FINAL_ENG.pdf)、[SIVE 2025 重述年报](https://www.sivers-semiconductors.com/wp-content/uploads/2026/05/Sivers_annualreport_2025_2.pdf)、[SOI FY26 结果](https://www.soitec.com/docs/default-source/financial-documents/2025-2026/en/soitec-fy%2726-pr---en.pdf?Status=Master&sfvrsn=a4e6d7e6_1)。

SOI 还展示了 non-GAAP/管理层口径的陷阱：公司 `current operating loss` 约 EUR 8M、对应 -1.3%，但另有 EUR 123M other operating expenses，完整 IFRS operating loss 是 EUR 131M、对应 -22.1%。两者都可展示，却不能用前者替代后者。

#### B.5.2 AI 收入、客户和产能：三种证据强度

| 公司 | 当前可量化证据 | 客户集中 | 产能与认证 | 结论 |
|---|---|---|---|---|
| AXTI | substrate revenue US$19.281M，但含 InP/GaAs/Ge；公司称增长主要来自数据中心/PON InP 与更多出口许可 | FY25 无单一客户超过 10%；前五大合计 29% | 目标 2026 年 InP 产能翻倍、推进 6-inch；所有衬底在中国制造 | `AI revenue=unknown`；出口许可可能把物理稀缺转化为无法交付 |
| SIVE | Photonics SEK 17.8M，同比 -32%；Wireless SEK 44.1M；US$799M opportunity pipeline | FY25 三大客户超过 50%；ALL.SPACE 被称为最大单一客户 | Glasgow 自有线加 WIN、O-Net、POET、GF、Jabil 等合作；多个项目仍在开发/qualification | `AI revenue=unknown`；pipeline 不是 backlog，合作不是量产 |
| SOI | Edge & Cloud AI EUR 214M；constant FX/scope +8%；排除 Imager-SOI +19%；Photonics-SOI 超过 US$100M | FY26 前五大 61%；两名客户各超过 10%，合计 40% | Bernin 与新加坡合计多条 200/300mm 产线；新加坡 Photonics-SOI 仍在认证 | 三者中 AI 相关已实现收入最清楚，但 EUR 214M 仍含 FD-SOI/Imager-SOI，不是纯数据中心 AI |

SOI 公开产能是一张很好的“installed capacity ≠ qualified capacity”案例：Bernin 1 约 750k 片/年 200mm、Bernin 2 约 800k 片/年 300mm、Bernin 3 POI 可达 1M 片/年；新加坡当前约 800k 片/年 300mm，RF-SOI/FD-SOI 已认证，但 Photonics-SOI/Power-SOI 仍在认证。没有客户资格的产线不能直接乘 ASP 变成收入。详细来源：[SOI FY26 URD](https://www.soitec.com/docs/default-source/agm-documents/2026/en/soitec---2025-2026-urd---va.pdf?sfvrsn=6bd9e43b_1)、[AI/Photonics-SOI 演示](https://www.soitec.com/docs/default-source/financial-reports/2025-2026/en/soitec---enabling-ai-with-engineered-substrates-2026-01-06.pdf?Status=Master&sfvrsn=bfd1f78a_1)。

这三家的精确 AI 市场份额全部是 `unknown`：AXT 只称全球 InP 衬底有三家主要供应商；Sivers 对极窄 SATCOM beamformer 分母的发行人声明不能外推光子份额；Soitec 自称 world leader，却没有在免费公开材料中给出同期间、同产品、同单位的独立分母。网络流传的“95%”不能在没有可复核原始定义时进入主表。

#### B.5.3 融资、股数和 pro forma 估值

| 公司 | 报告期现金与债务 | 截止日股本桥 | 观察日价格、市值与简化 EV | 收入估值 |
|---|---|---|---:|---:|
| AXTI | 现金 US$41.769M、短投 US$65.375M、受限现金 US$16.1M；短债 US$68.9M、长债 US$6.8M；另有优先股清算权和可赎回 NCI | 3/31 为 55.579M；4 月发行 8.560M 并全额行使 1.284M 超额配售，5/4 为 65.423M，较此前约 +17.7% | US$57.21；市值约 US$3.743B；期后净现金和完整 pro forma EV 只给区间 | TTM P/S 约 39.0x；pro forma EV/Sales=`unknown`；亏损，P/E=N/M |
| SIVE | 3/31 现金 SEK 26.6M；银行/信用机构债务 SEK 46.7M、可转债 SEK 111.6M，另有租赁 | 311.334M + 4 月 8.620M + 7 月 12.281M + 7/3 债转股 22.847M = 约 355.081M | SEK 47.28；市值约 SEK 16.79B；期间现金消耗和交易费未知，pro forma EV 只给区间 | FY25 P/S 约 54.8x；pro forma EV/Sales=`unknown`；亏损，P/E=N/M |
| SOI | 现金 EUR 562M；金融债务 EUR 620M；公司定义净金融债务 EUR 56M | FY26 加权平均 35.674M，因缺少同日交易所点时股数，市值只作近似 | EUR 98.00；近似市值 EUR 3.496B；EV EUR 3.552B | P/S 5.9x；EV/Sales 6.0x；公司口径 EV/EBITDA 23.5x；FCF yield 约 1.8% |

AXTI 价格来自本地 Futu OpenD 快照；SIVE 与 SOI 为 `third_party_market_data` 的公开延迟行情，分别可由 [SIVE 历史行情](https://stockanalysis.com/quote/sto/SIVE/history/) 与 [SOI 历史行情](https://twelvedata.com/markets/220684/stock/euronext/soi/historical-data)复核。SIVE 的美国 `SIVEF` 只是 OTC 外国普通股报价，不是主上市；SOI 的点时股数缺少同日交易所确认，因此市值明确标“近似”。

股本事件来源：[AXTI 4 月发行文件](https://www.sec.gov/Archives/edgar/data/1051627/000121390026046176/ea0287123-424b5_axtinc.htm)、[SIVE 4 月 SEK 125M 定增](https://www.sivers-semiconductors.com/press/sivers-semiconductors-has-resolved-on-a-directed-share-issue-of-shares-amounting-to-approximately-125-msek/)、[SIVE 7 月 SEK 700M 定增](https://www.sivers-semiconductors.com/press/sivers-semiconductors-has-resolved-on-a-directed-share-issue-of-shares-amounting-to-approximately-sek-700-million/)、[SIVE 可转债转股](https://www.sivers-semiconductors.com/press/sivers-semiconductors-lender-bootstrap-europe-exercises-conversion-right-under-existing-convertible-loan/)。这些事件发生在最新财报期末之后，所以估值只能标 `first_party_calculation / pro_forma`，不能标 `company_reported`。

pro forma 不等于更准确。AXTI 的发行净额还需扣其他费用；SIVE 的 Q2 现金消耗、交易费和 SEK/USD 汇率会改变净债；SOI 使用加权平均股数而非严格点时股数。没有同日现金、债务、优先股、少数股东权益和完全稀释股数时，正确做法是给区间或 `unknown`，不是多写两位小数。

#### B.5.4 三家公司如何跑琳姐方法论

| 公司 | 钱流、架构与瓶颈 | 证据、交付与证券结构 | 催化、组合与反证 |
|---|---|---|---|
| AXTI | AI 光互联流向 InP 激光/探测器，再到 InP 衬底；当前可插拔与未来 CPO 都需要光源；瓶颈在晶体生长、低缺陷、6-inch、认证和出口许可 | 衬底纯度较高但收入规模小；4 月股数增约 17.7%，另有优先股、NCI 和股权工具 | 看许可、产能翻倍、6-inch 资格与收入/毛利；反证是许可延迟、良率失败、收入跟不上折旧；属于高估值执行风险表达 |
| SIVE | 资本流经 LRO、CPO/NPO/ELS 到 InP 激光；LiDAR、SATCOM 是不同收入池；瓶颈在功率、线宽、可靠性、认证和合作产能 | Photonics+Wireless 混合表达；三个月内两次增发和债转股，重述与客户集中必须优先处理 | 看 Jabil/GF/O-Net 等从 qualification 转量产；反证是合作停在开发、继续亏损融资；更接近期权型观察标的 |
| SOI | AI 数据中心经硅光收发/CPO 到 Photonics-SOI；壁垒在 Smart Cut IP、均匀性、大规模制造和长期认证 | 工程化衬底平台，AI 纯度较低但 CFO/FCF 为正、净债较低；需展示完整 IFRS 减值 | 看 Photonics-SOI、新加坡认证、CPO ramp 与 RF-SOI 去库存；反证是低利用率、库存修正和认证延期；适合材料平台比较 |

三家公司放在一起，能看清“产业纯度”和“投资质量”不是同一个排序：SIVE/AXTI 的远期弹性更高，但亏损、融资、许可和认证风险也更高；SOI 的 AI 纯度较低，却已经有更可核验的收入、CFO、FCF 和产能边界。

### B.6 十家公司横向归纳：方法论真正改变了什么

| 研究问题 | 存储四家给出的答案 | 光互联三家给出的答案 | 上游三家给出的答案 |
|---|---|---|---|
| 哪个数字最能证明当前收入？ | 可报告 Memory/DRAM/NAND/Data Center 收入 | D&C/Data Center/OCS 等分部或产品收入，但要剥离非 AI | SOI 的 Photonics-SOI 最直接；AXTI/SIVE 的纯 AI 收入仍 unknown |
| 最大架构误判 | 把 HBM 份额与 DRAM 份额相加，或把 NAND 当 HBM | 把 800G、1.6T、LRO、OCS、CPO 当一次性切换 | 把 substrate、engineered substrate、laser 当同一产品 |
| 最大瓶颈误判 | 高份额不自动等于持续短缺 | 展示/产能不等于良率和认证后的量产 | 安装产能不等于出口许可、客户资格和可交付产能 |
| 最大收入纯度误判 | Samsung DS、MU 全部收入或 SNDK 全部收入都不是纯 AI | LITE Components、COHR D&C、AAOI DC 均不是精确 AI 收入 | AXTI substrate、SIVE Photonics、SOI Edge & Cloud AI 都含非目标工作负载 |
| 最大证券结构风险 | 周期峰值利润、SKHY ADS、普通/优先股、股数分母 | 可转债、优先股、ATM、权证、客户持股 | 大额增发、债转股、NCI、会计重述和 pro forma 现金 |
| 催化如何写 | “HBM4E 从样品到量产”“RPO 转收入” | “800G/CPO 从认证到收入并转成现金” | “6-inch/新加坡产能从安装到合格出货” |
| 反证如何写 | ASP、份额、CapEx 回报和库存反转 | 良率、客户集中、CFO、库存和继续融资 | 许可、认证、合作停滞、现金消耗和稀释 |

十家公司没有一个可以只靠“AI TAM 很大”完成 thesis。已经进入量产和交付的成熟公司也可能处在利润率高点；最上游、最稀缺的公司也可能因许可、良率或融资无法把价值留给普通股股东；技术路线正确也可能因为市场已经按数十倍销售额定价而缺乏安全边际。

把研究结论压缩成一句标准句式：

```text
因为 [下游资本流] 正在推动 [明确架构变化]，
[具体产品/能力] 可能成为 [有证据的瓶颈]；
[公司] 通过 [可核验收入/合同/产能状态] 获得 [多少暴露]，
但当前普通股还要承担 [债务/稀释/估值/客户/周期]；
下一次可验证状态变化是 [日期或报告期 + 指标]，
若 [反证] 发生，thesis 失效。
```

举三个不同成熟度的示例：

- **MU：** HBM4 已量产且数据中心收入可量化，但高毛利与高 CapEx 都处在极端区间；下一步验证 HBM4E、份额、ASP 和 FCF，若长协无法抵御价格反转或 CapEx 回报下降，不能再按当前利润外推。
- **AAOI：** 800G 已首次批量交付，Data Center 收入也已增长，但公司仍亏损、CFO 为负并经历 ATM；下一步必须看到收入转毛利和现金，若产能只增长不产生合格出货，thesis 失败。
- **SIVE：** 多个合作仍处开发/qualification，Photonics 收入反而下降，连续融资后估值主要押注未来；下一步不是再收集合作 logo，而是等量产收入、毛利和现金消耗改善，若继续融资却不转量产，应下调 thesis 阶段。

### B.7 把十家公司逐一套入琳姐方法论

| 公司 | 瓶颈、寡头与利润弹性 | `capable → delivery` 当前状态 | 下一轮六向验证 | 明确证伪条件 |
|---|---|---|---|---|
| SNDK | NAND 有效供给、JV 良率与企业 SSD 认证；行业寡头但价格周期很强 | Data Center 收入、NBM 协议与 RPO 已形成商业证据；不是 HBM 公司 | 查 Kioxia JV、客户 SSD 采购、竞争者 NAND CapEx、exabytes、ASP 与 CFO | 量停价跌、RPO 不转收入、JV/产品爬坡失败 |
| MU | HBM/DRAM/NAND 同时受益；三家 DRAM 寡头，固定成本带来极高利润弹性 | HBM4 已进入高量出货；HBM4E 与更多客户仍需继续验证 | 查 GPU/ASIC 客户、Samsung/SK hynix、设备订单、HBM/DRAM 价格、wafer 与财报 | 份额下降、ASP 反转、CapEx 回报不足、FCF 恶化 |
| Samsung | Memory、Foundry 和终端跨层；规模最大但股票 AI 纯度最低 | HBM4 已开始销售，Foundry/更多 HBM 客户的稳定交付仍是核心 | 查客户认证、SK hynix/MU 份额、Foundry 良率、HBM 价格、先进封装与 DS 财务 | 认证再次延期、Foundry 持续拖累、资本回报继续下降 |
| SK hynix | HBM 领先份额、服务器 DRAM 与 eSSD；利润率对价格/利用率极敏感 | HBM4 量产体系、SOCAMM2 已量产；HBM4E 仍处下一阶段 | 查 NVIDIA/ASIC 客户、Samsung/MU、M15X/P&T7、HBM 价格和 K-IFRS 财务 | HBM 份额被侵蚀、扩产提前造成过剩、峰值利润快速回归 |
| LITE | InP 激光、200G EML、云收发器与 OCS；客户和 InP 合格产能集中 | 当前产品已出货并产生利润/现金；CPO 激光与 2028 新厂属于后续 | 查客户采购、COHR/AAOI、InP 设备与良率、模块 ASP、股本和 FCF | 客户集中恶化、ASP 下跌、新厂延期、债转股后每股价值不增 |
| COHR | InP/VCSEL/SiPh 到模块的技术栈最宽；规模大但收入纯度较低 | D&C 已有大额收入；1.6T/CPO 展示与扩产不等于成熟现金流 | 查 NVIDIA/客户、LITE/AAOI、Sherman/CHIPS、模块价格、库存与 CFO | CFO 持续为负、库存不能转销售、工业业务拖累、CapEx 回报不足 |
| AAOI | 800G/1.6T 与自制激光芯片带来高弹性，但客户集中、规模小 | 800G 首次批量交付；月产能不等于利用率、良率和收入 | 查 hyperscaler、LITE/COHR、产线/测试、模块价格、应收与现金流 | 产能增长不转出货、毛利不升、应收恶化、继续融资稀释 |
| AXTI | InP 晶体生长、低缺陷与出口许可可能是真瓶颈；全球合格供应商少 | 已有 InP 衬底收入；6-inch 和翻倍产能仍需许可、认证与良率 | 查器件客户、其他衬底商、出口许可、6-inch 设备、价格与毛利 | 许可延期、6-inch 良率失败、收入跟不上折旧、再融资 |
| SIVE | InP DFB/CW 激光与阵列有远期弹性，但 Photonics/Wireless 混合且持续亏损 | 多个项目仍在开发/qualification；US$799M pipeline 不是 backlog | 查合作客户、LITE/COHR/AAOI、代工伙伴、量产价格、收入和现金消耗 | 合作长期停在开发、Photonics 收入不升、继续融资且无量产 |
| SOI | Smart Cut、Photonics-SOI 均匀性和长期认证构成壁垒；平台业务纯度较低 | Photonics-SOI 已有收入；新加坡相关产能仍需客户认证 | 查硅光客户、同类衬底、认证进度、wafer 价格、利用率与 IFRS 财务 | 认证延期、低利用率、库存修正、完整 IFRS 利润继续恶化 |

这张表体现了方法论的核心顺序：**先找钱流和物理瓶颈，再确认少数供应商是否能稳定交付；随后验证价格/利用率能否进入利润，最后才处理估值和组合。**越靠上游、越小盘的公司，不能因为“更纯”就降低交付和融资证据门槛。

### B.8 小白如何使用这组案例

1. 先选一个产业组，不要从十家公司里直接挑涨幅最大的一只。
2. 用第 16.2 节确认证券、币种和产业位置，避免 ticker 与材料缩写混淆。
3. 抄写对应组最新财务表，并亲自重算一个同比、一个利润率和一个 FCF。
4. 把产品逐项标为 `开发 / 送样 / 认证 / 量产 / 交付 / 收入 / 现金`。
5. 按四条铁律写一页纸：瓶颈、寡头、利润弹性、扩产纪律。
6. 用供应商、客户、竞争者、价格、产能、财务六向验证，不用同源新闻凑数量。
7. 更新点时股数和期后融资，再计算市值；无法完成的完全稀释值保留 `unknown`。
8. 最后写三条证伪条件和共同风险，不写“AI 长期向好所以继续持有”。

完成后，你应该能解释为什么：SNDK 不能当 HBM 股票，Samsung 不能把整个集团算 AI，LITE/COHR/AAOI 的收入增长不代表同样的现金质量，AXTI/SIVE/SOI 也不是同一种“上游材料股”。这才是把方法论变成可复核判断，而不是把十个 ticker 换成十个新故事。

---
