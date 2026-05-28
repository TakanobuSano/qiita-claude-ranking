#!/usr/bin/env python3
"""
Qiita 記事ランキング取得スクリプト

Qiita API v2 を使って、claude / ClaudeCode タグの記事を直近7日間で取得し、
stocks_count 降順でランキング化して Markdown と CSV を出力する。

- 認証: .env の QIITA_ACCESS_TOKEN (なければ非認証)
- 出力: ./output/qiita_claude_ranking_YYYYMMDD.md / .csv
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

import urllib.request
import urllib.error


# ===== 設定 =====
QIITA_API_BASE = "https://qiita.com/api/v2/items"
TARGET_TAGS = ["claude", "ClaudeCode"]
LOOKBACK_DAYS = 7
PER_PAGE = 100  # Qiita API の上限
MAX_PAGES = 10  # 念のための安全装置 (最大 100 * 10 = 1000 件)
TOP_N = 20
TIMEOUT_SEC = 30
MAX_RETRY = 5
OUTPUT_DIR = Path("./output")
JST = timezone(timedelta(hours=9))  # Qiita は日本のサービスなので JST 基準で日付を扱う


# ===== .env 読み込み (依存ゼロの簡易ローダー) =====
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
        # 既存環境変数は上書きしない
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


def http_get_json(url: str, headers: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Qiita API を叩いて (JSON, レスポンスヘッダ) を返す。
    429 / 5xx は指数バックオフで MAX_RETRY 回までリトライ。
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
            # 429 はレートリミット。Retry-After ヘッダがあれば従う
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
                print(f"  [warn] 429 rate limit. waiting {wait}s (attempt {attempt}/{MAX_RETRY})", file=sys.stderr)
                time.sleep(wait)
                continue
            # 5xx は一時的エラーとしてリトライ
            if 500 <= e.code < 600:
                wait = 2 ** attempt
                print(f"  [warn] HTTP {e.code}. retry in {wait}s ({attempt}/{MAX_RETRY})", file=sys.stderr)
                time.sleep(wait)
                continue
            # 4xx (429 以外) は即座に投げる
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"Qiita API error {e.code}: {err_body or e.reason}") from e
        except urllib.error.URLError as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [warn] network error: {e}. retry in {wait}s ({attempt}/{MAX_RETRY})", file=sys.stderr)
            time.sleep(wait)
            continue

    raise RuntimeError(f"Qiita API request failed after {MAX_RETRY} retries: {last_err}")


def fetch_tag(tag: str, since_date: str, token: str | None) -> list[Article]:
    """
    1 つのタグについて、created:>=since_date の記事をページングしながら全件取得。
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
        params = {"page": page, "per_page": PER_PAGE, "query": query}
        url = f"{QIITA_API_BASE}?{urlencode(params)}"
        print(f"  GET tag={tag} page={page}", file=sys.stderr)

        data, resp_headers = http_get_json(url, headers)

        if not isinstance(data, list):
            print(f"  [warn] unexpected response shape for tag={tag}: {type(data).__name__}", file=sys.stderr)
            break

        if not data:
            # 空ページに到達 → 取得完了
            break

        for raw in data:
            try:
                articles.append(Article.from_api(raw))
            except Exception as e:
                print(f"  [warn] skip malformed item: {e}", file=sys.stderr)

        # レートリミット情報を残しておく
        remaining = resp_headers.get("Rate-Limit-Remaining") or resp_headers.get("rate-limit-remaining")
        if remaining is not None:
            try:
                if int(remaining) < 5:
                    print(f"  [info] rate-limit-remaining={remaining}, sleeping briefly", file=sys.stderr)
                    time.sleep(2)
            except ValueError:
                pass

        # per_page 未満で返ってきたら最終ページ
        if len(data) < PER_PAGE:
            break

        # マナーとして軽くウェイト
        time.sleep(0.3)

    return articles


def dedupe(articles: Iterable[Article]) -> list[Article]:
    seen: dict[str, Article] = {}
    for a in articles:
        if not a.id:
            continue
        # 重複時は stocks_count が大きい方 (最新値を取りたい) を残す
        existing = seen.get(a.id)
        if existing is None or a.stocks_count > existing.stocks_count:
            seen[a.id] = a
    return list(seen.values())


def format_created_at(created_at: str) -> str:
    """
    Qiita API の created_at を、Qiita記事向けに読みやすい形式へ変換する。
    例: 2026-05-24T16:00:00+09:00 -> 2026-05-24 16時投稿
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


def render_markdown(top: list[Article], since_date: str, today: str, total_unique: int) -> str:
    lines: list[str] = []

    lines.append("# Qiita Claude関連タグ 週間ストック数ランキング")
    lines.append("")
    lines.append(f"- 対象タグ: {', '.join(TARGET_TAGS)}")
    lines.append(f"- 対象期間: {since_date} 〜 {today}")
    lines.append(f"- 集計記事数: {total_unique} 件")
    lines.append("- ランキング基準: ストック数順")
    lines.append("")
    lines.append("> 注意: このランキングは「直近7日間に投稿された記事の累計ストック数ランキング」です。")
    lines.append("> 「この1週間で増えたストック数ランキング」ではありません。")
    lines.append("")

    if not top:
        lines.append("該当する記事が見つかりませんでした。")
        lines.append("")
        return "\n".join(lines)

    for i, a in enumerate(top, 1):
        title = escape_markdown_text(a.title)
        user_id = a.user_id
        user_url = f"https://qiita.com/{user_id}" if user_id else ""
        created = format_created_at(a.created_at)

        tag_badges = " ".join(
            f"`{tag}`" for tag in a.tags[:5] if tag
        )

        lines.append(f"## {i}位 [{title}]({a.url})")
        lines.append("")
        lines.append(
            f"**{a.stocks_count}ストック**　"
            f"**{a.likes_count}いいね**　/　"
            f"[{user_id}]({user_url}) さん {created}"
        )
        lines.append("")

        if tag_badges:
            lines.append(tag_badges)
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def write_csv(path: Path, top: list[Article]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rank", "id", "title", "url", "user_id",
            "stocks_count", "likes_count", "comments_count",
            "page_views_count", "created_at", "tags",
        ])
        for i, a in enumerate(top, 1):
            writer.writerow([
                i, a.id, a.title, a.url, a.user_id,
                a.stocks_count, a.likes_count, a.comments_count,
                a.page_views_count if a.page_views_count is not None else "",
                a.created_at, ",".join(a.tags),
            ])


def main() -> int:
    load_dotenv(".env")
    token = os.environ.get("QIITA_ACCESS_TOKEN") or None

    if token:
        print("[info] using QIITA_ACCESS_TOKEN (authenticated mode)", file=sys.stderr)
    else:
        print("[info] no QIITA_ACCESS_TOKEN found; running unauthenticated", file=sys.stderr)

    today_jst = datetime.now(JST).date()
    since = today_jst - timedelta(days=LOOKBACK_DAYS)
    since_date = since.isoformat()
    today_str = today_jst.isoformat()

    print(f"[info] fetching tags={TARGET_TAGS} since {since_date} (JST)", file=sys.stderr)

    all_articles: list[Article] = []
    for tag in TARGET_TAGS:
        try:
            fetched = fetch_tag(tag, since_date, token)
            print(f"  fetched tag={tag}: {len(fetched)} items", file=sys.stderr)
            all_articles.extend(fetched)
        except Exception as e:
            print(f"[error] failed to fetch tag={tag}: {e}", file=sys.stderr)
            # 他タグは続行 (片方失敗しても部分結果を出力する)
            continue

    unique = dedupe(all_articles)
    unique.sort(key=lambda a: (a.stocks_count, a.likes_count), reverse=True)
    top = unique[:TOP_N]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = today_jst.strftime("%Y%m%d")
    md_path = OUTPUT_DIR / f"qiita_claude_ranking_{stamp}.md"
    csv_path = OUTPUT_DIR / f"qiita_claude_ranking_{stamp}.csv"

    md_text = render_markdown(top, since_date, today_str, len(unique))
    md_path.write_text(md_text, encoding="utf-8")
    write_csv(csv_path, top)

    print(f"[done] unique articles: {len(unique)}, top {len(top)} written.", file=sys.stderr)
    print(f"  - {md_path}", file=sys.stderr)
    print(f"  - {csv_path}", file=sys.stderr)

    if not top:
        print("[warn] no articles found in the period.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
