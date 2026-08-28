#!/usr/bin/env python3
"""Yad2 poller: detect new listings and notify Telegram."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests

YAD2_URL = (
    "https://gw.yad2.co.il/recommendations/items/vehicles"
    "?count=20&type=home&categoryId=1&subCategoriesIds=21"
)
ITEM_URL = "https://www.yad2.co.il/vehicles/item/{token}"
STATE_PATH = Path(__file__).resolve().parent / "sent_ads.json"
REQUEST_TIMEOUT = 30

YAD2_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Origin": "https://www.yad2.co.il",
    "Referer": "https://www.yad2.co.il/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

ID_KEYS = ("token", "id", "listingId", "adNumber")
CONTAINER_KEYS = (
    "data",
    "feed",
    "items",
    "feed_items",
    "private",
    "commercial",
    "listings",
    "results",
    "ads",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("yad2_bot")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"ids": []}
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s (%s); starting empty", STATE_PATH, exc)
        return {"ids": []}
    ids = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return {"ids": []}
    return {"ids": [str(item) for item in ids]}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def listing_id(ad: dict[str, Any]) -> str | None:
    for key in ID_KEYS:
        value = ad.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _looks_like_ad(obj: Any) -> bool:
    return isinstance(obj, dict) and listing_id(obj) is not None


def collect_listings(node: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            collect_listings(item, found)
        return
    if not isinstance(node, dict):
        return
    if _looks_like_ad(node):
        found.append(node)
        return
    for key in CONTAINER_KEYS:
        if key in node:
            collect_listings(node[key], found)
    if not any(key in node for key in CONTAINER_KEYS):
        for value in node.values():
            if isinstance(value, (dict, list)):
                collect_listings(value, found)


def fetch_listings() -> list[dict[str, Any]]:
    headers = dict(YAD2_HEADERS)
    cookie = os.environ.get("YAD2_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie

    try:
        response = requests.get(YAD2_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Yad2 request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Yad2 returned HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Yad2 response is not JSON") from exc

    listings: list[dict[str, Any]] = []
    collect_listings(payload, listings)

    unique: dict[str, dict[str, Any]] = {}
    for ad in listings:
        ad_id = listing_id(ad)
        if ad_id:
            unique[ad_id] = ad

    if not unique:
        top_keys = list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
        raise RuntimeError(
            f"No listings parsed from Yad2 payload. Top-level keys: {top_keys}"
        )

    log.info("Fetched %s listings from Yad2", len(unique))
    return list(unique.values())


def filter_new(ads: list[dict[str, Any]], seen: set[str]) -> list[dict[str, Any]]:
    new_ads: list[dict[str, Any]] = []
    for ad in ads:
        ad_id = listing_id(ad)
        if ad_id and ad_id not in seen:
            new_ads.append(ad)
    return new_ads


def _nested_text(ad: dict[str, Any], *path: str) -> str | None:
    node: Any = ad
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if node is None or isinstance(node, (dict, list)):
        return None
    text = str(node).strip()
    return text or None


def format_message(ad: dict[str, Any]) -> str:
    ad_id = listing_id(ad) or ""
    token = str(ad.get("token") or ad_id)
    url = ITEM_URL.format(token=token) if token else ""

    manufacturer = (
        _nested_text(ad, "manufacturer", "text")
        or _nested_text(ad, "manufacturer")
        or _nested_text(ad, "manufacturerEn")
        or ""
    )
    model = (
        _nested_text(ad, "model", "text")
        or _nested_text(ad, "model")
        or _nested_text(ad, "modelEn")
        or ""
    )
    year = _nested_text(ad, "year") or _nested_text(ad, "vehicleDates", "year") or ""
    title = " ".join(part for part in (manufacturer, model, year) if part) or "מודעה חדשה"

    price = ad.get("price")
    if isinstance(price, dict):
        price = price.get("value") or price.get("text") or price.get("amount")
    price_line = f"מחיר: {price}" if price not in (None, "") else ""

    city = (
        _nested_text(ad, "address", "city", "text")
        or _nested_text(ad, "city", "text")
        or _nested_text(ad, "city")
        or _nested_text(ad, "cityEn")
        or ""
    )
    city_line = f"עיר: {city}" if city else ""

    km = ad.get("km") or _nested_text(ad, "km")
    hand = ad.get("hand") or _nested_text(ad, "hand")
    extras = []
    if km not in (None, ""):
        extras.append(f"ק״מ: {km}")
    if hand not in (None, ""):
        extras.append(f"יד: {hand}")

    lines = [title]
    for line in (price_line, city_line, " · ".join(extras), url):
        if line:
            lines.append(line)
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram returned HTTP {response.status_code}: {response.text[:300]}"
        )


def main() -> int:
    try:
        ads = fetch_listings()
        state = load_state()
        seen = set(state["ids"])
        current_ids = [listing_id(ad) for ad in ads if listing_id(ad)]

        if not seen:
            save_state({"ids": current_ids})
            log.info("First run: seeded %s listing IDs, no ads sent", len(current_ids))
            try:
                send_telegram(f"Yad2 bot seeded {len(current_ids)} listings. New ads will be sent from the next run.")
            except RuntimeError as exc:
                log.warning("Seed notification skipped: %s", exc)
            return 0

        new_ads = filter_new(ads, seen)
        for ad in new_ads:
            send_telegram(format_message(ad))
            log.info("Notified listing %s", listing_id(ad))

        merged = list(dict.fromkeys([*state["ids"], *current_ids]))
        save_state({"ids": merged})
        log.info("Done. New ads: %s. State size: %s", len(new_ads), len(merged))
        return 0
    except Exception as exc:
        log.exception("Run failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
