#!/usr/bin/env python3
"""
最新のQiita ClaudeランキングMarkdownをTeams Workflows Webhookへ投稿するスクリプト。

目的:
- output/qiita_claude_ranking_YYYYMMDD.md からランキングを取得する
- Qiita用MarkdownをTeams向けに整形する
- 1位〜10位のみを1つのAdaptive Cardで投稿する
- 各記事タイトルがTeams上で巨大見出しにならないようにする
- Qiita記事側に表示された前日比をTeamsにも表示する
- 11位以降はQiita記事へのリンクから確認してもらう

必要な環境変数:
- TEAMS_WEBHOOK_URL
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib import error, request


OUTPUT_DIR = Path("output")
OUTPUT_FILE_PATTERN = "qiita_claude_ranking_*.md"

ARTICLE_URL = "https://qiita.com/4q_sano/items/1d98dd5fb49ce99bd288"
GITHUB_REPOSITORY_URL = "https://github.com/TakanobuSano/qiita-claude-ranking"

MAX_RANKING_ITEMS = 10


def find_latest_markdown() -> Path:
    files = sorted(OUTPUT_DIR.glob(OUTPUT_FILE_PATTERN))

    if not files:
        raise FileNotFoundError(
            f"{OUTPUT_DIR}/ 配下に {OUTPUT_FILE_PATTERN} が見つかりません。"
        )

    return files[-1]


def extract_date_text(md_path: Path) -> str:
    date_part = md_path.stem.replace("qiita_claude_ranking_", "")

    try:
        dt = datetime.strptime(date_part, "%Y%m%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_part


def extract_summary_info(markdown: str) -> dict[str, str]:
    info = {
        "last_updated": "",
        "target_period": "",
        "article_count": "",
    }

    last_updated_match = re.search(r"最終更新:\s*\*\*(.*?)\*\*", markdown)
    if last_updated_match:
        info["last_updated"] = last_updated_match.group(1).strip()

    target_period_match = re.search(r"対象期間:\s*(.+)", markdown)
    if target_period_match:
        info["target_period"] = target_period_match.group(1).strip()

    article_count_match = re.search(r"集計記事数:\s*(.+)", markdown)
    if article_count_match:
        info["article_count"] = article_count_match.group(1).strip()

    return info


def remove_qiita_note_blocks(markdown: str) -> str:
    """
    Teamsでは :::note info などのQiita独自記法がそのまま表示されるため除去する。
    """
    return re.sub(
        r":::note\s+info\s*.*?:::",
        "",
        markdown,
        flags=re.DOTALL,
    ).strip()


def split_ranking_blocks(markdown: str) -> list[str]:
    """
    Markdown本文からランキングブロックを抽出する。

    想定:
    ## 1位 [タイトル](URL)
    ...
    ## 2位 [タイトル](URL)
    ...
    """
    cleaned = remove_qiita_note_blocks(markdown)

    matches = list(re.finditer(r"^##\s+\d+位\s+.+$", cleaned, flags=re.MULTILINE))

    if not matches:
        return []

    blocks: list[str] = []

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        block = cleaned[start:end].strip()

        if block:
            blocks.append(block)

    return blocks[:MAX_RANKING_ITEMS]


def normalize_delta(delta_text: str) -> str:
    """
    Qiita側の「前日比 +3」「+3」「±0」「新規」などをTeams表示向けに整える。
    """
    value = delta_text.strip()

    value = value.replace("前日比", "").strip()
    value = value.replace("　", " ").strip()

    return value


def parse_ranking_block(block: str) -> dict[str, object]:
    """
    1つのランキングブロックから必要情報を抽出する。

    対応する想定例:
    ## 1位 [記事タイトル](https://qiita.com/...)

    ◇ **100ストック**（前日比 +8） ♡ **102いいね**（前日比 +5） / [user](https://qiita.com/user) さん 2026-05-30 15時投稿

    `Claude` `ClaudeCode`
    """
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    heading = lines[0] if lines else ""

    result: dict[str, object] = {
        "rank": "",
        "title": heading,
        "url": "",
        "stocks": "",
        "stocks_delta": "",
        "likes": "",
        "likes_delta": "",
        "author": "",
        "posted_at": "",
        "tags": [],
    }

    heading_match = re.match(r"^##\s+(\d+)位\s+\[(.*?)\]\((.*?)\)", heading)
    if heading_match:
        result["rank"] = heading_match.group(1)
        result["title"] = heading_match.group(2)
        result["url"] = heading_match.group(3)
    else:
        simple_heading_match = re.match(r"^##\s+(\d+)位\s+(.+)", heading)
        if simple_heading_match:
            result["rank"] = simple_heading_match.group(1)
            result["title"] = simple_heading_match.group(2)

    stocks_match = re.search(
        r"◇\s*\*\*(\d+)ストック\*\*(?:[（(]\s*前日比\s*([^）)]+)\s*[）)])?",
        block,
    )
    if stocks_match:
        result["stocks"] = stocks_match.group(1)
        if stocks_match.group(2):
            result["stocks_delta"] = normalize_delta(stocks_match.group(2))

    likes_match = re.search(
        r"♡\s*\*\*(\d+)いいね\*\*(?:[（(]\s*前日比\s*([^）)]+)\s*[）)])?",
        block,
    )
    if likes_match:
        result["likes"] = likes_match.group(1)
        if likes_match.group(2):
            result["likes_delta"] = normalize_delta(likes_match.group(2))

    author_match = re.search(r"/\s*\[(.*?)\]\(.*?\)\s*さん\s*(.+?投稿)", block)
    if author_match:
        result["author"] = author_match.group(1)
        result["posted_at"] = author_match.group(2)

    tags = re.findall(r"`([^`]+)`", block)
    result["tags"] = tags[:6]

    return result


def format_metric_with_delta(label: str, value: str, delta: str) -> str:
    """
    Teams表示用に、ストック数・いいね数と前日比を整形する。

    例:
    ◇ 100ストック（+8）
    ♡ 102いいね（±0）
    ◇ 12ストック（新規）
    """
    if not value:
        return ""

    if delta:
        return f"{label} {value}（{delta}）"

    return f"{label} {value}"


def build_item_text(item: dict[str, object]) -> str:
    """
    Teamsカード内に表示する1記事分のテキストを作成する。

    重要:
    - Qiita Markdownの「## 1位 ...」はTeamsに渡さない
    - 通常サイズの太字リンクとして表示する
    - 既存の3行構成は維持する
    - 前日比がある場合は、ストック数・いいね数の横に表示する
    """
    rank = str(item.get("rank", "")).strip()
    title = str(item.get("title", "")).strip()
    url = str(item.get("url", "")).strip()

    stocks = str(item.get("stocks", "")).strip()
    stocks_delta = str(item.get("stocks_delta", "")).strip()

    likes = str(item.get("likes", "")).strip()
    likes_delta = str(item.get("likes_delta", "")).strip()

    author = str(item.get("author", "")).strip()
    posted_at = str(item.get("posted_at", "")).strip()
    tags = item.get("tags", [])

    title_text = f"{rank}位 {title}" if rank else title

    if url:
        first_line = f"**[{title_text}]({url})**"
    else:
        first_line = f"**{title_text}**"

    metrics: list[str] = []

    stock_text = format_metric_with_delta("◇", f"{stocks}ストック", stocks_delta)
    if stock_text:
        metrics.append(stock_text)

    like_text = format_metric_with_delta("♡", f"{likes}いいね", likes_delta)
    if like_text:
        metrics.append(like_text)

    if author:
        metrics.append(f"{author} さん")

    if posted_at:
        metrics.append(posted_at)

    second_line = " / ".join(metrics)

    tag_line = ""
    if isinstance(tags, list) and tags:
        tag_line = " ".join([f"`{tag}`" for tag in tags])

    parts = [first_line]

    if second_line:
        parts.append(second_line)

    if tag_line:
        parts.append(tag_line)

    return "\n".join(parts)


def build_adaptive_card_payload(
    ranking_items: list[dict[str, object]],
    updated_date: str,
    summary_info: dict[str, str],
) -> dict:
    """
    Teams Workflows Webhook向けのAdaptive Card payloadを作成する。
    Top10を1投稿にまとめる。
    """
    body: list[dict[str, object]] = [
        {
            "type": "TextBlock",
            "text": "Claude関連Qiitaランキング Top10",
            "weight": "Bolder",
            "size": "Default",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"更新日: {updated_date}",
            "isSubtle": True,
            "spacing": "Small",
            "wrap": True,
        },
    ]

    summary_lines: list[str] = []

    if summary_info.get("last_updated"):
        summary_lines.append(f"最終更新: {summary_info['last_updated']}")

    if summary_info.get("target_period"):
        summary_lines.append(f"対象期間: {summary_info['target_period']}")

    if summary_info.get("article_count"):
        summary_lines.append(f"集計記事数: {summary_info['article_count']}")

    if summary_lines:
        body.append(
            {
                "type": "TextBlock",
                "text": "\n".join(summary_lines),
                "wrap": True,
                "spacing": "Small",
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": (
                "Qiitaの `claude` / `ClaudeCode` / `MCP` タグ記事を対象に、"
                "直近期間の投稿記事をストック数順で集計しています。"
            ),
            "wrap": True,
            "spacing": "Small",
        }
    )

    for item in ranking_items:
        body.append(
            {
                "type": "TextBlock",
                "text": build_item_text(item),
                "wrap": True,
                "separator": True,
                "spacing": "Medium",
            }
        )

    body.append(
        {
            "type": "TextBlock",
            "text": "11位以降はQiita記事で確認できます。",
            "wrap": True,
            "separator": True,
            "spacing": "Medium",
            "isSubtle": True,
        }
    )

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
                    "body": body,
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Qiita記事で全文を見る",
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


def build_payload_from_markdown(md_path: Path) -> dict:
    markdown = md_path.read_text(encoding="utf-8")
    updated_date = extract_date_text(md_path)
    summary_info = extract_summary_info(markdown)

    blocks = split_ranking_blocks(markdown)

    if not blocks:
        raise RuntimeError("ランキングブロックが見つかりません。Markdown形式を確認してください。")

    ranking_items = [parse_ranking_block(block) for block in blocks]

    if len(ranking_items) < MAX_RANKING_ITEMS:
        print(
            f"[warn] ranking items are fewer than {MAX_RANKING_ITEMS}: {len(ranking_items)}",
            file=sys.stderr,
        )

    return build_adaptive_card_payload(
        ranking_items=ranking_items,
        updated_date=updated_date,
        summary_info=summary_info,
    )


def post_to_teams(webhook_url: str, payload_obj: dict) -> None:
    """
    Teams Workflows WebhookへAdaptive Card形式でPOSTする。
    """
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
        print(f"[info] latest markdown: {latest_md_path}", file=sys.stderr)

        payload = build_payload_from_markdown(latest_md_path)

        print("[info] posting Top10 ranking card to Teams...", file=sys.stderr)
        post_to_teams(webhook_url, payload)

        print("[done] posted Teams card successfully.")
        return 0

    except Exception as exc:
        print(f"[error] failed to post to Teams: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
