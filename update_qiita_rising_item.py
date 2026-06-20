#!/usr/bin/env python3
"""
GitHub Actionsで生成した最新の急上昇ランキングMarkdownを、
既存のQiita記事に上書き更新するスクリプト。

必要な環境変数:
- QIITA_ACCESS_TOKEN
- QIITA_RISING_ITEM_ID
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


OUTPUT_DIR = Path("output/rising")
QIITA_API_BASE = "https://qiita.com/api/v2/items"

TITLE = "【毎日更新】Claude Code関連で今伸びているQiita記事TOP20を急上昇ランキング"

NORMAL_RANKING_URL = "https://qiita.com/4q_sano/items/b2100c31a1fb61116ace"
WEEKLY_TREND_RANKING_URL = "https://qiita.com/4q_sano/items/cc27d3564a657046242a"
POPULAR_RANKING_URL = "https://qiita.com/4q_sano/items/1d98dd5fb49ce99bd288"
EXPLANATION_ARTICLE_URL = "https://qiita.com/4q_sano/items/1bc5e0669a8f0166936c"
GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/qiita-claude-ranking"


def find_latest_markdown() -> Path:
    """
    output/rising/ 配下から最新の qiita_claude_rising_*.md を取得する。
    ファイル名の日付順に並べ、最後のファイルを最新として扱う。
    """
    files = sorted(OUTPUT_DIR.glob("qiita_claude_rising_*.md"))

    if not files:
        raise FileNotFoundError(
            "output/rising/ 配下に qiita_claude_rising_*.md が見つかりません。"
        )

    return files[-1]


def extract_date_text(md_path: Path) -> str:
    """
    ファイル名から更新日を抽出する。

    例:
    qiita_claude_rising_20260608.md
    -> 2026-06-08
    """
    date_part = md_path.stem.replace("qiita_claude_rising_", "")

    try:
        dt = datetime.strptime(date_part, "%Y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_part


def build_body(md_path: Path) -> str:
    """
    最新Markdown本文に、急上昇ランキング説明用のフッターを追加する。
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
- 対象記事: 直近30日間に投稿された記事
- 集計基準: 直近7日間のストック増加数

:::note warn
このランキングは「直近30日間に投稿された記事のうち、直近7日間で増えたストック数」のランキングです。累計ストック数順のランキングではありません。
:::

## 関連ランキング

累計ストック数順の通常ランキングはこちらです。

{NORMAL_RANKING_URL}

Claude Code向けのスキル・MCP・関連ツールを探したい方は、こちらも参考になります。

{WEEKLY_TREND_RANKING_URL}

{POPULAR_RANKING_URL}

## 作成方法の解説

{EXPLANATION_ARTICLE_URL}

## GitHubリポジトリ

{GITHUB_REPOSITORY_URL}
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
            "User-Agent": "qiita-claude-rising-ranking-updater/1.0",
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
    item_id = os.environ.get("QIITA_RISING_ITEM_ID")

    if not token:
        print("[error] QIITA_ACCESS_TOKEN is required.", file=sys.stderr)
        return 1

    if not item_id:
        print("[error] QIITA_RISING_ITEM_ID is required.", file=sys.stderr)
        return 1

    private = os.environ.get("QIITA_POST_PRIVATE", "true").lower() == "true"

    md_path = find_latest_markdown()
    body = build_body(md_path)

    print(f"[info] updating Qiita rising item: {item_id}", file=sys.stderr)
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

    print("[done] updated Qiita rising item")
    print(result.get("url", ""))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
