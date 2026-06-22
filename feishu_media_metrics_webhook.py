#!/usr/bin/env python3
"""
HTTP webhook for Feishu Base automation.

POST /metrics
Authorization: Bearer <WEBHOOK_TOKEN>

Body:
{
  "record_id": "recxxxx",
  "video_url": "https://..."
}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote

import requests

from feishu_media_metrics_sync import (
    BASE_TOKEN,
    FIELD_COMMENT,
    FIELD_FAVORITE,
    FIELD_LIKE,
    FIELD_LINK,
    FIELD_VIEW,
    FIELD_VIEW_SCREENSHOT,
    SELECT_FIELDS,
    TABLE_ID,
    extract_url,
    scrape,
    to_int,
)


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
FIELD_IDS = {
    FIELD_LINK: "fld8Y9l8aH",
    FIELD_LIKE: "fld3piIvmU",
    FIELD_COMMENT: "fldYFfJ4bl",
    FIELD_FAVORITE: "flddGDniLg",
    FIELD_VIEW: "fldFnqVdPk",
    FIELD_VIEW_SCREENSHOT: "fldnQ6Ep8X",
}
FIELD_NAMES_BY_ID = {field_id: name for name, field_id in FIELD_IDS.items()}
_OCR_ENGINE: Any | None = None


class FeishuClient:
    def __init__(self) -> None:
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self.base_token = os.environ.get("FEISHU_BASE_TOKEN", BASE_TOKEN)
        self.table_id = os.environ.get("FEISHU_TABLE_ID", TABLE_ID)
        if not self.app_id or not self.app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        self._tenant_access_token: str | None = None

    def tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        resp = requests.post(
            f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=20,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"tenant token failed: {data}")
        self._tenant_access_token = data["tenant_access_token"]
        return self._tenant_access_token

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.tenant_access_token()}"
        resp = requests.request(
            method,
            f"{FEISHU_API_BASE}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu API failed: {data}")
        return data

    def request_bytes(self, method: str, path: str, **kwargs: Any) -> bytes:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.tenant_access_token()}"
        resp = requests.request(
            method,
            f"{FEISHU_API_BASE}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        resp.raise_for_status()
        return resp.content

    def get_record(self, record_id: str) -> dict[str, Any]:
        data = self.request(
            "GET",
            f"/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records/{record_id}",
        )
        raw_fields = data["data"]["record"].get("fields") or {}
        fields = dict(raw_fields)
        for field_id, field_name in FIELD_NAMES_BY_ID.items():
            if field_id in raw_fields and field_name not in fields:
                fields[field_name] = raw_fields[field_id]
        fields["record_id"] = record_id
        return fields

    def update_record(self, record_id: str, patch: dict[str, int]) -> None:
        self.request(
            "PUT",
            f"/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records/{record_id}",
            json={"fields": patch},
        )

    def list_attachments(self, record_id: str, field_id: str) -> list[dict[str, Any]]:
        data = self.request(
            "POST",
            f"/base/v3/bases/{self.base_token}/tables/{self.table_id}/get_attachments",
            json={"record_id_list": [record_id]},
        )
        record_attachments = data.get("data", {}).get("attachments", {}).get(record_id, {})
        return record_attachments.get(field_id) or []

    def download_attachment(self, file_token: str, extra_info: str | None) -> bytes:
        params = {"extra": extra_info} if extra_info else None
        return self.request_bytes("GET", f"/drive/v1/medias/{quote(file_token, safe='')}/download", params=params)


def get_ocr_engine() -> Any:
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def text_number(value: str) -> int | None:
    match = re.search(r"[\d,.]+(?:万|[wW])?", value.replace("，", ","))
    if not match:
        return None
    return to_int(match.group(0))


def box_center(box: list[list[float]]) -> tuple[float, float]:
    xs = [point[0] for point in box]
    ys = [point[1] for point in box]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def parse_view_count_from_ocr(items: list[dict[str, Any]]) -> int | None:
    for item in items:
        text = item["text"]
        x, y = item["center"]
        candidates: list[tuple[float, int]] = []
        for other in items:
            number = text_number(other["text"])
            if number is None:
                continue
            ox, oy = other["center"]
            if "阅读" in text and ox > x and abs(oy - y) < 28:
                candidates.append((abs(oy - y) + abs(ox - x) / 10, number))
            if "观看人数" in text and oy < y and abs(ox - x) < 45:
                candidates.append((abs(oy - y) + abs(ox - x), number))
        if candidates:
            return sorted(candidates)[0][1]
    return None


def extract_view_count_from_image(image: bytes) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
        tmp.write(image)
        tmp.flush()
        result, _ = get_ocr_engine()(tmp.name)

    items: list[dict[str, Any]] = []
    for row in result or []:
        box, text = row[0], str(row[1])
        items.append({"text": text, "center": box_center(box)})

    views = parse_view_count_from_ocr(items)
    return {"views": views, "ocr_text": [item["text"] for item in items]}


def handle_metrics(payload: dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
    record_id = str(payload.get("record_id") or "").strip()
    if not record_id:
        raise ValueError("record_id is required")

    client = FeishuClient()
    current = client.get_record(record_id)
    raw_video_url = payload.get("video_url") or current.get(FIELD_LINK)
    video_url = extract_url(raw_video_url) or str(raw_video_url or "").strip()
    if not video_url:
        raise ValueError("video_url is required")

    metrics = scrape(video_url)
    patch = metrics.patch(overwrite=overwrite, current=current)
    patch.pop(FIELD_VIEW, None)
    if patch:
        client.update_record(record_id, patch)

    return {
        "ok": True,
        "record_id": record_id,
        "source": metrics.source,
        "updated": patch,
        "skipped_existing": {
            field: current.get(field)
            for field in [FIELD_LIKE, FIELD_COMMENT, FIELD_FAVORITE]
            if current.get(field) not in (None, "")
        },
    }


def handle_views(payload: dict[str, Any], overwrite: bool = False) -> dict[str, Any]:
    record_id = str(payload.get("record_id") or "").strip()
    if not record_id:
        raise ValueError("record_id is required")

    client = FeishuClient()
    current = client.get_record(record_id)
    if not overwrite and current.get(FIELD_VIEW) not in (None, ""):
        return {"ok": True, "record_id": record_id, "updated": {}, "skipped_existing": {FIELD_VIEW: current.get(FIELD_VIEW)}}

    attachments = client.list_attachments(record_id, FIELD_IDS[FIELD_VIEW_SCREENSHOT])
    if not attachments:
        raise ValueError(f"{FIELD_VIEW_SCREENSHOT} is required")

    last_result: dict[str, Any] | None = None
    for attachment in attachments:
        image = client.download_attachment(attachment["file_token"], attachment.get("extra_info"))
        last_result = extract_view_count_from_image(image)
        views = last_result.get("views")
        if views is not None:
            patch = {FIELD_VIEW: views}
            client.update_record(record_id, patch)
            return {
                "ok": True,
                "record_id": record_id,
                "source": "screenshot",
                "updated": patch,
                "file": attachment.get("name"),
                "ocr_text": last_result.get("ocr_text", []),
            }

    return {
        "ok": True,
        "record_id": record_id,
        "source": "screenshot",
        "updated": {},
        "error": "view count not found",
        "ocr_text": (last_result or {}).get("ocr_text", []),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FeishuMediaMetricsWebhook/1.0"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/metrics", "/views"}:
            self.send_json(404, {"ok": False, "error": "not found"})
            return

        token = os.environ.get("WEBHOOK_TOKEN", "")
        auth = self.headers.get("Authorization", "")
        if token and auth != f"Bearer {token}":
            self.send_json(401, {"ok": False, "error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw) if raw else {}
            overwrite = bool(payload.get("overwrite"))
            result = handle_views(payload, overwrite=overwrite) if self.path == "/views" else handle_metrics(payload, overwrite=overwrite)
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8788")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"listening on http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
