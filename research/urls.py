from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("trade-map/", views.dashboard_page, {"page_key": "trade-map"}, name="trade-map"),
    path("regime-log/", views.regime_log, name="regime-log"),
    path("daily-report/", views.daily_list, name="daily-list"),
    path("daily-report/<slug:report_date>/", views.daily_detail, name="daily-detail"),
    path("assets/", views.assets_overview, name="assets-overview"),
    path(
        "assets/equities/", views.dashboard_page, {"page_key": "assets-equities"}, name="equities"
    ),
    path("assets/etfs/", views.dashboard_page, {"page_key": "assets-etfs"}, name="etfs"),
    path("assets/equities/options/", views.options_view, name="options"),
    path("assets/equities/positioning/", views.positioning_view, name="positioning"),
    path("assets/bonds/", views.dashboard_page, {"page_key": "assets-bonds"}, name="bonds"),
    path(
        "assets/commodities/",
        views.dashboard_page,
        {"page_key": "assets-commodities"},
        name="commodities",
    ),
    path("assets/fx/", views.dashboard_page, {"page_key": "assets-fx"}, name="fx"),
    path("assets/crypto/", views.dashboard_page, {"page_key": "assets-crypto"}, name="crypto"),
    path("assets/crypto/derivatives/", views.crypto_derivatives, name="crypto-derivatives"),
    path("rates/", views.dashboard_page, {"page_key": "rates"}, name="rates-overview"),
    path("rates/fed-funds/", views.dashboard_page, {"page_key": "fed-funds"}, name="fed-funds"),
    path(
        "rates/yield-curve/", views.dashboard_page, {"page_key": "yield-curve"}, name="yield-curve"
    ),
    path("rates/auctions/", views.dashboard_page, {"page_key": "auctions"}, name="auctions"),
    path("rates/real-rates/", views.dashboard_page, {"page_key": "real-rates"}, name="real-rates"),
    path(
        "rates/expectations/",
        views.dashboard_page,
        {"page_key": "expectations"},
        name="expectations",
    ),
    path("fed/", views.fed_hub, name="fed-hub"),
    path("fed/statements/", views.fed_list, {"doc_type": "statement"}, name="fed-statements"),
    path(
        "fed/statements/<slug:slug>/",
        views.fed_detail,
        {"doc_type": "statement"},
        name="fed-detail",
    ),
    path("fed/speeches/", views.fed_list, {"doc_type": "speech"}, name="fed-speeches"),
    path(
        "fed/speeches/<slug:slug>/",
        views.fed_detail,
        {"doc_type": "speech"},
        name="fed-speech-detail",
    ),
    path("fed/news/", views.fed_list, {"doc_type": "news"}, name="fed-news"),
    path("fed/news/<slug:slug>/", views.fed_detail, {"doc_type": "news"}, name="fed-news-detail"),
    path(
        "fed/hawkish-dovish/",
        views.dashboard_page,
        {"page_key": "fed-hawkish-dovish"},
        name="hawkish-dovish",
    ),
    path("liquidity/", views.dashboard_page, {"page_key": "liquidity"}, name="liquidity-overview"),
    path(
        "liquidity/transmission-chain/",
        views.dashboard_page,
        {"page_key": "transmission-chain"},
        name="transmission-chain",
    ),
    path(
        "liquidity/fed-balance-sheet/",
        views.dashboard_page,
        {"page_key": "fed-balance-sheet"},
        name="fed-balance-sheet",
    ),
    path(
        "liquidity/operations/", views.dashboard_page, {"page_key": "operations"}, name="operations"
    ),
    path("liquidity/rrp-tga/", views.dashboard_page, {"page_key": "rrp-tga"}, name="rrp-tga"),
    path("liquidity/reserves/", views.dashboard_page, {"page_key": "reserves"}, name="reserves"),
    path(
        "liquidity/global-dollar/",
        views.dashboard_page,
        {"page_key": "global-dollar"},
        name="global-dollar",
    ),
    path(
        "liquidity/subsurface/", views.dashboard_page, {"page_key": "subsurface"}, name="subsurface"
    ),
    path("economy/", views.dashboard_page, {"page_key": "economy"}, name="economy-overview"),
    path("economy/gdp/", views.dashboard_page, {"page_key": "gdp"}, name="gdp"),
    path(
        "economy/employment/", views.dashboard_page, {"page_key": "employment"}, name="employment"
    ),
    path("economy/inflation/", views.dashboard_page, {"page_key": "inflation"}, name="inflation"),
    path("economy/consumer/", views.dashboard_page, {"page_key": "consumer"}, name="consumer"),
    path(
        "volatility/", views.dashboard_page, {"page_key": "volatility"}, name="volatility-overview"
    ),
    path(
        "volatility/dashboard/",
        views.dashboard_page,
        {"page_key": "volatility-dashboard"},
        name="volatility-dashboard",
    ),
    path("volatility/vix/", views.dashboard_page, {"page_key": "vix"}, name="vix"),
    path(
        "volatility/move/",
        views.dashboard_page,
        {"page_key": "volatility-move"},
        name="volatility-move",
    ),
    path("volatility/fx-vol/", views.dashboard_page, {"page_key": "fx-vol"}, name="fx-vol"),
    path(
        "volatility/implied-vs-realized/",
        views.dashboard_page,
        {"page_key": "implied-vs-realized"},
        name="implied-vs-realized",
    ),
    path("credit/", views.dashboard_page, {"page_key": "credit"}, name="credit-overview"),
    path(
        "credit/spreads/",
        views.dashboard_page,
        {"page_key": "credit-spreads"},
        name="credit-spreads",
    ),
    path("credit/cds/", views.dashboard_page, {"page_key": "credit-cds"}, name="credit-cds"),
    path(
        "credit/stress/", views.dashboard_page, {"page_key": "credit-stress"}, name="credit-stress"
    ),
    path(
        "credit/issuance/",
        views.gone,
        {"reason": "一级信用发行模块已重构下线；取得可靠发行数据许可后再启用。"},
        name="credit-issuance",
    ),
    path(
        "credit/events/",
        views.gone,
        {"reason": "信用事件页因缺少结构化评级授权数据而下线。"},
        name="credit-events",
    ),
    path("news/", views.news_list, name="news"),
    path(
        "semiconductor-news/",
        views.news_list,
        {"semiconductor_only": True},
        name="semiconductor-news",
    ),
    path("research/reports/", views.reports, name="reports"),
    path("research/reports/all/", views.reports, {"all_reports": True}, name="reports-all"),
    path("research/fund-letters/", views.fund_letters, name="fund-letters"),
    path("research/fund-letters/<int:pk>/", views.fund_letter_detail, name="fund-letter-detail"),
    path("glossary/", views.glossary, name="glossary"),
    path("supply-chain/", views.dashboard_page, {"page_key": "supply-chain"}, name="supply-chain"),
    path(
        "supply-chain/foundry/",
        views.dashboard_page,
        {"page_key": "supply-chain-foundry"},
        name="supply-chain-foundry",
    ),
    path(
        "supply-chain/packaging/",
        views.dashboard_page,
        {"page_key": "supply-chain-packaging"},
        name="supply-chain-packaging",
    ),
    path(
        "supply-chain/hbm/",
        views.dashboard_page,
        {"page_key": "supply-chain-hbm"},
        name="supply-chain-hbm",
    ),
    path(
        "supply-chain/gpu/",
        views.dashboard_page,
        {"page_key": "supply-chain-gpu"},
        name="supply-chain-gpu",
    ),
    path(
        "supply-chain/demand/",
        views.dashboard_page,
        {"page_key": "supply-chain-demand"},
        name="supply-chain-demand",
    ),
    path("search/", views.search, name="search"),
    path("data-sources/", views.data_sources, name="data-sources"),
    path("ai-industry/", views.ai_hub, name="ai-hub"),
    path("ai-industry/market-map/", views.ai_market_map, name="ai-market-map"),
    path("ai-industry/graph/", views.ai_graph, name="ai-graph"),
    path("ai-industry/news/", views.news_list, {"ai_only": True}, name="ai-news"),
    path("ai-industry/chain/", views.ai_hub, {"chain_mode": True}, name="ai-chain"),
    path(
        "ai-industry/chain/semiconductor-manufacturing/",
        views.ai_hub,
        {"chain_mode": True},
        name="semiconductor-chain",
    ),
    path(
        "ai-industry/chain/semiconductor-manufacturing/<slug:slug>/", views.ai_node, name="ai-node"
    ),
    path("ai-industry/company/<slug:slug>/", views.ai_company, name="ai-company"),
    path("ai-industry/chain/model-evolution/", views.model_evolution, name="model-evolution"),
    path(
        "ai-industry/chain/model-evolution/model/<slug:slug>/",
        views.model_detail,
        name="model-detail",
    ),
    path(
        "ai-industry/vibe-coding/",
        views.coding_agents,
        name="coding-agents",
    ),
    path(
        "ai-industry/vibe-coding/<slug:slug>/",
        views.coding_agent_detail,
        name="coding-agent-detail",
    ),
    path("ai-industry/chain/applications/", views.applications, name="applications"),
    path("ai-industry/chain/glossary/", views.glossary, {"ai_only": True}, name="ai-glossary"),
    path(
        "ai-industry/chain/glossary/<slug:slug>/",
        views.ai_glossary_detail,
        name="ai-glossary-detail",
    ),
    path("ai-industry/chain/teardown/", views.ai_teardown, name="ai-teardown"),
    path("llms.txt", views.llms_txt, name="llms"),
    path("robots.txt", views.robots_txt, name="robots"),
    path("sitemap.xml", views.sitemap_xml, name="sitemap"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="service-worker"),
    path("offline/", views.offline, name="offline"),
    path("healthz/", views.health, name="health"),
]
