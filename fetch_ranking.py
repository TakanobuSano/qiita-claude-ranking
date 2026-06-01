#!/usr/bin/env python3
"""
Qiita 記事ランキング取得スクリプト。

Qiita API v2 を使って、claude / ClaudeCode / MCP タグの記事を直近14日間で取得し、
stocks_count 降順でランキング化して Markdown と CSV を出力する。

追加機能:
- 前回CSVと比較して、ストック数・いいね数の前回比を表示する
- Qiita記事本文にも「前日比 +N」を表示する
- CSVにも差分列を出力する

前提:
- output/ に過去の qiita_claude_ranking_YYYYMMDD.csv が残っていること
- 過去CSVがない場合は「比較データなし」として処理する
"""

from __future__ import annotations

import csv
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
import urllib.error
import urllib.request


# ===== 設定 =====

QIITA_API_BASE = "https://qiita.com/api/v2/items"

TARGET_TAGS = ["claude", "ClaudeCode", "MCP"]

LOOKBACK_DAYS = 14
PER_PAGE = 100
MAX_PAGES = 10
TOP_N = 20

TIMEOUT_SEC = 30
MAX_RETRY = 5

OUTPUT_DIR = Path("./output")
OUTPUT_FILE_PREFIX = "qiita_claude_ranking_"

JST = timezone(timedelta(hours=9))


# ===== .env 読み込み =====

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

    # 前回比
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
    Qiita API を叩いて (JSON, レスポンスヘッダ) を返す。
    429 / 5xx は指数バックオフで MAX_RETRY 回までリトライする。
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

    for a in articles:
        if not a.id:
            continue

        existing = seen.get(a.id)

        if existing is None or a.stocks_count > existing.stocks_count:
            seen[a.id] = a

    return list(seen.values())


def format_created_at(created_at: str) -> str:
    """
    Qiita API の created_at を、Qiita記事向けに読みやすい形式へ変換する。
    """
    if not created_at:
        return ""

    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H時投稿")
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

    例:
    今日が 20260601 の場合、
    qiita_claude_ranking_20260531.csv があればそれを使う。
    20260531 がなければ、20260530 など最新の過去CSVを使う。
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


def apply_deltas(top: list[Article], previous_snapshot: dict[str, dict[str, int]]) -> None:
    """
    現在のランキングに前回比を付与する。
    """
    for current_rank, article in enumerate(top, start=1):
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
    前回比の表示文字列を作る。
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
    since_date: str,
    today: str,
    total_unique: int,
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
    lines.append(f"- 対象期間: {since_date} 〜 {today}")
    lines.append(f"- 集計記事数: {total_unique} 件")

    if previous_csv_path:
        previous_date = previous_csv_path.stem.replace(OUTPUT_FILE_PREFIX, "")
        try:
            previous_dt = datetime.strptime(previous_date, "%Y%m%d")
            previous_text = previous_dt.strftime("%Y-%m-%d")
        except ValueError:
            previous_text = previous_date

        lines.append(f"- 前回比較: {previous_text} のCSVと比較")
    else:
        lines.append("- 前回比較: 比較データなし")

    lines.append(":::")
    lines.append("")

    if not top:
        lines.append(":::note info")
        lines.append("該当する記事が見つかりませんでした。")
        lines.append(":::")
        lines.append("")
        return "\n".join(lines)

    lines.append(":::note warn")
    lines.append(
        "このランキングは「直近14日間に投稿された記事の累計ストック数ランキング」です。"
        "前日比は、前回保存されたCSVとの差分です。"
    )
    lines.append(":::")
    lines.append("")

    for i, a in enumerate(top, 1):
        title = escape_markdown_text(a.title)
        user_id = a.user_id
        user_url = f"https://qiita.com/{user_id}" if user_id else ""
        created = format_created_at(a.created_at)

        tag_badges = " ".join(
            f"`{tag}`" for tag in a.tags[:5] if tag
        )

        stocks_delta_text = format_delta(a.stocks_delta)
        likes_delta_text = format_delta(a.likes_delta)

        lines.append(f"## {i}位 [{title}]({a.url})")
        lines.append("")

        user_part = (
            f"[{user_id}]({user_url}) さん"
            if user_id and user_url
            else "ユーザー不明"
        )

        lines.append(
            f"◇ **{a.stocks_count}ストック**（前日比 {stocks_delta_text}） "
            f"♡ **{a.likes_count}いいね**（前日比 {likes_delta_text}） / "
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


def write_csv(path: Path, top: list[Article]) -> None:
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

        for i, a in enumerate(top, 1):
            writer.writerow(
                [
                    i,
                    a.id,
                    a.title,
                    a.url,
                    a.user_id,
                    a.stocks_count,
                    "" if a.stocks_delta is None else a.stocks_delta,
                    a.likes_count,
                    "" if a.likes_delta is None else a.likes_delta,
                    "" if a.previous_rank is None else a.previous_rank,
                    "" if a.rank_delta is None else a.rank_delta,
                    a.comments_count,
                    a.page_views_count if a.page_views_count is not None else "",
                    a.created_at,
                    ",".join(a.tags),
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
    since = today_jst - timedelta(days=LOOKBACK_DAYS)

    since_date = since.isoformat()
    today_str = today_jst.isoformat()
    today_stamp = today_jst.strftime("%Y%m%d")
    updated_at = now_jst.strftime("%Y-%m-%d %H:%M:%S")

    print(
        f"[info] fetching tags={TARGET_TAGS} since {since_date} (JST)",
        file=sys.stderr,
    )

    all_articles: list[Article] = []

    for tag in TARGET_TAGS:
        try:
            fetched = fetch_tag(tag, since_date, token)
            print(f"[info] fetched tag={tag}: {len(fetched)} items", file=sys.stderr)
            all_articles.extend(fetched)
        except Exception as e:
            print(f"[error] failed to fetch tag={tag}: {e}", file=sys.stderr)
            continue

    unique = dedupe(all_articles)
    unique.sort(key=lambda a: (a.stocks_count, a.likes_count), reverse=True)

    top = unique[:TOP_N]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    previous_csv_path = find_previous_csv(today_stamp)
    previous_snapshot = load_previous_snapshot(previous_csv_path)
    apply_deltas(top, previous_snapshot)

    if previous_csv_path:
        print(f"[info] previous csv: {previous_csv_path}", file=sys.stderr)
    else:
        print("[info] previous csv: not found", file=sys.stderr)

    md_path = OUTPUT_DIR / f"{OUTPUT_FILE_PREFIX}{today_stamp}.md"
    csv_path = OUTPUT_DIR / f"{OUTPUT_FILE_PREFIX}{today_stamp}.csv"

    md_text = render_markdown(
        top=top,
        since_date=since_date,
        today=today_str,
        total_unique=len(unique),
        updated_at=updated_at,
        previous_csv_path=previous_csv_path,
    )

    md_path.write_text(md_text, encoding="utf-8")
    write_csv(csv_path, top)

    print(
        f"[done] unique articles: {len(unique)}, top {len(top)} written.",
        file=sys.stderr,
    )
    print(f" - {md_path}", file=sys.stderr)
    print(f" - {csv_path}", file=sys.stderr)

    if not top:
        print("[warn] no articles found in the period.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
