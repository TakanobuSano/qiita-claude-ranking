#!/usr/bin/env python3
"""
Qiita Claude関連 急上昇ランキング生成スクリプト。

通常ランキング用に保存しているCSVを使って、
「直近30日間に投稿された記事のうち、直近7日間でストック数が増えた記事ランキング」を生成する。

前提:
- fetch_ranking.py が output/qiita_claude_ranking_YYYYMMDD.csv を出力していること
- CSVには直近30日間に投稿された記事が全件保存されていること
- 7日前のCSVが存在すること

出力:
- output/rising/qiita_claude_rising_YYYYMMDD.md
- output/rising/qiita_claude_rising_YYYYMMDD.csv
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TARGET_TAGS = ["claude", "ClaudeCode", "MCP"]

BASE_OUTPUT_DIR = Path("output")
RISING_OUTPUT_DIR = BASE_OUTPUT_DIR / "rising"
BASE_FILE_PREFIX = "qiita_claude_ranking_"
RISING_FILE_PREFIX = "qiita_claude_rising_"

TARGET_POST_LOOKBACK_DAYS = 30
RISING_LOOKBACK_DAYS = 7
TOP_N = 20

JST = timezone(timedelta(hours=9))


@dataclass
class RisingArticle:
    id: str
    title: str
    url: str
    user_id: str
    current_stocks_count: int
    previous_stocks_count: int
    stocks_delta_7d: int
    current_likes_count: int
    previous_likes_count: int
    likes_delta_7d: int
    comments_count: int
    page_views_count: str
    created_at: str
    tags: str
    is_new_in_period: bool


def parse_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_created_date(created_at: str) -> date | None:
    if not created_at:
        return None

    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(JST).date()
    except ValueError:
        return None


def format_created_at(created_at: str) -> str:
    if not created_at:
        return ""

    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(JST).strftime("%Y-%m-%d %H時投稿")
    except ValueError:
        return created_at[:10]


def escape_markdown_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def format_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta}"

    if delta < 0:
        return str(delta)

    return "±0"


def extract_stamp(path: Path, prefix: str) -> str:
    return path.stem.replace(prefix, "")


def stamp_to_date(stamp: str) -> date:
    return datetime.strptime(stamp, "%Y%m%d").date()


def find_latest_base_csv() -> Path:
    files = sorted(BASE_OUTPUT_DIR.glob(f"{BASE_FILE_PREFIX}*.csv"))

    if not files:
        raise FileNotFoundError(
            f"{BASE_OUTPUT_DIR}/ 配下に {BASE_FILE_PREFIX}*.csv が見つかりません。"
        )

    return files[-1]


def find_base_csv_by_date(target_date: date) -> Path | None:
    target_stamp = target_date.strftime("%Y%m%d")
    path = BASE_OUTPUT_DIR / f"{BASE_FILE_PREFIX}{target_stamp}.csv"

    if path.exists():
        return path

    return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build_previous_snapshot(rows: list[dict[str, str]]) -> dict[str, dict[str, int]]:
    snapshot: dict[str, dict[str, int]] = {}

    for row in rows:
        article_id = row.get("id", "")

        if not article_id:
            continue

        snapshot[article_id] = {
            "stocks_count": parse_int(row.get("stocks_count")),
            "likes_count": parse_int(row.get("likes_count")),
        }

    return snapshot


def build_rising_articles(
    current_rows: list[dict[str, str]],
    previous_rows: list[dict[str, str]],
    current_date: date,
    baseline_date: date,
) -> list[RisingArticle]:
    previous_snapshot = build_previous_snapshot(previous_rows)
    target_since_date = current_date - timedelta(days=TARGET_POST_LOOKBACK_DAYS)

    articles: list[RisingArticle] = []

    for row in current_rows:
        article_id = row.get("id", "")

        if not article_id:
            continue

        created_at = row.get("created_at", "")
        created_date = parse_created_date(created_at)

        if created_date is None:
            continue

        # 急上昇ランキングの対象は、直近30日間に投稿された記事。
        if not (target_since_date <= created_date <= current_date):
            continue

        current_stocks_count = parse_int(row.get("stocks_count"))
        current_likes_count = parse_int(row.get("likes_count"))

        previous = previous_snapshot.get(article_id)
        is_new_in_period = False

        if previous is None:
            # 7日前のCSVに存在しない記事のうち、7日前以降に投稿された記事は、
            # 7日間の増加数を現在値として扱う。
            if created_date >= baseline_date:
                previous_stocks_count = 0
                previous_likes_count = 0
                is_new_in_period = True
            else:
                # 7日前時点で存在していた可能性が高いのに前回CSVにない場合、
                # 正確な差分が出せないためランキング対象から除外する。
                continue
        else:
            previous_stocks_count = previous["stocks_count"]
            previous_likes_count = previous["likes_count"]

        stocks_delta_7d = current_stocks_count - previous_stocks_count
        likes_delta_7d = current_likes_count - previous_likes_count

        # 急上昇ランキングなので、ストック増加がない記事は表示対象から除外する。
        if stocks_delta_7d <= 0:
            continue

        articles.append(
            RisingArticle(
                id=article_id,
                title=row.get("title", ""),
                url=row.get("url", ""),
                user_id=row.get("user_id", ""),
                current_stocks_count=current_stocks_count,
                previous_stocks_count=previous_stocks_count,
                stocks_delta_7d=stocks_delta_7d,
                current_likes_count=current_likes_count,
                previous_likes_count=previous_likes_count,
                likes_delta_7d=likes_delta_7d,
                comments_count=parse_int(row.get("comments_count")),
                page_views_count=row.get("page_views_count", ""),
                created_at=created_at,
                tags=row.get("tags", ""),
                is_new_in_period=is_new_in_period,
            )
        )

    articles.sort(
        key=lambda a: (
            a.stocks_delta_7d,
            a.current_stocks_count,
            a.likes_delta_7d,
            a.current_likes_count,
        ),
        reverse=True,
    )

    return articles


def render_insufficient_markdown(
    current_date: date,
    baseline_date: date,
    current_csv_path: Path,
) -> str:
    target_tags_text = ", ".join(f"`{tag}`" for tag in TARGET_TAGS)

    lines: list[str] = []
    lines.append("")
    lines.append(":::note warn")
    lines.append("急上昇ランキングの生成に必要な7日前のCSVがまだ見つかりません。")
    lines.append(":::")
    lines.append("")
    lines.append("## 集計準備中")
    lines.append("")
    lines.append("このページは、直近30日間に投稿されたClaude関連Qiita記事を対象に、直近7日間のストック増加数でランキング化する予定です。")
    lines.append("")
    lines.append(":::note info")
    lines.append(f"- 対象タグ: {target_tags_text}")
    lines.append(f"- 対象記事: 直近{TARGET_POST_LOOKBACK_DAYS}日間に投稿された記事")
    lines.append(f"- 集計基準: 直近{RISING_LOOKBACK_DAYS}日間のストック増加数")
    lines.append(f"- 最新CSV: `{current_csv_path.name}`")
    lines.append(f"- 必要な比較元CSVの日付: {baseline_date.isoformat()}")
    lines.append(f"- 現在の日付: {current_date.isoformat()}")
    lines.append(":::")
    lines.append("")
    lines.append("7日前のCSVが保存されたあと、自動的にランキングを生成できます。")
    lines.append("")

    return "\n".join(lines)


def render_markdown(
    rising_articles: list[RisingArticle],
    current_date: date,
    baseline_date: date,
    current_csv_path: Path,
    baseline_csv_path: Path,
) -> str:
    target_tags_text = ", ".join(f"`{tag}`" for tag in TARGET_TAGS)
    top = rising_articles[:TOP_N]
    updated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append("")
    lines.append(":::note info")
    lines.append(f"最終更新: **{updated_at} JST**")
    lines.append("")
    lines.append(f"- 対象タグ: {target_tags_text}")
    lines.append(f"- 対象記事: 直近{TARGET_POST_LOOKBACK_DAYS}日間に投稿された記事")
    lines.append(f"- 集計期間: {baseline_date.isoformat()} 〜 {current_date.isoformat()}")
    lines.append(f"- 集計基準: 直近{RISING_LOOKBACK_DAYS}日間のストック増加数")
    lines.append(f"- 集計対象記事数: {len(rising_articles)} 件")
    lines.append(":::")
    lines.append("")

    if not top:
        lines.append(":::note info")
        lines.append("直近7日間でストック数が増加した記事は見つかりませんでした。")
        lines.append(":::")
        lines.append("")
        return "\n".join(lines)

    for i, article in enumerate(top, 1):
        title = escape_markdown_text(article.title)
        user_id = article.user_id
        user_url = f"https://qiita.com/{user_id}" if user_id else ""
        created = format_created_at(article.created_at)
        tag_badges = " ".join(f"`{tag}`" for tag in article.tags.split(",")[:5] if tag)

        user_part = (
            f"[{user_id}]({user_url}) さん"
            if user_id and user_url
            else "ユーザー不明"
        )

        new_label = " / 新規" if article.is_new_in_period else ""

        lines.append(f"## {i}位 [{title}]({article.url})")
        lines.append("")
        lines.append(
            f"◇ **{format_delta(article.stocks_delta_7d)}ストック** "
            f"（{article.previous_stocks_count} → {article.current_stocks_count}） "
            f"♡ **{format_delta(article.likes_delta_7d)}いいね** "
            f"（{article.previous_likes_count} → {article.current_likes_count}） / "
            f"{user_part} {created}{new_label}"
        )
        lines.append("")

        if tag_badges:
            lines.append(tag_badges)
            lines.append("")

        if i != len(top):
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def write_rising_csv(path: Path, rising_articles: list[RisingArticle]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)

        writer.writerow(
            [
                "rank",
                "id",
                "title",
                "url",
                "user_id",
                "stocks_delta_7d",
                "current_stocks_count",
                "previous_stocks_count",
                "likes_delta_7d",
                "current_likes_count",
                "previous_likes_count",
                "comments_count",
                "page_views_count",
                "created_at",
                "tags",
                "is_new_in_period",
            ]
        )

        for i, article in enumerate(rising_articles, 1):
            writer.writerow(
                [
                    i,
                    article.id,
                    article.title,
                    article.url,
                    article.user_id,
                    article.stocks_delta_7d,
                    article.current_stocks_count,
                    article.previous_stocks_count,
                    article.likes_delta_7d,
                    article.current_likes_count,
                    article.previous_likes_count,
                    article.comments_count,
                    article.page_views_count,
                    article.created_at,
                    article.tags,
                    "true" if article.is_new_in_period else "false",
                ]
            )


def main() -> int:
    RISING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    current_csv_path = find_latest_base_csv()
    current_stamp = extract_stamp(current_csv_path, BASE_FILE_PREFIX)
    current_date = stamp_to_date(current_stamp)
    baseline_date = current_date - timedelta(days=RISING_LOOKBACK_DAYS)
    baseline_csv_path = find_base_csv_by_date(baseline_date)

    md_path = RISING_OUTPUT_DIR / f"{RISING_FILE_PREFIX}{current_stamp}.md"
    csv_path = RISING_OUTPUT_DIR / f"{RISING_FILE_PREFIX}{current_stamp}.csv"

    if baseline_csv_path is None:
        md_text = render_insufficient_markdown(
            current_date=current_date,
            baseline_date=baseline_date,
            current_csv_path=current_csv_path,
        )
        md_path.write_text(md_text, encoding="utf-8")
        write_rising_csv(csv_path, [])

        print(
            f"[warn] baseline csv not found: "
            f"{BASE_FILE_PREFIX}{baseline_date.strftime('%Y%m%d')}.csv",
            file=sys.stderr,
        )
        print(f"[done] wrote placeholder: {md_path}", file=sys.stderr)
        print(f"[done] wrote empty csv: {csv_path}", file=sys.stderr)
        return 0

    current_rows = read_csv_rows(current_csv_path)
    previous_rows = read_csv_rows(baseline_csv_path)

    rising_articles = build_rising_articles(
        current_rows=current_rows,
        previous_rows=previous_rows,
        current_date=current_date,
        baseline_date=baseline_date,
    )

    md_text = render_markdown(
        rising_articles=rising_articles,
        current_date=current_date,
        baseline_date=baseline_date,
        current_csv_path=current_csv_path,
        baseline_csv_path=baseline_csv_path,
    )

    md_path.write_text(md_text, encoding="utf-8")
    write_rising_csv(csv_path, rising_articles)

    print(
        f"[done] rising articles: {len(rising_articles)}, "
        f"top {min(len(rising_articles), TOP_N)} written.",
        file=sys.stderr,
    )
    print(f" - {md_path}", file=sys.stderr)
    print(f" - {csv_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
