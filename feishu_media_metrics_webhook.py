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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

from feishu_media_metrics_sync import (
    BASE_TOKEN,
    FIELD_COMMENT,
    FIELD_FAVORITE,
    FIELD_LIKE,
    FIELD_LINK,
    FIELD_VIEW,
    SELECT_FIELDS,
    TABLE_ID,
    extract_url,
    scrape,
)


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
FIELD_IDS = {
    FIELD_LINK: "fld8Y9l8aH",
    FIELD_LIKE: "fld3piIvmU",
    FIELD_COMMENT: "fldYFfJ4bl",
    FIELD_FAVORITE: "flddGDniLg",
    FIELD_VIEW: "fldFnqVdPk",
}
FIELD_NAMES_BY_ID = {field_id: name for name, field_id in FIELD_IDS.items()}


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
        api_patch = {FIELD_IDS.get(field, field): value for field, value in patch.items()}
        self.request(
            "PUT",
            f"/bitable/v1/apps/{self.base_token}/tables/{self.table_id}/records/{record_id}",
            json={"fields": api_patch},
        )


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
    if patch:
        client.update_record(record_id, patch)

    return {
        "ok": True,
        "record_id": record_id,
        "source": metrics.source,
        "updated": patch,
        "skipped_existing": {
            field: current.get(field)
            for field in [FIELD_LIKE, FIELD_COMMENT, FIELD_FAVORITE, FIELD_VIEW]
            if current.get(field) not in (None, "")
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "FeishuMediaMetricsWebhook/1.0"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, {"ok": True})
            return
        self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/metrics":
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
            result = handle_metrics(payload)
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
