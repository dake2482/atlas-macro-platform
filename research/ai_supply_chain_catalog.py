"""Reviewed clean-room catalogue for the public AI supply-chain node routes.

The slugs are a compatibility contract derived from the public route manifest in
``assets/timsun_public_ai_contract.json``.  Names and descriptions are original
Atlas Macro taxonomy text.  Every row links to a primary public source, while
scores, financial aggregates, valuations and investment theses are deliberately
left empty until a separately sourced data pipeline can support them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from django.db import transaction

from .models import SupplyChainNode


@dataclass(frozen=True, slots=True)
class SupplyChainNodeCatalogEntry:
    slug: str
    name: str
    layer: str
    description: str
    source_note: str


AI_SUPPLY_CHAIN_LAYERS: tuple[str, ...] = (
    "材料与晶圆",
    "前道设备",
    "晶圆制造",
    "先进封装与测试",
    "计算芯片",
    "存储与电源",
    "网络与光通信",
    "服务器与数据中心",
    "云、模型与应用",
)


AI_SUPPLY_CHAIN_NODE_CATALOG: tuple[SupplyChainNodeCatalogEntry, ...] = (
    # 1. Materials and wafers
    SupplyChainNodeCatalogEntry(
        "silicon-wafers",
        "半导体硅片",
        "材料与晶圆",
        "晶圆制造的基底材料，其尺寸、纯度、平整度与缺陷控制会影响后续制程的良率与成本。",
        "https://www.sumcosi.com/english/products/lineup.html",
    ),
    SupplyChainNodeCatalogEntry(
        "photoresist",
        "光刻胶与多层材料",
        "材料与晶圆",
        "用于把掩模图形转移到晶圆的感光材料体系，需要与曝光波长、显影和刻蚀步骤共同匹配。",
        "https://www.jsr.co.jp/jsr_e/products/em/biz/",
    ),
    SupplyChainNodeCatalogEntry(
        "electronic-specialty-gases",
        "电子特气",
        "材料与晶圆",
        "沉积、刻蚀、掺杂与腔室清洁中使用的高纯度气体，关键看供应稳定性、纯度和安全交付能力。",
        "https://www.linde.com/about-us",
    ),
    SupplyChainNodeCatalogEntry(
        "wet-chemicals",
        "半导体湿电子化学品",
        "材料与晶圆",
        "用于清洗、湿法刻蚀、去胶与表面处理的超高纯化学品，金属和颗粒污染控制是核心约束。",
        "https://www.entegris.com/en/home/our-science/by-industry/microelectronics/semiconductor.html",
    ),
    SupplyChainNodeCatalogEntry(
        "targets",
        "半导体溅射靶材",
        "材料与晶圆",
        "物理气相沉积所需的高纯金属与合金靶材，其成分、微结构和一致性直接影响薄膜质量。",
        "https://www.materion.com/en/products/electronic-materials/thin-film-deposition-materials/specialty-sputtering-targets",
    ),
    # 2. Front-end equipment
    SupplyChainNodeCatalogEntry(
        "lithography",
        "光刻设备",
        "前道设备",
        "将电路图形缩小并投影到晶圆的核心设备，曝光光源、光学系统、工件台和计算光刻共同决定解析度。",
        "https://www.asml.com/en/technology/lithography-principles",
    ),
    SupplyChainNodeCatalogEntry(
        "deposition",
        "薄膜沉积设备",
        "前道设备",
        "通过 CVD、ALD、ECD 等方法形成导体或绝缘薄膜，先进结构对厚度、覆盖性与界面缺陷提出更高要求。",
        "https://www.lamresearch.com/products/our-processes/deposition/",
    ),
    SupplyChainNodeCatalogEntry(
        "etch",
        "刻蚀设备",
        "前道设备",
        "选择性移除晶圆上的材料以形成器件结构，关键能力包括原子级精度、高深宽比与全片一致性。",
        "https://www.lamresearch.com/products/our-processes/etch/",
    ),
    SupplyChainNodeCatalogEntry(
        "ion-implantation",
        "离子注入设备",
        "前道设备",
        "将可控剂量和能量的离子注入晶圆，用于调节器件电学特性，并对污染、角度和产能进行精密控制。",
        "https://www.axcelis.com/products/purion-xe-series-high-energy-ion-implantation/",
    ),
    SupplyChainNodeCatalogEntry(
        "cleaning",
        "晶圆清洗设备",
        "前道设备",
        "在多个制程步骤之间去除颗粒、残留物与污染，同时尽量避免材料损失和精细结构损伤。",
        "https://www.lamresearch.com/product/eos/",
    ),
    # 3. Wafer manufacturing
    SupplyChainNodeCatalogEntry(
        "cmp-materials",
        "CMP 材料与耗材",
        "晶圆制造",
        "化学机械平坦化中使用的抛光液、抛光垫、清洗与过滤材料，用于控制去除速率、平整度和划伤缺陷。",
        "https://www.entegris.com/en/home/our-science/by-industry/microelectronics/semiconductor/cmp.html",
    ),
    SupplyChainNodeCatalogEntry(
        "metrology-inspection",
        "量测与缺陷检测",
        "晶圆制造",
        "对关键尺寸、套刻误差、薄膜与缺陷进行在线测量，为良率爬坡、制程窗口和设备控制提供反馈。",
        "https://www.kla.com/",
    ),
    SupplyChainNodeCatalogEntry(
        "advanced-nodes",
        "先进制程",
        "晶圆制造",
        "面向高性能和高能效芯片的领先逻辑工艺，通过晶体管结构、互连和设计工艺协同延续扩展。",
        "https://www.tsmc.com/english/dedicatedFoundry/technology/logic/l_2nm",
    ),
    SupplyChainNodeCatalogEntry(
        "mature-nodes",
        "成熟制程",
        "晶圆制造",
        "为模拟、射频、微控制器、汽车与工业芯片提供已验证工艺，优势更多来自成本、可靠性与产能。",
        "https://www.tsmc.com/english/dedicatedFoundry/technology",
    ),
    SupplyChainNodeCatalogEntry(
        "specialty-process",
        "特色制程",
        "晶圆制造",
        "覆盖图像传感、嵌入式存储、射频、模拟、高压和 BCD 等专用工艺平台，服务多样化终端市场。",
        "https://www.tsmc.com/english/dedicatedFoundry/technology/specialty",
    ),
    # 4. Advanced packaging and test
    SupplyChainNodeCatalogEntry(
        "2-5d-3d-packaging",
        "2.5D / 3D 封装",
        "先进封装与测试",
        "借助中介层、再布线层或垂直堆叠集成多枚裸片，以缩短数据路径并提高带宽密度。",
        "https://3dfabric.tsmc.com/english/dedicatedFoundry/technology/3DFabric.htm",
    ),
    SupplyChainNodeCatalogEntry(
        "cowos",
        "CoWoS 先进封装",
        "先进封装与测试",
        "将逻辑芯片与 HBM 在中介结构上进行高密度集成的封装族，是大型 AI 加速器的重要系统环节。",
        "https://3dfabric.tsmc.com/english/dedicatedFoundry/technology/cowos.htm",
    ),
    SupplyChainNodeCatalogEntry(
        "soic",
        "SoIC 三维堆叠",
        "先进封装与测试",
        "通过高密度垂直键合重新组合不同功能与工艺节点的芯粒，减少芯片间通信的距离和能耗。",
        "https://3dfabric.tsmc.com/english/dedicatedFoundry/technology/SoIC.htm",
    ),
    SupplyChainNodeCatalogEntry(
        "osat",
        "OSAT 封装测试",
        "先进封装与测试",
        "独立提供芯片组装、封装与量产测试的产业环节，负责把晶圆端产出转换为可交付器件或模块。",
        "https://amkor.com/technology/",
    ),
    SupplyChainNodeCatalogEntry(
        "test-equipment",
        "半导体测试设备",
        "先进封装与测试",
        "在研发和量产阶段对数字、模拟、射频及混合信号器件进行自动测试，筛除缺陷并验证性能。",
        "https://www.advantest.com/en/products/semiconductor-test-system/soc/",
    ),
    # 5. Compute silicon
    SupplyChainNodeCatalogEntry(
        "gpu",
        "GPU 加速器",
        "计算芯片",
        "以大规模并行算术和高带宽存储通道加速 AI 训练与推理，需与软件栈、互连和系统设计共同评估。",
        "https://www.nvidia.com/en-us/data-center/",
    ),
    SupplyChainNodeCatalogEntry(
        "ai-asic",
        "定制 AI ASIC",
        "计算芯片",
        "针对矩阵运算、数据移动或特定推理负载定制的专用芯片，以较低通用性换取能效和成本优化。",
        "https://cloud.google.com/tpu/docs/system-architecture-tpu-vm",
    ),
    SupplyChainNodeCatalogEntry(
        "cpu",
        "数据中心 CPU",
        "计算芯片",
        "承担操作系统、任务调度、数据预处理与加速器编排，其内存通道、PCIe 连接和每瓦性能影响整机效率。",
        "https://www.amd.com/en/products/processors/server/epyc.html",
    ),
    SupplyChainNodeCatalogEntry(
        "fpga",
        "FPGA 可编程加速",
        "计算芯片",
        "通过现场可重配逻辑实现特定数据通路，适合快速变化、低时延或需要定制 I/O 的计算和网络任务。",
        "https://www.amd.com/en/products/adaptive-socs-and-fpgas/fpga.html",
    ),
    SupplyChainNodeCatalogEntry(
        "chiplet",
        "Chiplet 芯粒",
        "计算芯片",
        "把大型系统拆分为可在不同工艺上制造的功能裸片，再经标准或专用的封装内互连组合。",
        "https://www.uciexpress.org/specifications",
    ),
    # 6. Memory and power
    SupplyChainNodeCatalogEntry(
        "hbm",
        "HBM 高带宽存储",
        "存储与电源",
        "将多层 DRAM 堆叠并在逻辑芯片附近以宽接口连接，为 AI 加速器提供高吞吐与较低比特能耗。",
        "https://www.micron.com/products/memory/hbm",
    ),
    SupplyChainNodeCatalogEntry(
        "dram",
        "DRAM 主存",
        "存储与电源",
        "服务器与加速系统的易失性主存，容量、速率、功耗和可靠性共同限制模型训练与推理的数据供给。",
        "https://www.micron.com/products/memory/dram-components",
    ),
    SupplyChainNodeCatalogEntry(
        "nand",
        "NAND 闪存",
        "存储与电源",
        "为数据集、模型权重和检索库提供非易失存储，位密度、写入寿命与每比特成本是关键指标。",
        "https://www.micron.com/products/storage/nand-flash",
    ),
    SupplyChainNodeCatalogEntry(
        "enterprise-ssd",
        "企业级 SSD",
        "存储与电源",
        "面向数据中心持续负载设计的固态存储，需同时考察随机 I/O、尾延迟、耐久性、保护机制与功耗。",
        "https://www.micron.com/products/storage/ssd/data-center-ssd",
    ),
    SupplyChainNodeCatalogEntry(
        "pmic",
        "电源管理芯片",
        "存储与电源",
        "将输入电源转换为处理器、存储和板级器件所需的多路电压，其效率、瞬态响应和热设计影响系统稳定性。",
        "https://www.ti.com/power-management/overview.html",
    ),
    # 7. Networking and optics
    SupplyChainNodeCatalogEntry(
        "infiniband-ethernet",
        "InfiniBand 与高速以太网",
        "网络与光通信",
        "连接大规模加速器集群的交换网络，需同时管理带宽、延迟、无损传输、拥塞与 RDMA 语义。",
        "https://www.infinibandta.org/ibta-specification/",
    ),
    SupplyChainNodeCatalogEntry(
        "optical-modules",
        "高速光模块",
        "网络与光通信",
        "在交换机与光纤之间完成电光转换，为机架间和数据中心间连接提供高数据率、可插拔的传输单元。",
        "https://www.oiforum.com/technical-work/implementation-agreements-ias/",
    ),
    SupplyChainNodeCatalogEntry(
        "cpo",
        "CPO 共封装光学",
        "网络与光通信",
        "将光学引擎放到交换或计算芯片附近，用更短的高速电连接换取带宽密度和能效改善。",
        "https://www.oiforum.com/technical-work/implementation-agreements-ias/",
    ),
    SupplyChainNodeCatalogEntry(
        "serdes",
        "高速 SerDes",
        "网络与光通信",
        "在芯片与链路之间完成高速串并转换、时钟恢复和信号均衡，是交换芯片、网卡与光模块的共同接口层。",
        "https://www.oiforum.com/technical-work/implementation-agreements-ias/",
    ),
    SupplyChainNodeCatalogEntry(
        "switching-asic",
        "网络交换 ASIC",
        "网络与光通信",
        "在数据中心交换机中执行线速转发、路由、隧道和拥塞控制，端口密度与缓冲策略影响集群利用率。",
        "https://www.broadcom.com/products/ethernet-connectivity/switching/strataxgs/bcm78910-series",
    ),
    # 8. Servers and data centres
    SupplyChainNodeCatalogEntry(
        "ai-servers",
        "AI 服务器",
        "服务器与数据中心",
        "把加速器、CPU、内存、高速互连、存储和电源散热集成为可部署的训练或推理节点。",
        "https://www.nvidia.com/en-us/data-center/dgx-platform/",
    ),
    SupplyChainNodeCatalogEntry(
        "racks",
        "机架级系统",
        "服务器与数据中心",
        "将服务器、网络、电源母线、线缆和冷却接口在机架层面统一，便于高密度设备的交付与维护。",
        "https://www.opencompute.org/index.php/community/rack-and-power",
    ),
    SupplyChainNodeCatalogEntry(
        "liquid-cooling",
        "数据中心液冷",
        "服务器与数据中心",
        "通过冷板、冷却液分配单元或浸没介质移走高热流密度，并对接口、材料兼容与运维流程提出新要求。",
        "https://www.opencompute.org/community/cooling-environments",
    ),
    SupplyChainNodeCatalogEntry(
        "ups-power",
        "UPS 与机房供配电",
        "服务器与数据中心",
        "在电网与 IT 负载之间提供配电、电能质量和短时后备，高密度 AI 机架使单柜容量、冗余与转换效率更为关键。",
        "https://www.opencompute.org/documents/ocp-open-rack-v3-power-shelf-rev-1-0-1-pdf",
    ),
    SupplyChainNodeCatalogEntry(
        "dpu-nic",
        "DPU 与高速 NIC",
        "服务器与数据中心",
        "在服务器端点处连接高速网络，并可把网络、存储、安全和基础设施管理从主 CPU 卸载到专用处理器。",
        "https://www.nvidia.com/en-us/networking/products/data-processing-unit/",
    ),
    # 9. Cloud, models, and applications
    SupplyChainNodeCatalogEntry(
        "cloud-providers",
        "云基础设施服务商",
        "云、模型与应用",
        "把计算、存储、网络与平台能力作为可弹性分配的服务交付，并承担 AI 加速资源的调度、计量和运维。",
        "https://csrc.nist.gov/pubs/sp/800/145/final",
    ),
    SupplyChainNodeCatalogEntry(
        "data-center-operators",
        "数据中心运营商",
        "云、模型与应用",
        "负责机房的选址、建设、电力和冷却容量、网络连接与日常运行，是把硬件资本开支转化为可用算力的载体。",
        "https://www.energy.gov/cmei/buildings/data-centers-and-servers",
    ),
    SupplyChainNodeCatalogEntry(
        "model-labs",
        "基础模型研发机构",
        "云、模型与应用",
        "组织数据、算法、计算和评测来训练可适配多种任务的模型，还需要处理发布、安全、治理与开发者接入。",
        "https://www.nist.gov/news-events/news/2025/01/updated-guidelines-managing-misuse-risk-dual-use-foundation-models",
    ),
    SupplyChainNodeCatalogEntry(
        "enterprise-ai",
        "企业级 AI 平台",
        "云、模型与应用",
        "将模型、企业数据、权限、工作流与可观测性整合进生产系统，重点在可控部署、审计与持续运营。",
        "https://airc.nist.gov/airmf-resources/airmf/",
    ),
    SupplyChainNodeCatalogEntry(
        "sovereign-ai",
        "主权 AI 基础设施",
        "云、模型与应用",
        "在特定国家或区域的法律、数据和算力边界内建设 AI 能力，关注数据控制、技术自主、访问权和公共算力供给。",
        "https://digital-strategy.ec.europa.eu/en/policies/ai-factories",
    ),
)


def validate_ai_supply_chain_catalog() -> None:
    """Fail fast when the checked-in route or provenance contract drifts."""

    if len(AI_SUPPLY_CHAIN_NODE_CATALOG) != 45:
        raise ValueError("AI supply-chain catalogue must contain exactly 45 nodes")

    slugs = [item.slug for item in AI_SUPPLY_CHAIN_NODE_CATALOG]
    if len(set(slugs)) != len(slugs):
        raise ValueError("AI supply-chain catalogue slugs must be unique")

    layer_counts = {
        layer: sum(item.layer == layer for item in AI_SUPPLY_CHAIN_NODE_CATALOG)
        for layer in AI_SUPPLY_CHAIN_LAYERS
    }
    if set(item.layer for item in AI_SUPPLY_CHAIN_NODE_CATALOG) != set(AI_SUPPLY_CHAIN_LAYERS):
        raise ValueError("AI supply-chain catalogue must use only the nine reviewed layers")
    if any(count != 5 for count in layer_counts.values()):
        raise ValueError("Each AI supply-chain layer must contain exactly five nodes")

    for item in AI_SUPPLY_CHAIN_NODE_CATALOG:
        parsed = urlparse(item.source_note)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"Node {item.slug} must have an HTTPS primary source")
        if parsed.hostname in {"example.com", "timsun.net"}:
            raise ValueError(f"Node {item.slug} cannot cite a demo or comparison site")
        if len(item.source_note) > 240:
            raise ValueError(f"Node {item.slug} source URL exceeds the model field")


@transaction.atomic
def sync_ai_supply_chain_catalog() -> dict[str, int]:
    """Upsert reviewed nodes and erase unsupported demo-derived numeric fields."""

    validate_ai_supply_chain_catalog()
    created = 0
    updated = 0
    for item in AI_SUPPLY_CHAIN_NODE_CATALOG:
        _, was_created = SupplyChainNode.objects.update_or_create(
            slug=item.slug,
            defaults={
                "name": item.name,
                "layer": item.layer,
                "description": item.description,
                "thesis": "",
                "quadrant": "资料目录",
                "narrative_score": Decimal("0"),
                "revenue_growth": None,
                "gross_margin": None,
                "median_pe": None,
                "median_ps": None,
                "market_cap_usd_m": None,
                "source_note": item.source_note,
            },
        )
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "updated": updated, "total": len(AI_SUPPLY_CHAIN_NODE_CATALOG)}


validate_ai_supply_chain_catalog()
