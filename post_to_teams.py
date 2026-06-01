#!/usr/bin/env python3
"""
最新のQiitaランキングMarkdownをTeams Webhookへ投稿するスクリプト。

必要な環境変数:
- TEAMS_WEBHOOK_URL

前提:
- fetch_ranking.py により output/qiita_claude_ranking_YYYYMMDD.md が生成済み
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib import request, error


OUTPUT_DIR = Path("output")
ARTICLE_URL = "https://qiita.com/4q_sano/items/1d98dd5fb49ce99bd288"
MAX_TEAMS_TEXT_LENGTH = 24000


def find_latest_markdown() -> Path:
    files = sorted(OUTPUT_DIR.glob("qiita_claude_ranking_*.md"))

    if not files:
        raise FileNotFoundError(
            "output/ 配下に qiita_claude_ranking_*.md が見つかりません。"
        )

    return files[-1]


def extract_date_text(md_path: Path) -> str:
    date_part = md_path.stem.replace("qiita_claude_ranking_", "")

    try:
        dt = datetime.strptime(date_part, "%Y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_part


def build_teams_message(md_path: Path) -> str:
    ranking_body = md_path.read_text(encoding="utf-8").strip()
    updated_date = extract_date_text(md_path)

    message = f"""【Qiita Claude関連タグ 週間ランキング】

更新日:
{updated_date}

概要:
Qiitaの `claude` / `ClaudeCode` / `MCP` タグ記事を対象に、直近14日間に投稿された記事をストック数順で集計しました。

ランキング:
{ranking_body}

詳しく読む:
{ARTICLE_URL}
"""

    if len(message) > MAX_TEAMS_TEXT_LENGTH:
        message = message[:MAX_TEAMS_TEXT_LENGTH]
        message += f"""

...（Teams投稿用に一部省略）

全文はこちら:
{ARTICLE_URL}
"""

    return message


def post_to_teams(webhook_url: str, text: str) -> None:
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")

    req = request.Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "qiita-claude-ranking-teams-notifier/1.0",
        },
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            status_code = resp.getcode()

        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"Teams webhook failed: HTTP {status_code}")

    except error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Teams webhook failed: HTTP {e.code}: {err_body}") from e


def main() -> int:
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")

    if not webhook_url:
        print("[error] TEAMS_WEBHOOK_URL is required.", file=sys.stderr)
        return 1

    md_path = find_latest_markdown()
    message = build_teams_message(md_path)

    if not message.strip():
        print("[error] Teams message is empty.", file=sys.stderr)
        return 1

    print(f"[info] posting Teams message from: {md_path}", file=sys.stderr)
    post_to_teams(webhook_url, message)
    print("[done] posted to Teams")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
