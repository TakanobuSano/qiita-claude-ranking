#!/usr/bin/env python3
"""
GitHub Actionsで生成した最新ランキングMarkdownを、
既存のQiita記事に上書き更新するスクリプト。

必要な環境変数:
- QIITA_ACCESS_TOKEN
- QIITA_ITEM_ID
- QIITA_POST_PRIVATE
  - true  : 限定共有のまま更新
  - false : 公開記事として更新
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib import error, request


OUTPUT_DIR = Path("output")
QIITA_API_BASE = "https://qiita.com/api/v2/items"

TITLE = "Claude関連 注目ストック数ランキング【毎日自動更新】"

EXPLANATION_ARTICLE_URL = "https://qiita.com/4q_sano/items/1bc5e0669a8f0166936c"
GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/qiita-claude-ranking"


def find_latest_markdown() -> Path:
    """
    output/ 配下から最新の qiita_claude_ranking_*.md を取得する。
    ファイル名の日付順に並べ、最後のファイルを最新として扱う。
    """
    files = sorted(OUTPUT_DIR.glob("qiita_claude_ranking_*.md"))

    if not files:
        raise FileNotFoundError(
            "output/ 配下に qiita_claude_ranking_*.md が見つかりません。"
        )

    return files[-1]


def extract_date_text(md_path: Path) -> str:
    """
    ファイル名から更新日を抽出する。

    例:
    qiita_claude_ranking_20260528.md
    -> 2026-05-28
    """
    date_part = md_path.stem.replace("qiita_claude_ranking_", "")

    try:
        dt = datetime.strptime(date_part, "%Y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_part


def build_body(md_path: Path) -> str:
    """
    最新Markdown本文に、ランキング説明用のフッターを追加する。
    """
    body = md_path.read_text(encoding="utf-8")
    updated_date = extract_date_text(md_path)

    footer = f"""

---

## このランキングについて

この記事は、GitHub Actions と Qiita API v2 を使って自動更新しています。

- 最終更新日: {updated_date}
- 更新頻度: 毎日自動更新
- 更新方法: GitHub Actions と Qiita API v2 による自動更新
- 補足: cron-job.org から `workflow_dispatch` を起動して更新しています
- 対象タグ: `claude`, `ClaudeCode`, `MCP`
- 集計基準: 直近14日間に投稿された記事の累計ストック数

:::note warn
このランキングは「直近14日間に投稿された記事の累計ストック数ランキング」です。「この2週間で増えたストック数ランキング」ではありません。
:::

## 作成方法の解説

{EXPLANATION_ARTICLE_URL}

## GitHubリポジトリ

[qiita-claude-ranking]({GITHUB_REPOSITORY_URL})
"""

    return body + footer


def update_qiita_item(
    item_id: str,
    title: str,
    body: str,
    private: bool,
    token: str,
) -> dict:
    """
    Qiita API v2 の PATCH /api/v2/items/:item_id を使って、
    既存のQiita記事を上書き更新する。
    """
    url = f"{QIITA_API_BASE}/{item_id}"

    payload = {
        "title": title,
        "body": body,
        "private": private,
        "tags": [
            {"name": "Python", "versions": []},
            {"name": "GitHubActions", "versions": []},
            {"name": "Claude", "versions": []},
            {"name": "ClaudeCode", "versions": []},
            {"name": "MCP", "versions": []},
        ],
        "slide": False,
    }

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "qiita-claude-ranking-updater/1.0",
        },
    )

    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Qiita update failed: HTTP {e.code}: {err_body}") from e


def main() -> int:
    token = os.environ.get("QIITA_ACCESS_TOKEN")
    item_id = os.environ.get("QIITA_ITEM_ID")

    if not token:
        print("[error] QIITA_ACCESS_TOKEN is required.", file=sys.stderr)
        return 1

    if not item_id:
        print("[error] QIITA_ITEM_ID is required.", file=sys.stderr)
        return 1

    private = os.environ.get("QIITA_POST_PRIVATE", "true").lower() == "true"

    md_path = find_latest_markdown()
    body = build_body(md_path)

    print(f"[info] updating Qiita item: {item_id}", file=sys.stderr)
    print(f"[info] source markdown: {md_path}", file=sys.stderr)
    print(f"[info] private: {private}", file=sys.stderr)
    print(f"[info] title: {TITLE}", file=sys.stderr)

    result = update_qiita_item(
        item_id=item_id,
        title=TITLE,
        body=body,
        private=private,
        token=token,
    )

    print("[done] updated Qiita item")
    print(result.get("url", ""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
