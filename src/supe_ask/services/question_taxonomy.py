from __future__ import annotations

import re
from typing import Any


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", text.lower()) if len(token) > 1}


QUESTION_TAXONOMY: list[dict[str, Any]] = [
    {
        "cluster": "revenue_billing",
        "title": "Revenue & Billing Performance",
        "intent": "Track topline movement, revenue shortfalls, and billing concentration.",
        "entities": ["salesman", "retailer", "beat", "distributor", "geography", "sku"],
        "metrics": ["revenue", "billing", "orders", "aov", "growth"],
        "time_grains": ["today", "mtd", "weekly", "monthly"],
        "keywords": ["revenue", "billing", "sales", "topline", "gmv", "invoice", "orders", "growth"],
        "canonical_questions": [
            "What is my total secondary revenue MTD?",
            "Which regions are behind billing this month?",
            "Where did revenue drop versus last month?",
        ],
    },
    {
        "cluster": "collections_outstanding",
        "title": "Collection & Outstanding",
        "intent": "Monitor collections health, delayed payments, and outstanding risk.",
        "entities": ["retailer", "salesman", "distributor", "geography"],
        "metrics": ["collection", "outstanding", "overdue", "recovery"],
        "time_grains": ["mtd", "weekly", "monthly"],
        "keywords": ["collection", "outstanding", "overdue", "dues", "recovery", "payment"],
        "canonical_questions": [
            "How much outstanding do we have right now?",
            "Which retailers are overdue beyond 30 days?",
            "Which distributors are blocking collections?",
        ],
    },
    {
        "cluster": "coverage_execution",
        "title": "Coverage & Visit Execution",
        "intent": "Measure route execution, visit productivity, and billed outlet coverage.",
        "entities": ["salesman", "beat", "retailer", "geography"],
        "metrics": ["coverage", "visits", "productive_calls", "beat_adherence"],
        "time_grains": ["today", "mtd", "weekly"],
        "keywords": ["coverage", "visit", "execution", "calls", "beat", "adherence", "productive"],
        "canonical_questions": [
            "Which salesmen have coverage below 70%?",
            "Which beats are under-executed this week?",
            "Where are billed calls lagging plan?",
        ],
    },
    {
        "cluster": "salesman_productivity",
        "title": "Salesman Performance & Productivity",
        "intent": "Compare seller productivity, effectiveness, and output per route.",
        "entities": ["salesman", "beat", "geography"],
        "metrics": ["revenue", "orders", "coverage", "productivity", "conversion"],
        "time_grains": ["mtd", "weekly", "monthly"],
        "keywords": ["salesman", "rep", "productivity", "performance", "conversion", "efficiency"],
        "canonical_questions": [
            "Tell me about Rajesh Kumar.",
            "Who are my top and bottom salesmen this month?",
            "Which salesmen need intervention first?",
        ],
    },
    {
        "cluster": "retailer_health",
        "title": "Retailer Behavior & Health",
        "intent": "Understand retailer dormancy, ordering rhythm, wallet share, and risk.",
        "entities": ["retailer", "salesman", "beat", "geography"],
        "metrics": ["aov", "frequency", "dormancy", "outstanding", "growth"],
        "time_grains": ["mtd", "monthly", "quarterly"],
        "keywords": ["retailer", "shop", "store", "dormant", "frequency", "aov", "health"],
        "canonical_questions": [
            "Which retailers are becoming dormant?",
            "Which stores reduced order frequency this month?",
            "Which retailers should be reactivated first?",
        ],
    },
    {
        "cluster": "sku_performance",
        "title": "SKU & Product Performance",
        "intent": "Track SKU winners, decliners, penetration, and mix contribution.",
        "entities": ["sku", "brand", "retailer", "geography", "distributor"],
        "metrics": ["revenue", "units", "growth", "penetration", "mix"],
        "time_grains": ["mtd", "monthly", "quarterly"],
        "keywords": ["sku", "product", "brand", "mix", "penetration", "units", "declining"],
        "canonical_questions": [
            "Which SKUs are declining this month?",
            "Which products are driving growth?",
            "Which SKUs have low penetration but high velocity?",
        ],
    },
    {
        "cluster": "distributor_health",
        "title": "Distributor Performance & Health",
        "intent": "Evaluate distributor sales, damage, fill-rate, and outlet activation.",
        "entities": ["distributor", "salesman", "geography"],
        "metrics": ["revenue", "damage", "fulfilment", "outstanding", "active_outlets"],
        "time_grains": ["mtd", "monthly"],
        "keywords": ["distributor", "damage", "fulfilment", "fill", "stockist", "active outlets"],
        "canonical_questions": [
            "Which distributors have damage above 2%?",
            "Which distributors are underperforming versus average?",
            "Where is distributor health deteriorating?",
        ],
    },
    {
        "cluster": "beat_territory",
        "title": "Beat & Territory Management",
        "intent": "Inspect route balance, territory quality, and beat-level execution.",
        "entities": ["beat", "salesman", "geography"],
        "metrics": ["revenue", "coverage", "realization", "ebv", "visits"],
        "time_grains": ["mtd", "weekly", "monthly"],
        "keywords": ["beat", "territory", "route", "realization", "ebv", "coverage"],
        "canonical_questions": [
            "Which beats are lagging revenue this month?",
            "Where is territory coverage uneven?",
            "Which routes need rebucketing?",
        ],
    },
    {
        "cluster": "target_achievement",
        "title": "Target Setting & Achievement",
        "intent": "Measure attainment, gap to target, and pace-to-go.",
        "entities": ["salesman", "beat", "distributor", "geography"],
        "metrics": ["target", "attainment", "gap", "pace", "run_rate"],
        "time_grains": ["mtd", "monthly", "quarterly"],
        "keywords": ["target", "achievement", "attainment", "pace", "gap", "run rate"],
        "canonical_questions": [
            "Who is off-track versus target?",
            "What run rate do we need to close the month?",
            "Which teams are ahead of plan?",
        ],
    },
    {
        "cluster": "growth_expansion",
        "title": "Growth & Expansion",
        "intent": "Find whitespace, expansion opportunities, and growth pockets.",
        "entities": ["geography", "retailer", "sku", "distributor"],
        "metrics": ["growth", "new_outlets", "penetration", "whitespace"],
        "time_grains": ["mtd", "monthly", "quarterly"],
        "keywords": ["growth", "expand", "expansion", "whitespace", "new outlets", "headroom"],
        "canonical_questions": [
            "Where are we leaving money on the table?",
            "Which regions have the most whitespace?",
            "Where should we expand next?",
        ],
    },
    {
        "cluster": "new_launch",
        "title": "New Launch Performance",
        "intent": "Evaluate launch velocity, distribution ramp-up, and early traction.",
        "entities": ["sku", "retailer", "geography", "distributor"],
        "metrics": ["launch_revenue", "launch_units", "distribution", "trial"],
        "time_grains": ["weekly", "monthly"],
        "keywords": ["launch", "new sku", "new product", "trial", "ramp", "distribution"],
        "canonical_questions": [
            "How is the new launch performing?",
            "Which markets adopted the launch fastest?",
            "Where is launch distribution lagging?",
        ],
    },
    {
        "cluster": "scheme_promo",
        "title": "Scheme & Promotion Effectiveness",
        "intent": "Measure promo lift, ROI, and post-scheme stickiness.",
        "entities": ["retailer", "sku", "geography", "salesman"],
        "metrics": ["lift", "roi", "uplift", "repeat_rate", "scheme_cost"],
        "time_grains": ["campaign", "mtd", "monthly"],
        "keywords": ["scheme", "promotion", "promo", "offer", "discount", "uplift", "roi"],
        "canonical_questions": [
            "What scheme should I run for dormant retailers?",
            "Which promotions actually lifted sales?",
            "Where did scheme ROI fall short?",
        ],
    },
    {
        "cluster": "inventory_supply",
        "title": "Inventory & Supply Chain",
        "intent": "Spot stock gaps, service failures, and supply-side blockers.",
        "entities": ["sku", "distributor", "geography"],
        "metrics": ["stockout", "fill_rate", "inventory_days", "service_level"],
        "time_grains": ["daily", "weekly", "mtd"],
        "keywords": ["inventory", "stock", "stockout", "supply", "fill rate", "service level"],
        "canonical_questions": [
            "Which SKUs are stock constrained?",
            "Where are service levels slipping?",
            "Which distributors are blocking availability?",
        ],
    },
    {
        "cluster": "competitive_intel",
        "title": "Competitive Intelligence",
        "intent": "Frame share threats, competitor wins, and price or assortment pressure.",
        "entities": ["sku", "retailer", "geography"],
        "metrics": ["share", "competitive_loss", "price_gap"],
        "time_grains": ["monthly", "quarterly"],
        "keywords": ["competitor", "share", "competition", "price gap", "lost share"],
        "canonical_questions": [
            "Where are we losing share?",
            "Which markets are exposed to competition?",
            "Which products are losing against rivals?",
        ],
    },
    {
        "cluster": "seasonal_temporal",
        "title": "Seasonal & Temporal Patterns",
        "intent": "Understand day/week/month effects, festival shifts, and timing patterns.",
        "entities": ["geography", "sku", "retailer"],
        "metrics": ["seasonality", "weekday_mix", "festival_uplift"],
        "time_grains": ["daily", "weekly", "monthly"],
        "keywords": ["seasonal", "festival", "weekday", "weekend", "temporal", "timing"],
        "canonical_questions": [
            "Which categories are seasonal right now?",
            "How is demand shifting week to week?",
            "What changed after the festival period?",
        ],
    },
    {
        "cluster": "operational_efficiency",
        "title": "Operational Efficiency",
        "intent": "Identify wasted effort, poor route economics, and low-yield activity.",
        "entities": ["salesman", "beat", "distributor", "geography"],
        "metrics": ["productivity", "efficiency", "yield", "cost_to_serve"],
        "time_grains": ["mtd", "weekly", "monthly"],
        "keywords": ["efficiency", "yield", "productivity", "waste", "cost to serve", "route economics"],
        "canonical_questions": [
            "Which routes are inefficient?",
            "Where are we spending effort without output?",
            "Which teams have weak productivity?",
        ],
    },
    {
        "cluster": "strategic_what_if",
        "title": "Strategic & What-If Questions",
        "intent": "Support scenario analysis, intervention simulation, and prioritization.",
        "entities": ["salesman", "geography", "retailer", "sku"],
        "metrics": ["incremental_revenue", "coverage_lift", "roi", "scenario_impact"],
        "time_grains": ["mtd", "monthly", "quarterly"],
        "keywords": ["what if", "scenario", "simulate", "impact", "intervention", "add", "remove"],
        "canonical_questions": [
            "What if I add 3 salesmen in Maharashtra?",
            "What intervention gives the highest upside?",
            "If coverage improves, what revenue lift can we expect?",
        ],
    },
    {
        "cluster": "cross_entity",
        "title": "Cross-Entity & System-Level Questions",
        "intent": "Connect entities to expose structural drivers and system-level bottlenecks.",
        "entities": ["salesman", "retailer", "sku", "distributor", "beat", "geography"],
        "metrics": ["cross_entity_correlation", "dependency", "system_gap"],
        "time_grains": ["mtd", "monthly"],
        "keywords": ["cross", "correlation", "system", "driver", "linked", "depends"],
        "canonical_questions": [
            "How does North compare with South?",
            "What is driving weak retailer health?",
            "Which upstream issues are causing sales losses?",
        ],
    },
    {
        "cluster": "people_org_health",
        "title": "People, Org Health & Team Structure",
        "intent": "Monitor span, manager quality, hiring needs, and org stress points.",
        "entities": ["salesman", "manager", "beat", "geography"],
        "metrics": ["span", "vacancy", "productivity", "attrition_risk"],
        "time_grains": ["monthly", "quarterly"],
        "keywords": ["team", "org", "manager", "hiring", "vacancy", "span", "people"],
        "canonical_questions": [
            "Which territories are under-resourced?",
            "Where do we need more people?",
            "Which managers have overloaded teams?",
        ],
    },
    {
        "cluster": "channel_specific",
        "title": "Channel-Specific Questions",
        "intent": "Separate GT, MT, and e-commerce behavior to avoid blended conclusions.",
        "entities": ["channel", "retailer", "sku", "geography", "distributor"],
        "metrics": ["channel_growth", "channel_mix", "channel_penetration"],
        "time_grains": ["mtd", "monthly", "quarterly"],
        "keywords": ["channel", "gt", "mt", "ecommerce", "modern trade", "general trade"],
        "canonical_questions": [
            "How are GT and MT behaving differently?",
            "Which products win online but lag offline?",
            "Where is channel mix shifting fastest?",
        ],
    },
]


def taxonomy_prompt_summary() -> str:
    lines = [
        f"- {cluster['title']}: {cluster['intent']} Key entities: {', '.join(cluster['entities'][:4])}. Key metrics: {', '.join(cluster['metrics'][:4])}."
        for cluster in QUESTION_TAXONOMY
    ]
    return "\n".join(lines)


def relevant_taxonomy_context(question: str, limit: int = 3) -> list[dict[str, Any]]:
    question_tokens = _tokenize(question)
    scored: list[tuple[int, dict[str, Any]]] = []
    for cluster in QUESTION_TAXONOMY:
        score = 0
        cluster_tokens = set(cluster["keywords"]) | _tokenize(cluster["title"]) | _tokenize(cluster["intent"])
        score += len(question_tokens & cluster_tokens) * 3
        for canonical in cluster["canonical_questions"]:
            canonical_tokens = _tokenize(canonical)
            score += len(question_tokens & canonical_tokens) * 2
        if score <= 0:
            continue
        scored.append((score, cluster))

    top_clusters = [cluster for _, cluster in sorted(scored, key=lambda item: (-item[0], item[1]["title"]))[:limit]]
    if top_clusters:
        return [
            {
                "cluster": cluster["cluster"],
                "title": cluster["title"],
                "intent": cluster["intent"],
                "entities": cluster["entities"],
                "metrics": cluster["metrics"],
                "time_grains": cluster["time_grains"],
                "canonical_questions": cluster["canonical_questions"][:3],
            }
            for cluster in top_clusters
        ]

    return [
        {
            "cluster": cluster["cluster"],
            "title": cluster["title"],
            "intent": cluster["intent"],
            "entities": cluster["entities"],
            "metrics": cluster["metrics"],
            "time_grains": cluster["time_grains"],
            "canonical_questions": cluster["canonical_questions"][:2],
        }
        for cluster in QUESTION_TAXONOMY[:2]
    ]
