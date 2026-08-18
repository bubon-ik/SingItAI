#!/usr/bin/env python3
"""Audit the x402 ecosystem and produce a shortlist of services worth wiring up.

Primary source is x402-list.com, an independently monitored directory. Unlike
the raw CDP facilitator catalog (14k+ listings, mostly bulk-registered spam),
this one publishes per-service uptime, a compliance grade, a risk level, and
measured on-chain traction including the top-buyer concentration share.

The filter below keeps services that are online, verified, reliable, and paid
for by more than one real buyer.

Usage:
    python3 scripts/x402-bazaar-audit.py
    python3 scripts/x402-bazaar-audit.py --min-buyers 3 --min-uptime 95
    python3 scripts/x402-bazaar-audit.py --category Data --out-dir ./out

No dependencies beyond the standard library.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SERVICES_URL = "https://x402-list.com/api/v1/services"
PAGE_SIZE = 100  # API maximum
REQUEST_TIMEOUT = 45
RETRIES = 3


def fetch_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    full_url = f"{url}?{query}" if query else url
    request = urllib.request.Request(
        full_url,
        headers={"Accept": "application/json", "User-Agent": "sign402-audit/1.0"},
    )

    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(1.5 * (attempt + 1))
            continue
        return payload if isinstance(payload, dict) else {"data": payload}

    raise RuntimeError(f"Failed to fetch {full_url}: {last_error}")


def fetch_services(url: str, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """Page through /api/v1/services.

    The API paginates with `page` and `per_page` (max 100, default 25) and
    reports `meta.total_pages`. Passing `limit` is silently ignored, which is
    why a naive call only ever returns the first 25 services.
    """
    services: list[dict[str, Any]] = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        payload = fetch_json(url, {"page": page, "per_page": page_size})
        batch = payload.get("data")
        if not isinstance(batch, list) or not batch:
            break
        services.extend(batch)

        meta = payload.get("meta") or {}
        try:
            total_pages = int(meta.get("total_pages", page))
        except (TypeError, ValueError):
            total_pages = page

        print(
            f"  page {page}/{total_pages} — {len(services)} services so far",
            file=sys.stderr,
        )
        page += 1
        if page <= total_pages:
            time.sleep(0.4)  # stay well under the 200 req/min limit

    return services


def flatten(service: dict[str, Any]) -> dict[str, Any]:
    assessment = service.get("assessment") or {}
    traction = assessment.get("traction") or {}
    return {
        "name": service.get("name", ""),
        "slug": service.get("slug", ""),
        "category": service.get("category", ""),
        "status": service.get("status", ""),
        "verified": bool(service.get("verified")),
        "endpoints": service.get("endpoint_count", 0) or 0,
        "min_price_usd": service.get("min_price_usd"),
        "networks": ",".join(service.get("networks") or []),
        "uptime_24h": service.get("uptime_24h"),
        "uptime_30d": assessment.get("reliability_uptime_30d"),
        "p95_ms": assessment.get("response_p95_ms"),
        "grade": assessment.get("compliance_grade", ""),
        "risk": assessment.get("risk_level", ""),
        "buyers_30d": traction.get("unique_buyers_30d") or 0,
        "volume_usd_30d": traction.get("volume_usd_30d") or 0,
        "tx_30d": traction.get("tx_count_30d") or 0,
        "top_buyer_share": traction.get("top_buyer_share_30d"),
        "median_settlement_usd": traction.get("median_settlement_usd_30d"),
        "swarm_cluster": 0,
        "base_url": service.get("base_url", ""),
        "website": service.get("website_url", ""),
        "description": " ".join(str(service.get("description", "")).split())[:240],
    }


def distributed_volume(row: dict[str, Any]) -> float:
    """Volume excluding the largest single buyer.

    One buyer is one relationship, not a market. BlockRun books $160k/30d with
    a 98.7% top-buyer share; stripped of that buyer it is worth ~$2k.
    """
    volume = float(row.get("volume_usd_30d") or 0)
    share = row.get("top_buyer_share")
    if share is None:
        return volume
    return volume * (1.0 - float(share))


def usd_per_buyer(row: dict[str, Any]) -> float:
    buyers = int(row.get("buyers_30d") or 0)
    if buyers <= 0:
        return 0.0
    return float(row.get("volume_usd_30d") or 0) / buyers


def mark_swarms(rows: list[dict[str, Any]], min_cluster: int = 3) -> None:
    """Flag services that share an identical (buyers, volume) fingerprint.

    Eight unrelated services reporting exactly 442 buyers and exactly $6 are
    not eight markets. They are one wallet swarm sweeping a list, which inflates
    unique-buyer counts without representing demand.
    """
    fingerprints: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            int(row.get("buyers_30d") or 0),
            f"{float(row.get('volume_usd_30d') or 0):.2f}",
        )
        if key[0] == 0:
            continue
        fingerprints.setdefault(key, []).append(row)

    for group in fingerprints.values():
        is_swarm = len(group) >= min_cluster
        for row in group:
            row["swarm_cluster"] = len(group) if is_swarm else 0


def passes(row: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.require_online and row["status"] != "online":
        return False
    if args.require_verified and not row["verified"]:
        return False
    if row["risk"] and row["risk"] not in {"clean", "low"}:
        return False

    uptime = row["uptime_30d"]
    if uptime is None:
        uptime = row["uptime_24h"]
    if uptime is None or float(uptime) < args.min_uptime:
        return False

    if int(row["buyers_30d"]) < args.min_buyers:
        return False

    # A service whose whole volume comes from one wallet is the author paying
    # themselves. Drop it unless the caller explicitly allows concentration.
    share = row["top_buyer_share"]
    if share is not None and float(share) > args.max_top_buyer_share:
        return False

    if args.category and row["category"].lower() != args.category.lower():
        return False

    # Real money, not dust.
    if distributed_volume(row) < args.min_volume:
        return False

    # Farmed traffic books hundreds of buyers against a few dollars. Real usage
    # is measured in dollars per buyer, not in buyer headcount.
    if usd_per_buyer(row) < args.min_usd_per_buyer:
        return False

    if not args.keep_swarms and int(row.get("swarm_cluster") or 0) >= 3:
        return False

    return True


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        print(f"nothing to write to {path.name}", file=sys.stderr)
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    lines = [
        "# x402 services worth wiring up",
        "",
        f"Source: {SERVICES_URL} · generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
        f"Filter: online, verified, uptime 30d >= {args.min_uptime}%, "
        f">= {args.min_buyers} unique buyers, "
        f"top-buyer share <= {args.max_top_buyer_share:.0%}, "
        f"net volume >= ${args.min_volume:,.0f}, "
        f">= ${args.min_usd_per_buyer:.2f} per buyer, wallet swarms dropped.",
        "",
        "Net volume excludes the largest single buyer. Sorted by it.",
        "",
        "Data: x402-list.com (CC BY 4.0)",
        "",
        "| Service | Category | Net $30d | Gross $30d | Buyers | $/buyer | Uptime | From | Networks | What it does |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        price = f"${row['min_price_usd']}" if row["min_price_usd"] is not None else "—"
        uptime = row["uptime_30d"] if row["uptime_30d"] is not None else row["uptime_24h"]
        description = row["description"].replace("|", "/")[:160]
        website = row["website"] or row["base_url"]
        name = f"[{row['name']}]({website})" if website else row["name"]
        lines.append(
            f"| {name} | {row['category']} "
            f"| ${float(row['distributed_volume_usd']):,.0f} "
            f"| ${float(row['volume_usd_30d']):,.0f} "
            f"| {row['buyers_30d']} | ${float(row['usd_per_buyer']):.2f} "
            f"| {uptime}% | {price} | {row['networks']} | {description} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=SERVICES_URL)
    parser.add_argument("--min-buyers", type=int, default=2)
    parser.add_argument("--min-uptime", type=float, default=90.0)
    parser.add_argument("--max-top-buyer-share", type=float, default=0.9)
    parser.add_argument("--category", default="")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument(
        "--min-volume",
        type=float,
        default=50.0,
        help="Minimum 30d USDC volume excluding the largest buyer (default: 50)",
    )
    parser.add_argument(
        "--min-usd-per-buyer",
        type=float,
        default=0.10,
        help="Minimum 30d volume per unique buyer; filters farmed traffic (default: 0.10)",
    )
    parser.add_argument(
        "--keep-swarms",
        action="store_true",
        help="Keep services sharing an identical buyer/volume fingerprint",
    )
    parser.add_argument("--require-online", action="store_true", default=True)
    parser.add_argument("--require-verified", action="store_true", default=True)
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    services = fetch_services(args.url, args.page_size)
    print(f"fetched {len(services)} services", file=sys.stderr)

    rows = [flatten(service) for service in services]
    mark_swarms(rows)
    for row in rows:
        row["distributed_volume_usd"] = round(distributed_volume(row), 2)
        row["usd_per_buyer"] = round(usd_per_buyer(row), 4)
    write_csv(out_dir / "x402-services-all.csv", rows)

    swarmed = sum(1 for row in rows if int(row.get("swarm_cluster") or 0) >= 3)
    if swarmed:
        print(
            f"{swarmed} services share a buyer/volume fingerprint with others "
            f"(likely wallet swarms)",
            file=sys.stderr,
        )

    live = [row for row in rows if passes(row, args)]
    live.sort(key=lambda row: float(row["distributed_volume_usd"]), reverse=True)
    print(
        f"{len(live)} of {len(rows)} services pass the filter "
        f"({len(live) / max(len(rows), 1):.1%})",
        file=sys.stderr,
    )

    write_csv(out_dir / "x402-services-live.csv", live)
    write_markdown(out_dir / "x402-services-live.md", live, args)

    print()
    print(
        f"{'net $30d':>10} {'gross':>10} {'buyers':>7} {'$/buyer':>9}  "
        f"{'category':<12} service"
    )
    for row in live[:30]:
        print(
            f"${float(row['distributed_volume_usd']):>9,.0f} "
            f"${float(row['volume_usd_30d']):>9,.0f} "
            f"{int(row['buyers_30d']):>7} "
            f"${float(row['usd_per_buyer']):>8.2f}  "
            f"{str(row['category']):<12} {row['name']}"
        )


if __name__ == "__main__":
    main()
