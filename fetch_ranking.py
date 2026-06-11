#!/usr/bin/env python3
"""
Qiita 記事ランキング取得スクリプト。

Qiita API v2 を使って、claude / ClaudeCode / MCP タグの記事を取得し、
Markdown と CSV を出力する。

主な機能:
- 急上昇ランキングの将来作成に備えて、直近30日間に投稿された対象タグ記事を取得する
- 記事IDで重複排除する
- CSVには、直近30日間に投稿された取得対象記事を全件保存する
- Markdownには、直近14日間に投稿された記事の累計ストック数ランキング上位20件のみ表示する
- 前回CSVと比較して、ストック数・いいね数の差分を算出する
- Qiita記事上では見やすさ優先で「（+54）」のように差分のみ表示する
"""

from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import urllib.error
import urllib.request


QIITA_API_BASE = "https://qiita.com/api/v2/items"

TARGET_TAGS = ["claude", "ClaudeCode", "MCP"]

# 通常ランキング:
# 直近14日間に投稿された記事の累計ストック数順。
RANKING_LOOKBACK_DAYS = 14

# CSV保存対象:
# 将来、直近30日間に投稿された記事を対象にした急上昇ランキングを作るため、
# 通常ランキングより広い期間で記事を取得・保存する。
DATA_LOOKBACK_DAYS = 30

PER_PAGE = 100
MAX_PAGES = 10
TOP_N = 20

TIMEOUT_SEC = 30
MAX_RETRY = 5

OUTPUT_DIR = Path("./output")
OUTPUT_FILE_PREFIX = "qiita_claude_ranking_"

JST = timezone(timedelta(hours=9))


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)

    if not p.exists():
        return

    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


@dataclass
class Article:
    id: str
    title: str
    url: str
    user_id: str
    likes_count: int
    stocks_count: int
    comments_count: int
    page_views_count: int | None
    created_at: str
    tags: list[str] = field(default_factory=list)

    stocks_delta: int | None = None
    likes_delta: int | None = None
    previous_rank: int | None = None
    rank_delta: int | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Article":
        return cls(
            id=raw.get("id", ""),
            title=raw.get("title", ""),
            url=raw.get("url", ""),
            user_id=(raw.get("user") or {}).get("id", ""),
            likes_count=raw.get("likes_count") or 0,
            stocks_count=raw.get("stocks_count") or 0,
            comments_count=raw.get("comments_count") or 0,
            page_views_count=raw.get("page_views_count"),
            created_at=raw.get("created_at", ""),
            tags=[t.get("name", "") for t in (raw.get("tags") or [])],
        )


def http_get_json(
    url: str,
    headers: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Qiita API を叩いて、JSON とレスポンスヘッダを返す。
    429 / 5xx は指数バックオフでリトライする。
    """
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRY + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8")

                import json as _json

                data = _json.loads(body) if body else []
                resp_headers = {k: v for k, v in resp.headers.items()}

                return data, resp_headers

        except urllib.error.HTTPError as e:
            last_err = e

            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 2**attempt
                )

                print(
                    f"[warn] 429 rate limit. waiting {wait}s "
                    f"(attempt {attempt}/{MAX_RETRY})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue

            if 500 <= e.code < 600:
                wait = 2**attempt
                print(
                    f"[warn] HTTP {e.code}. retry in {wait}s "
                    f"({attempt}/{MAX_RETRY})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue

            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass

            raise RuntimeError(
                f"Qiita API error {e.code}: {err_body or e.reason}"
            ) from e

        except urllib.error.URLError as e:
            last_err = e
            wait = 2**attempt
            print(
                f"[warn] network error: {e}. retry in {wait}s "
                f"({attempt}/{MAX_RETRY})",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

    raise RuntimeError(f"Qiita API request failed after {MAX_RETRY} retries: {last_err}")


def fetch_tag(tag: str, since_date: str, token: str | None) -> list[Article]:
    """
    1つのタグについて、created:>=since_date の記事をページングしながら取得する。
    """
    headers = {
        "User-Agent": "qiita-claude-ranking/1.0",
        "Accept": "application/json",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    articles: list[Article] = []

    for page in range(1, MAX_PAGES + 1):
        query = f"tag:{tag} created:>={since_date}"

        params = {
            "page": page,
            "per_page": PER_PAGE,
            "query": query,
        }

        url = f"{QIITA_API_BASE}?{urlencode(params)}"

        print(f"[info] GET tag={tag} page={page}", file=sys.stderr)

        data, resp_headers = http_get_json(url, headers)

        if not isinstance(data, list):
            print(
                f"[warn] unexpected response shape for tag={tag}: "
                f"{type(data).__name__}",
                file=sys.stderr,
            )
            break

        if not data:
            break

        for raw in data:
            try:
                articles.append(Article.from_api(raw))
            except Exception as e:
                print(f"[warn] skip malformed item: {e}", file=sys.stderr)

        remaining = (
            resp_headers.get("Rate-Limit-Remaining")
            or resp_headers.get("rate-limit-remaining")
        )

        if remaining is not None:
            try:
                if int(remaining) < 5:
                    print(
                        f"[info] rate-limit-remaining={remaining}, sleeping briefly",
                        file=sys.stderr,
                    )
                    time.sleep(2)
            except ValueError:
                pass

        if len(data) < PER_PAGE:
            break

        time.sleep(0.3)

    return articles


def dedupe(articles: Iterable[Article]) -> list[Article]:
    """
    複数タグで同じ記事が取得される可能性があるため、記事IDで重複排除する。
    """
    seen: dict[str, Article] = {}

    for article in articles:
        if not article.id:
            continue

        existing = seen.get(article.id)

        if existing is None or article.stocks_count > existing.stocks_count:
            seen[article.id] = article

    return list(seen.values())


def parse_created_date(created_at: str) -> date | None:
    """
    Qiita API の created_at を JST の日付に変換する。
    フィルタリングに使うため、変換できない場合は None を返す。
    """
    if not created_at:
        return None

    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(JST).date()
    except ValueError:
        return None


def filter_articles_by_created_date(
    articles: list[Article],
    since_date: date,
    today: date,
) -> list[Article]:
    """
    created_at が since_date 〜 today の範囲に入る記事だけを返す。
    入力 articles はすでに並び替え済みなので、並び順は維持する。
    """
    filtered: list[Article] = []

    for article in articles:
        created_date = parse_created_date(article.created_at)

        if created_date is None:
            continue

        if since_date <= created_date <= today:
            filtered.append(article)

    return filtered


def format_created_at(created_at: str) -> str:
    """
    Qiita API の created_at を、Qiita記事向けに読みやすい形式へ変換する。
    """
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
    """
    Markdownリンクの表示テキストで壊れやすい文字を簡易エスケープする。
    """
    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def find_previous_csv(today_stamp: str) -> Path | None:
    """
    output/ から、今日より前の最新CSVを探す。

    今日が 20260601 の場合、
    qiita_claude_ranking_20260531.csv があればそれを使う。
    なければ、20260530 など最新の過去CSVを使う。
    """
    if not OUTPUT_DIR.exists():
        return None

    candidates: list[tuple[str, Path]] = []

    for path in OUTPUT_DIR.glob(f"{OUTPUT_FILE_PREFIX}*.csv"):
        stem = path.stem

        if not stem.startswith(OUTPUT_FILE_PREFIX):
            continue

        stamp = stem.replace(OUTPUT_FILE_PREFIX, "")

        if not stamp.isdigit():
            continue

        if stamp < today_stamp:
            candidates.append((stamp, path))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def load_previous_snapshot(csv_path: Path | None) -> dict[str, dict[str, int]]:
    """
    前回CSVを読み込み、記事IDをキーにした辞書を作る。

    戻り値:
    {
        "記事ID": {
            "rank": 1,
            "stocks_count": 100,
            "likes_count": 120
        }
    }
    """
    if csv_path is None:
        return {}

    if not csv_path.exists():
        return {}

    snapshot: dict[str, dict[str, int]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            article_id = row.get("id", "")

            if not article_id:
                continue

            try:
                rank = int(row.get("rank") or 0)
            except ValueError:
                rank = 0

            try:
                stocks_count = int(row.get("stocks_count") or 0)
            except ValueError:
                stocks_count = 0

            try:
                likes_count = int(row.get("likes_count") or 0)
            except ValueError:
                likes_count = 0

            snapshot[article_id] = {
                "rank": rank,
                "stocks_count": stocks_count,
                "likes_count": likes_count,
            }

    return snapshot


def apply_deltas(articles: list[Article], previous_snapshot: dict[str, dict[str, int]]) -> None:
    """
    現在の記事リストに前回比を付与する。

    CSVを全件保存するため、直近30日間の unique 全件に対して差分を付ける。
    """
    for current_rank, article in enumerate(articles, start=1):
        previous = previous_snapshot.get(article.id)

        if previous is None:
            article.stocks_delta = None
            article.likes_delta = None
            article.previous_rank = None
            article.rank_delta = None
            continue

        article.stocks_delta = article.stocks_count - previous["stocks_count"]
        article.likes_delta = article.likes_count - previous["likes_count"]
        article.previous_rank = previous["rank"]

        if previous["rank"]:
            # 順位は数値が小さいほど上位。
            # 前回5位 → 今回2位なら +3 と表現する。
            article.rank_delta = previous["rank"] - current_rank
        else:
            article.rank_delta = None


def format_delta(delta: int | None) -> str:
    """
    Qiita記事本文で使う差分表示を作る。
    見やすさ優先のため、「前日比」「前回比」は本文には出さない。

    例:
    +54
    ±0
    -2
    新規
    """
    if delta is None:
        return "新規"

    if delta > 0:
        return f"+{delta}"

    if delta < 0:
        return str(delta)

    return "±0"


def render_markdown(
    top: list[Article],
    ranking_since_date: str,
    today: str,
    total_ranking_articles: int,
    updated_at: str,
    previous_csv_path: Path | None,
) -> str:
    lines: list[str] = []

    target_tags_text = ", ".join(f"`{tag}`" for tag in TARGET_TAGS)

    lines.append("")
    lines.append(":::note info")
    lines.append(f"最終更新: **{updated_at} JST**")
    lines.append("")
    lines.append(f"- 対象タグ: {target_tags_text}")
    lines.append(f"- ランキング対象期間: {ranking_since_date} 〜 {today}")
    lines.append(f"- 集計記事数: {total_ranking_articles} 件")
    if previous_csv_path:
        lines.append("- 比較: 前回更新時点との差分")
    else:
        lines.append("- 比較: なし")

    lines.append(":::")
    lines.append("")

    if not top:
        lines.append(":::note info")
        lines.append("該当する記事が見つかりませんでした。")
        lines.append(":::")
        lines.append("")
        return "\n".join(lines)

    for i, article in enumerate(top, 1):
        title = escape_markdown_text(article.title)
        user_id = article.user_id
        user_url = f"https://qiita.com/{user_id}" if user_id else ""
        created = format_created_at(article.created_at)

        tag_badges = " ".join(
            f"`{tag}`" for tag in article.tags[:5] if tag
        )

        stocks_delta_text = format_delta(article.stocks_delta)
        likes_delta_text = format_delta(article.likes_delta)

        lines.append(f"## {i}位 [{title}]({article.url})")
        lines.append("")

        user_part = (
            f"[{user_id}]({user_url}) さん"
            if user_id and user_url
            else "ユーザー不明"
        )

        lines.append(
            f"◇ **{article.stocks_count}ストック**（{stocks_delta_text}） "
            f"♡ **{article.likes_count}いいね**（{likes_delta_text}） / "
            f"{user_part} {created}"
        )

        lines.append("")

        if tag_badges:
            lines.append(tag_badges)
            lines.append("")

        if i != len(top):
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def write_csv(path: Path, articles: list[Article]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)

        writer.writerow(
            [
                "rank",
                "id",
                "title",
                "url",
                "user_id",
                "stocks_count",
                "stocks_delta",
                "likes_count",
                "likes_delta",
                "previous_rank",
                "rank_delta",
                "comments_count",
                "page_views_count",
                "created_at",
                "tags",
            ]
        )

        for i, article in enumerate(articles, 1):
            writer.writerow(
                [
                    i,
                    article.id,
                    article.title,
                    article.url,
                    article.user_id,
                    article.stocks_count,
                    "" if article.stocks_delta is None else article.stocks_delta,
                    article.likes_count,
                    "" if article.likes_delta is None else article.likes_delta,
                    "" if article.previous_rank is None else article.previous_rank,
                    "" if article.rank_delta is None else article.rank_delta,
                    article.comments_count,
                    article.page_views_count if article.page_views_count is not None else "",
                    article.created_at,
                    ",".join(article.tags),
                ]
            )


def main() -> int:
    load_dotenv(".env")

    token = os.environ.get("QIITA_ACCESS_TOKEN") or None

    if token:
        print("[info] using QIITA_ACCESS_TOKEN (authenticated mode)", file=sys.stderr)
    else:
        print("[info] no QIITA_ACCESS_TOKEN found; running unauthenticated", file=sys.stderr)

    now_jst = datetime.now(JST)
    today_jst = now_jst.date()

    ranking_since = today_jst - timedelta(days=RANKING_LOOKBACK_DAYS)
    data_since = today_jst - timedelta(days=DATA_LOOKBACK_DAYS)

    ranking_since_date = ranking_since.isoformat()
    data_since_date = data_since.isoformat()
    today_str = today_jst.isoformat()
    today_stamp = today_jst.strftime("%Y%m%d")
    updated_at = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    print(
        f"[info] fetching tags={TARGET_TAGS} since {data_since_date} (JST)",
        file=sys.stderr,
    )

    all_articles: list[Article] = []

    for tag in TARGET_TAGS:
        try:
            fetched = fetch_tag(tag, data_since_date, token)
            print(f"[info] fetched tag={tag}: {len(fetched)} items", file=sys.stderr)
            all_articles.extend(fetched)
        except Exception as e:
            print(f"[error] failed to fetch tag={tag}: {e}", file=sys.stderr)
            continue

    unique = dedupe(all_articles)
    unique.sort(key=lambda a: (a.stocks_count, a.likes_count), reverse=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    previous_csv_path = find_previous_csv(today_stamp)
    previous_snapshot = load_previous_snapshot(previous_csv_path)

    # CSVを全件保存するため、直近30日間の unique 全件に差分を付与する。
    apply_deltas(unique, previous_snapshot)

    # Markdownは、通常ランキングとして直近14日間に投稿された記事だけを対象にする。
    ranking_articles = filter_articles_by_created_date(
        articles=unique,
        since_date=ranking_since,
        today=today_jst,
    )
    top = ranking_articles[:TOP_N]

    if previous_csv_path:
        print(f"[info] previous csv: {previous_csv_path}", file=sys.stderr)
    else:
        print("[info] previous csv: not found", file=sys.stderr)

    md_path = OUTPUT_DIR / f"{OUTPUT_FILE_PREFIX}{today_stamp}.md"
    csv_path = OUTPUT_DIR / f"{OUTPUT_FILE_PREFIX}{today_stamp}.csv"

    md_text = render_markdown(
        top=top,
        ranking_since_date=ranking_since_date,
        today=today_str,
        total_ranking_articles=len(ranking_articles),
        updated_at=updated_at,
        previous_csv_path=previous_csv_path,
    )

    md_path.write_text(md_text, encoding="utf-8")

    # Markdownは直近14日間の上位20件のみ表示する。
    # CSVは将来の急上昇ランキング用に、直近30日間の記事を全件保存する。
    write_csv(csv_path, unique)

    print(
        f"[done] saved articles: {len(unique)}, "
        f"ranking articles: {len(ranking_articles)}, top {len(top)} written.",
        file=sys.stderr,
    )
    print(f" - {md_path}", file=sys.stderr)
    print(f" - {csv_path}", file=sys.stderr)

    if not top:
        print("[warn] no articles found in the ranking period.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
