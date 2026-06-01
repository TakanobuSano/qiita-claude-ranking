#!/usr/bin/env python3
"""
最新のQiita ClaudeランキングMarkdownをTeams Workflows Webhookへ投稿するスクリプト。

前提:
- output/ 配下に qiita_claude_ranking_YYYYMMDD.md が生成済み
- GitHub Secrets に TEAMS_WEBHOOK_URL が登録済み
- .github/workflows/weekly-ranking.yml から python post_to_teams.py で実行する

必要な環境変数:
- TEAMS_WEBHOOK_URL
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib import error, request


OUTPUT_DIR = Path("output")
OUTPUT_FILE_PATTERN = "qiita_claude_ranking_*.md"

ARTICLE_URL = "https://qiita.com/4q_sano/items/1d98dd5fb49ce99bd288"
GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/qiita-claude-ranking"

# Teams Workflowsのpayload上限対策。
# 画面上は256KBと表示されていますが、カード本文が長すぎると見づらいため控えめに制限します。
MAX_TEAMS_TEXT_LENGTH = 12000


def find_latest_markdown() -> Path:
    """
    output/ 配下から最新のランキングMarkdownを取得する。
    ファイル名が qiita_claude_ranking_YYYYMMDD.md 形式なので、名前順の最後を最新として扱う。
    """
    files = sorted(OUTPUT_DIR.glob(OUTPUT_FILE_PATTERN))

    if not files:
        raise FileNotFoundError(
            f"{OUTPUT_DIR}/ 配下に {OUTPUT_FILE_PATTERN} が見つかりません。"
        )

    return files[-1]


def extract_date_text(md_path: Path) -> str:
    """
    ファイル名から更新日を抽出する。
    例:
    qiita_claude_ranking_20260601.md -> 2026-06-01
    """
    date_part = md_path.stem.replace("qiita_claude_ranking_", "")

    try:
        dt = datetime.strptime(date_part, "%Y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_part


def trim_text(text: str, max_length: int) -> str:
    """
    Teams投稿が長くなりすぎないように文字数制限する。
    """
    if len(text) <= max_length:
        return text

    trimmed = text[:max_length].rstrip()

    return (
        f"{trimmed}\n\n"
        "...（Teams投稿用に一部省略）\n\n"
        f"全文はこちら:\n{ARTICLE_URL}"
    )


def build_teams_message(md_path: Path) -> str:
    """
    Teamsに表示する本文を作成する。
    """
    ranking_body = md_path.read_text(encoding="utf-8").strip()
    updated_date = extract_date_text(md_path)

    message = f"""【Qiita Claude関連タグ 週間ランキング】

更新日:
{updated_date}

概要:
Qiitaの `claude` / `ClaudeCode` / `MCP` タグ記事を対象に、直近7日間に投稿された記事をストック数順で集計しました。

ランキング:
{ranking_body}

詳しく読む:
{ARTICLE_URL}

GitHub:
{GITHUB_REPOSITORY_URL}
"""

    return trim_text(message.strip(), MAX_TEAMS_TEXT_LENGTH)


def build_adaptive_card_payload(message: str) -> dict:
    """
    Teams Workflows Webhook向けのAdaptive Card payloadを作成する。

    Workflows画面に
    "The message must include either an adaptive card or message card formatted payload"
    と表示されているため、単純な {"text": "..."} ではなくカード形式にする。
    """
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "Qiita Claude関連タグ 週間ランキング",
                            "weight": "Bolder",
                            "size": "Medium",
                            "wrap": True,
                        },
                        {
                            "type": "TextBlock",
                            "text": message,
                            "wrap": True,
                        },
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Qiita記事を開く",
                            "url": ARTICLE_URL,
                        },
                        {
                            "type": "Action.OpenUrl",
                            "title": "GitHubリポジトリを開く",
                            "url": GITHUB_REPOSITORY_URL,
                        },
                    ],
                },
            }
        ],
    }


def post_to_teams(webhook_url: str, message: str) -> None:
    """
    Teams Workflows WebhookへAdaptive Card形式でPOSTする。
    """
    payload_obj = build_adaptive_card_payload(message)
    payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

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
            response_body = resp.read().decode("utf-8", errors="replace")

        if status_code < 200 or status_code >= 300:
            raise RuntimeError(
                f"Teams webhook failed: HTTP {status_code}: {response_body}"
            )

    except error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Teams webhook failed: HTTP {e.code}: {err_body}"
        ) from e

    except error.URLError as e:
        raise RuntimeError(f"Teams webhook request failed: {e}") from e


def main() -> int:
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")

    if not webhook_url:
        print("[error] TEAMS_WEBHOOK_URL is required.", file=sys.stderr)
        return 1

    try:
        latest_md_path = find_latest_markdown()
        message = build_teams_message(latest_md_path)

        if not message.strip():
            print("[error] Teams message is empty.", file=sys.stderr)
            return 1

        print(f"[info] latest markdown: {latest_md_path}", file=sys.stderr)
        print("[info] posting ranking summary to Teams...", file=sys.stderr)

        post_to_teams(webhook_url, message)

        print("[done] posted to Teams successfully.")
        return 0

    except Exception as exc:
        print(f"[error] failed to post to Teams: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
