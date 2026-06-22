#!/usr/bin/env python3
"""
Sync public video metrics back to a Feishu Base sampling table.

Default mode is dry-run. Use --apply to write updates.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from requests import RequestException


BASE_TOKEN = "Sj0XbUehqasIgGsTB1JcVsbMn4e"
TABLE_ID = "tblCYLbbNQLVQjcq"

FIELD_LINK = "视频发布链接"
FIELD_LIKE = "点赞"
FIELD_COMMENT = "评论数量"
FIELD_FAVORITE = "收藏"
FIELD_VIEW = "播放量"
FIELD_VIEW_SCREENSHOT = "播放后台截图"

SELECT_FIELDS = [FIELD_LINK, FIELD_LIKE, FIELD_COMMENT, FIELD_FAVORITE, FIELD_VIEW, FIELD_VIEW_SCREENSHOT]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


@dataclass
class Metrics:
    likes: int | None = None
    comments: int | None = None
    favorites: int | None = None
    views: int | None = None
    shares: int | None = None
    source: str = ""

    def patch(self, overwrite: bool, current: dict[str, Any]) -> dict[str, int]:
        mapping = {
            FIELD_LIKE: self.likes,
            FIELD_COMMENT: self.comments,
            FIELD_FAVORITE: self.favorites,
            FIELD_VIEW: self.views,
        }
        patch: dict[str, int] = {}
        for field, value in mapping.items():
            if value is None:
                continue
            if overwrite or current.get(field) in (None, ""):
                patch[field] = value
        return patch


def run_lark(args: list[str]) -> dict[str, Any]:
    proc = subprocess.run(
        ["lark-cli", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout)


def iter_records(limit: int | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    offset = 0
    page_size = 200
    while True:
        cmd = [
            "base",
            "+record-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
            "--format",
            "json",
            "--limit",
            str(page_size),
            "--offset",
            str(offset),
        ]
        for field in SELECT_FIELDS:
            cmd.extend(["--field-id", field])
        payload = run_lark(cmd)
        data = payload["data"]
        rows = data.get("data") or []
        ids = data.get("record_id_list") or []
        fields = data.get("fields") or SELECT_FIELDS
        for record_id, row in zip(ids, rows):
            record = dict(zip(fields, row))
            record["record_id"] = record_id
            records.append(record)
            if limit and len(records) >= limit:
                return records
        if not data.get("has_more") or not rows:
            return records
        offset += len(rows)


def extract_url(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        for key in ("link", "url", "text"):
            url = extract_url(value.get(key))
            if url:
                return url
        return None
    if isinstance(value, list):
        for item in value:
            url = extract_url(item)
            if url:
                return url
        return None
    if isinstance(value, str):
        urls = re.findall(r"https?://[^\s\])]+", value)
        for url in urls:
            host = urlparse(url).netloc.lower()
            if any(domain in host for domain in ["xiaohongshu.com", "xhslink.com", "douyin.com", "bilibili.com", "b23.tv"]):
                return url
        if urls:
            return urls[0]
        match = re.search(r"\((https?://[^)]+)\)", value)
        if match:
            return match.group(1)
    return None


def get(url: str) -> requests.Response:
    return request_with_retry(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.google.com/",
        },
        timeout=20,
    )


def get_mobile(url: str) -> requests.Response:
    return request_with_retry(
        url,
        headers={
            "User-Agent": MOBILE_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.douyin.com/",
        },
        timeout=35,
    )


def request_with_retry(url: str, headers: dict[str, str], timeout: int) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return requests.get(
                url,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
            )
        except RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().strip(",;").replace(",", "")
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10000
        text = text[:-1]
    elif text.endswith("w") or text.endswith("W"):
        multiplier = 10000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def find_json_number(text: str, names: list[str]) -> int | None:
    for name in names:
        patterns = [
            rf'"{re.escape(name)}"\s*:\s*"([\d,.]+(?:万|[wW])?)"',
            rf'"{re.escape(name)}"\s*:\s*([\d.]+(?:万|[wW])?)',
            rf"'{re.escape(name)}'\s*:\s*'([\d,.]+(?:万|[wW])?)'",
            rf"'{re.escape(name)}'\s*:\s*([\d.]+(?:万|[wW])?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                number = to_int(match.group(1))
                if number is not None:
                    return number
    return None


def scrape_bilibili(url: str) -> Metrics:
    final = get(url).url
    bvid_match = re.search(r"BV[0-9A-Za-z]+", final)
    aid_match = re.search(r"/video/av(\d+)", final)
    params = parse_qs(urlparse(final).query)
    bvid = bvid_match.group(0) if bvid_match else params.get("bvid", [None])[0]
    aid = aid_match.group(1) if aid_match else params.get("aid", [None])[0]
    if not bvid and not aid:
        raise ValueError(f"cannot parse Bilibili video id from {final}")

    api = "https://api.bilibili.com/x/web-interface/view"
    resp = requests.get(
        api,
        params={"bvid": bvid} if bvid else {"aid": aid},
        headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        timeout=20,
    )
    payload = resp.json()
    if payload.get("code") != 0:
        raise ValueError(payload.get("message") or "Bilibili API failed")
    stat = payload["data"]["stat"]
    return Metrics(
        likes=to_int(stat.get("like")),
        comments=to_int(stat.get("reply")),
        favorites=to_int(stat.get("favorite")),
        views=to_int(stat.get("view")),
        shares=to_int(stat.get("share")),
        source="bilibili",
    )


def scrape_douyin(url: str) -> Metrics:
    resp = get_mobile(url)
    text = resp.text
    views = find_json_number(text, ["play_count", "playCount"])
    if views == 0:
        views = None
    return Metrics(
        likes=find_json_number(text, ["digg_count", "diggCount", "like_count"]),
        comments=find_json_number(text, ["comment_count", "commentCount"]),
        favorites=find_json_number(text, ["collect_count", "collectCount", "favorite_count"]),
        views=views,
        shares=find_json_number(text, ["share_count", "shareCount"]),
        source="douyin",
    )


def scrape_xiaohongshu(url: str) -> Metrics:
    resp = get(url)
    text = resp.text
    return Metrics(
        likes=find_json_number(text, ["likedCount", "likeCount", "liked_count"]),
        comments=find_json_number(text, ["commentCount", "commentsCount", "comment_count"]),
        favorites=find_json_number(text, ["collectedCount", "collectCount", "collected_count"]),
        views=find_json_number(text, ["viewCount", "view_count"]),
        shares=find_json_number(text, ["shareCount", "share_count"]),
        source="xiaohongshu",
    )


def scrape(url: str) -> Metrics:
    host = urlparse(url).netloc.lower()
    if "bilibili.com" in host or "b23.tv" in host:
        return scrape_bilibili(url)
    if "douyin.com" in host:
        return scrape_douyin(url)
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return scrape_xiaohongshu(url)
    raise ValueError(f"unsupported platform: {host}")


def update_record(record_id: str, patch: dict[str, int], dry_run: bool) -> None:
    if dry_run:
        return
    run_lark(
        [
            "base",
            "+record-upsert",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            TABLE_ID,
            "--record-id",
            record_id,
            "--json",
            json.dumps(patch, ensure_ascii=False),
        ]
    )


def needs_work(record: dict[str, Any], overwrite: bool) -> bool:
    if not extract_url(record.get(FIELD_LINK)):
        return False
    if overwrite:
        return True
    return any(record.get(field) in (None, "") for field in [FIELD_LIKE, FIELD_COMMENT, FIELD_FAVORITE, FIELD_VIEW])


def sync_once(args: argparse.Namespace) -> int:
    records = iter_records(args.limit)
    changed = 0
    skipped = 0
    failed = 0
    for record in records:
        if not needs_work(record, args.overwrite):
            skipped += 1
            continue
        url = extract_url(record.get(FIELD_LINK))
        if not url:
            skipped += 1
            continue
        try:
            metrics = scrape(url)
            patch = metrics.patch(args.overwrite, record)
            if not patch:
                print(f"SKIP {record['record_id']} {metrics.source}: no metrics found or nothing to update")
                skipped += 1
                continue
            update_record(record["record_id"], patch, dry_run=not args.apply)
            mode = "UPDATE" if args.apply else "DRY-RUN"
            print(f"{mode} {record['record_id']} {metrics.source} {json.dumps(patch, ensure_ascii=False)}")
            changed += 1
        except Exception as exc:
            print(f"FAIL {record['record_id']} {url}: {exc}", file=sys.stderr)
            failed += 1
    print(f"done changed={changed} skipped={skipped} failed={failed} apply={args.apply}")
    return 1 if failed and args.fail_on_error else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write values back to Feishu")
    parser.add_argument("--overwrite", action="store_true", help="overwrite existing metric cells")
    parser.add_argument("--limit", type=int, help="max records to scan")
    parser.add_argument("--interval", type=int, help="run forever, sleeping N seconds between scans")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    if args.interval:
        while True:
            sync_once(args)
            time.sleep(args.interval)
    return sync_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
