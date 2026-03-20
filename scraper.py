"""
新闻抓取 + 飞书推送 v5

核心逻辑：
1. 每天早上 8 点（北京时间）推送一次
2. 只抓过去 24 小时内发布的文章
3. 晚点用RSSHub抓取、量子位用官方RSS
4. RSS 有 content:encoded 全文的来源直接用，其余用 Jina 补全正文
5. Claude API 批量处理：过滤主题+广告、精简标题、3句摘要、重要性评分
6. 全局按重要性排序，每来源最多 5 条，总共推送 top 20
"""

import os
import re
import json
import hashlib
import time
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup

# ─── 配置 ──────────────────────────────────────────────────────────────────────

FEISHU_WEBHOOK    = os.environ.get("FEISHU_WEBHOOK", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_TOTAL         = 20      # 最终推送总条数上限
MAX_PER_SOURCE    = 5       # 单个来源最多推送条数
FETCH_LIMIT       = 10      # 每来源最多抓取条数
CLAUDE_BATCH      = 15      # Claude 每批处理条数
MAX_CONTENT_CHARS = 3000    # 传给 Claude 的正文最大字符数
JINA_TIMEOUT      = 25      # Jina Reader 超时秒数
HTTP_TIMEOUT      = 12      # 普通请求超时秒数
HOURS_LOOKBACK    = 24      # 只看过去多少小时的新闻

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

CONTENT_SELECTORS = [
    "article", "[itemprop='articleBody']", ".article-body",
    ".article-content", ".post-content", ".entry-content",
    ".content-body", ".story-body", ".article__body", ".news-content", "main",
]

# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc)

def is_within_24h(pub_date) -> bool:
    """判断发布时间是否在过去 24 小时内，解析失败时返回 True（保留）"""
    if not pub_date:
        return True
    try:
        if isinstance(pub_date, str):
            dt = parsedate_to_datetime(pub_date)
        else:
            dt = pub_date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return now_utc() - dt <= timedelta(hours=HOURS_LOOKBACK)
    except Exception:
        return True

def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()

def clean_html(html: str, max_len: int = MAX_CONTENT_CHARS) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:max_len] + "..." if len(text) > max_len else text

def http_get(url: str, timeout: int = HTTP_TIMEOUT):
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  GET {url[:70]} 失败: {e}")
        return None

def make_item(source: str, emoji: str, title: str, link: str,
              content: str = "", rss_summary: str = "") -> dict:
    return {
        "id":          make_id(link),
        "source":      source,
        "emoji":       emoji,
        "title":       title.strip(),
        "content":     content,
        "rss_summary": rss_summary,
        "summary":     "",
        "link":        link.strip(),
        "importance":  1,
    }

def jina_fetch(url: str) -> str:
    """用 Jina Reader 抓取任意 URL 的纯文本内容"""
    try:
        r = SESSION.get(
            f"https://r.jina.ai/{url}",
            timeout=JINA_TIMEOUT,
            headers={**HEADERS, "Accept": "text/plain", "X-Return-Format": "text"},
        )
        if r.status_code == 200 and len(r.text.strip()) > 200:
            return r.text.strip()
    except Exception as e:
        print(f"  Jina 失败 ({url[:60]}): {e}")
    return ""

# ─── 文章正文抓取 ──────────────────────────────────────────────────────────────

def fetch_article_content(url: str) -> str:
    """优先 Jina Reader，失败降级为 BeautifulSoup"""
    content = jina_fetch(url)
    if content:
        return content[:MAX_CONTENT_CHARS]

    # 降级：BeautifulSoup
    r = http_get(url)
    if not r:
        return ""
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup.select("nav,header,footer,aside,script,style,"
                            ".ad,.ads,.advertisement,.sponsored,.related,.sidebar"):
        tag.decompose()
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n").strip()
            text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
            if len(text) > 200:
                return text[:MAX_CONTENT_CHARS]
    body = soup.find("body")
    if body:
        text = body.get_text(separator="\n").strip()
        text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
        return text[:MAX_CONTENT_CHARS]
    return ""

def enrich_content(items: list) -> list:
    """对 content 为空的条目，用 Jina/BeautifulSoup 补全正文"""
    need = [it for it in items if not it["content"]]
    if not need:
        return items
    print(f"  补充抓取 {len(need)} 篇文章正文...")
    for it in need:
        content = fetch_article_content(it["link"])
        it["content"] = content if content else it["rss_summary"]
        time.sleep(1.0)
    return items

# ─── RSS 通用抓取 ──────────────────────────────────────────────────────────────

def from_rss(url: str, source: str, emoji: str) -> list:
    """
    解析 RSS，只保留 24 小时内的条目。
    优先取 content:encoded 全文；否则取 description，后续 enrich_content 补全。
    """
    try:
        feed = feedparser.parse(url)
        results = []
        for e in feed.entries[:FETCH_LIMIT * 2]:  # 多取一些，时间过滤后够用
            title = getattr(e, "title", "").strip()
            link  = getattr(e, "link",  "").strip()
            if not title or not link:
                continue

            # 24 小时过滤
            pub = getattr(e, "published", getattr(e, "updated", None))
            if not is_within_24h(pub):
                continue

            full_content = ""
            if hasattr(e, "content") and e.content:
                full_content = clean_html(e.content[0].get("value", ""))
            rss_summary = clean_html(
                getattr(e, "summary", getattr(e, "description", "")), max_len=500
            )
            results.append(make_item(source, emoji, title, link,
                                     content=full_content, rss_summary=rss_summary))
            if len(results) >= FETCH_LIMIT:
                break

        print(f"  [{source}] {len(results)} 条 (RSS, 24h内)")
        return results
    except Exception as e:
        print(f"  [{source}] RSS 失败: {e}")
        return []

# ─── 各来源解析器 ──────────────────────────────────────────────────────────────

def fetch_bbc():
    return from_rss("https://feeds.bbci.co.uk/zhongwen/trad/rss.xml", "BBC中文", "🌍")

def fetch_reuters():
    return from_rss("https://cn.reuters.com/rssfeed/topnews", "路透中文", "📡")

def fetch_latepost():
    try:
        r = SESSION.post("https://www.latepost.com/site/index", data={"page": 1, "limit": 5}, timeout=12)
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("data", []):
            title = item.get("title", "").strip()
            detail_url = item.get("detail_url", "")
            if not title or not detail_url:
                continue
            link = "https://www.latepost.com" + detail_url
            summary = item.get("abstract", "")
            results.append(make_item("晚点LatePost", "🌃", title, link, rss_summary=summary))
        print(f"  [晚点LatePost] {len(results)} 条 (API)")
        return results
    except Exception as e:
        print(f"  [晚点LatePost] API 失败: {e}")
        return []

def fetch_qbitai():
    return from_rss("https://www.qbitai.com/feed", "量子位", "⚛️")

def fetch_jiqizhixin():
    results = from_rss("https://www.jiqizhixin.com/rss", "机器之心", "🤖")
    if results:
        return results
    r = http_get("https://www.jiqizhixin.com")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    for a in soup.select("a[href*='/articles/']"):
        if len(results) >= FETCH_LIMIT:
            break
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.jiqizhixin.com" + href
        if href in seen_links:
            continue
        seen_links.add(href)
        title_text = a.get_text(strip=True)
        if len(title_text) < 5:
            continue
        results.append(make_item("机器之心", "🤖", title_text, href))
    print(f"  [机器之心] {len(results)} 条 (网页)")
    return results

def fetch_techcrunch():
    return from_rss("https://techcrunch.com/feed/", "TechCrunch", "🚀")

def fetch_wired():
    return from_rss("https://www.wired.com/feed/rss", "Wired", "🔌")

def fetch_theverge():
    return from_rss("https://www.theverge.com/rss/index.xml", "The Verge", "📱")

def fetch_mit():
    return from_rss("https://www.technologyreview.com/feed/", "MIT科技评论", "🔬")

def fetch_tmtpost():
    results = from_rss("https://www.tmtpost.com/rss", "钛媒体", "⚗️")
    if results:
        return results
    r = http_get("https://www.tmtpost.com")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    for a in soup.select("a[href*='/post/'], a[href*='/article/']"):
        if len(results) >= FETCH_LIMIT:
            break
        title_text = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.tmtpost.com" + href
        if href in seen_links or len(title_text) < 5:
            continue
        seen_links.add(href)
        results.append(make_item("钛媒体", "⚗️", title_text, href))
    print(f"  [钛媒体] {len(results)} 条 (网页)")
    return results

def fetch_36kr():
    results = from_rss("https://36kr.com/feed", "36Kr", "💎")
    if results:
        return results
    r = http_get("https://36kr.com")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    for a in soup.select("a[href*='/p/']"):
        if len(results) >= FETCH_LIMIT:
            break
        title_text = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = "https://36kr.com" + href
        if href in seen_links or len(title_text) < 8:
            continue
        seen_links.add(href)
        results.append(make_item("36Kr", "💎", title_text, href))
    print(f"  [36Kr] {len(results)} 条 (网页)")
    return results

def fetch_huxiu():
    results = from_rss("https://www.huxiu.com/rss/0.xml", "虎嗅", "🐯")
    if results:
        return results
    r = http_get("https://www.huxiu.com")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    for a in soup.select("a[href*='/article/']"):
        if len(results) >= FETCH_LIMIT:
            break
        title_text = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.huxiu.com" + href
        if href in seen_links or len(title_text) < 5:
            continue
        seen_links.add(href)
        results.append(make_item("虎嗅", "🐯", title_text, href))
    print(f"  [虎嗅] {len(results)} 条 (网页)")
    return results

def fetch_wallstreetcn():
    r = http_get("https://wallstreetcn.com")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    for sel in ["a[href*='/news/articles/']", "a[href*='/articles/']",
                ".article-title a", ".news-item a"]:
        for a in soup.select(sel):
            if len(results) >= FETCH_LIMIT:
                break
            title_text = a.get_text(strip=True)
            href = a.get("href", "")
            if not href or len(title_text) < 5:
                continue
            if not href.startswith("http"):
                href = "https://wallstreetcn.com" + href
            if href in seen_links:
                continue
            seen_links.add(href)
            results.append(make_item("华尔街见闻", "💹", title_text, href))
        if results:
            break
    print(f"  [华尔街见闻] {len(results)} 条")
    return results

def fetch_bloomberg():
    return from_rss("https://feeds.bloomberg.com/technology/news.rss", "Bloomberg", "📊")

def fetch_wsj():
    return from_rss("https://feeds.a.dj.com/rss/RSSWorldNews.xml", "WSJ", "🗞️")


# ─── Claude AI 处理 ────────────────────────────────────────────────────────────

CLAUDE_PROMPT = """\
你是一个专业新闻筛选助手，请处理以下新闻列表，每条新闻附有正文内容。

【筛选规则】
只保留以下三类（relevant: true）：
1. 科技：AI / 大模型 / 芯片 / 互联网产品 / 科技公司动态 / 网络安全
2. 资本市场：股市 / 投融资 / 并购 / IPO / 经济政策 / 央行 / 汇率
3. 地缘政治：国际关系 / 贸易摩擦 / 制裁 / 军事冲突 / 外交

以下一律过滤（relevant: false）：
- 广告 / 赞助内容（含 sponsored、promoted、广告、赞助等词）
- 娱乐 / 体育 / 健康 / 旅游 / 生活方式
- 标题或内容模糊、无实质信息

【处理要求（仅对 relevant: true）】
1. short_title：≤10 个中文字精简标题；繁体转简体。
2. summary_3：严格基于正文内容，3 句话总结，每句 15~40 字，中文，繁体转简体。
   - 必须来自正文，不得凭标题推测。
   - 正文为空或不足时，用 abstract 字段的内容作为 summary_3，如果 abstract 也为空则填：【正文不可用，请点击原文查看】
3. importance：AI / 大模型 / 芯片 / 量子计算 = 3；其他科技 / 网络安全 = 2；纯金融 / 纯地缘 = 1

新闻列表：
{batch_json}

只返回 JSON 数组，不含任何其他文字或代码块标记：
[{{"index":0,"relevant":true,"short_title":"精简标题","summary_3":"第一句。第二句。第三句。","importance":3}}]
"""

def process_with_claude(items: list) -> list:
    if not items:
        return []
    if not ANTHROPIC_API_KEY:
        print("⚠️  未设置 ANTHROPIC_API_KEY，跳过 AI 处理")
        return items

    processed = []
    total_batches = (len(items) + CLAUDE_BATCH - 1) // CLAUDE_BATCH

    for batch_num, i in enumerate(range(0, len(items), CLAUDE_BATCH), 1):
        batch = items[i : i + CLAUDE_BATCH]
        batch_data = [
            {
                "index":   j,
                "source":  it["source"],
                "title":   it["title"],
                "content": it["content"][:MAX_CONTENT_CHARS] if it["content"] else "",
                "abstract": it.get("rss_summary", ""),
            }
            for j, it in enumerate(batch)
        ]
        prompt = CLAUDE_PROMPT.format(
            batch_json=json.dumps(batch_data, ensure_ascii=False, indent=2)
        )
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 4000,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )
            resp.raise_for_status()
            raw = resp.json()["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            result_map = {r["index"]: r for r in json.loads(raw)}
            kept = 0
            for j, it in enumerate(batch):
                r = result_map.get(j, {})
                if not r.get("relevant", True):
                    continue
                it["title"]      = r.get("short_title", it["title"])
                it["summary"]    = r.get("summary_3",   "")
                it["importance"] = r.get("importance",  1)
                processed.append(it)
                kept += 1

            print(f"  Claude 批次 {batch_num}/{total_batches}：{len(batch)} 条 → 保留 {kept} 条")
            if batch_num < total_batches:
                time.sleep(1.5)

        except Exception as e:
            print(f"  Claude 批次 {batch_num} 失败: {e}，保留原始内容")
            processed.extend(batch)

    return processed

# ─── 排序 & 限制 ───────────────────────────────────────────────────────────────

def sort_and_limit(items: list) -> list:
    """
    全局按重要性降序排列，
    同时保证每个来源最多 MAX_PER_SOURCE 条，
    总数不超过 MAX_TOTAL。
    """
    items_sorted = sorted(items, key=lambda x: x.get("importance", 1), reverse=True)
    source_count: dict = {}
    result = []
    for it in items_sorted:
        src = it["source"]
        if source_count.get(src, 0) >= MAX_PER_SOURCE:
            continue
        source_count[src] = source_count.get(src, 0) + 1
        result.append(it)
        if len(result) >= MAX_TOTAL:
            break
    return result

# ─── 飞书推送 ──────────────────────────────────────────────────────────────────

def build_card(items: list) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 按来源分组，保持全局排序顺序
    groups: dict = {}
    for it in items:
        groups.setdefault(it["source"], []).append(it)

    elements = []
    for source, news_list in groups.items():
        emoji = news_list[0]["emoji"]
        elements.append({
            "tag":  "div",
            "text": {"tag": "lark_md", "content": f"**{emoji} {source}**"},
        })
        for n in news_list:
            content = f"· **[{n['title']}]({n['link']})**"
            if n["summary"]:
                content += f"\n{n['summary']}"
            elements.append({
                "tag":  "div",
                "text": {"tag": "lark_md", "content": content},
            })
        elements.append({"tag": "hr"})

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    elements.append({
        "tag":      "note",
        "elements": [{"tag": "plain_text",
                      "content": f"过去 24 小时 · 共 {len(items)} 条 · {now}"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": f"📰 每日新闻速递 · {now}"},
                "template": "wathet",
            },
            "elements": elements,
        },
    }

def send_to_feishu(items: list):
    if not FEISHU_WEBHOOK:
        print("⚠️  未设置 FEISHU_WEBHOOK，跳过推送")
        return
    if not items:
        print("无新内容，跳过推送")
        return
    payload = build_card(items)
    try:
        resp = SESSION.post(
            FEISHU_WEBHOOK, json=payload,
            headers={"Content-Type": "application/json"}, timeout=15,
        )
        data = resp.json()
        code = data.get("StatusCode", data.get("code", -1))
        if resp.status_code == 200 and code == 0:
            print(f"✅ 推送成功：{len(items)} 条")
        else:
            print(f"❌ 推送失败：{resp.status_code} {resp.text}")
    except Exception as e:
        print(f"❌ 推送异常：{e}")

# ─── 来源列表 ──────────────────────────────────────────────────────────────────

FETCHERS = [
    ("BBC中文",      fetch_bbc),
    ("路透中文",     fetch_reuters),
    ("晚点LatePost", fetch_latepost),
    ("量子位",       fetch_qbitai),
    ("机器之心",     fetch_jiqizhixin),
    ("TechCrunch",   fetch_techcrunch),
    ("Wired",        fetch_wired),
    ("The Verge",    fetch_theverge),
    ("MIT科技评论",  fetch_mit),
    ("钛媒体",       fetch_tmtpost),
    ("36Kr",         fetch_36kr),
    ("虎嗅",         fetch_huxiu),
    ("华尔街见闻",   fetch_wallstreetcn),
    ("Bloomberg",    fetch_bloomberg),
    ("WSJ",          fetch_wsj),
]

# ─── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"开始抓取 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"只保留过去 {HOURS_LOOKBACK} 小时内的新闻")
    print(f"{'='*55}")

    all_items = []

    # 1. 抓取各来源
    for name, fetcher in FETCHERS:
        try:
            results = fetcher()
            all_items.extend(results)
        except Exception as e:
            print(f"  [{name}] 异常: {e}")
        time.sleep(1.5)

    print(f"\n合计抓取 {len(all_items)} 条（24小时内）")

    if not all_items:
        print("无内容，跳过推送")
        return

    # 2. 补充抓取文章正文
    print("\n补充抓取文章正文（Jina Reader）...")
    all_items = enrich_content(all_items)

    # 3. Claude 处理
    print(f"\nClaude 处理中（{len(all_items)} 条）...")
    processed = process_with_claude(all_items)
    print(f"过滤后保留 {len(processed)} 条")

    # 4. 全局排序 + 限制条数
    final_items = sort_and_limit(processed)
    print(f"最终推送 {len(final_items)} 条（top {MAX_TOTAL}，每来源≤{MAX_PER_SOURCE}）")

    # 5. 推送飞书
    send_to_feishu(final_items)

    print("完成！\n")


if __name__ == "__main__":
    main()
