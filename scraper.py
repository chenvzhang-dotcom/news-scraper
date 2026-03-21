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

FEISHU_WEBHOOK             = os.environ.get("FEISHU_WEBHOOK", "")
ANTHROPIC_API_KEY          = os.environ.get("ANTHROPIC_API_KEY", "")
FEISHU_WEBHOOK_EARNINGS    = os.environ.get("FEISHU_WEBHOOK_EARNINGS", "")
EARNINGSWHISPERS_EMAIL     = os.environ.get("EARNINGSWHISPERS_EMAIL", "")
EARNINGSWHISPERS_PASSWORD  = os.environ.get("EARNINGSWHISPERS_PASSWORD", "")

JINSA_STATE_FILE = "jinsa_sent.json"

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


# ══════════════════════════════════════════════════════════════════════════════
# 通用飞书发送（支持指定 Webhook）
# ══════════════════════════════════════════════════════════════════════════════

def feishu_send(webhook: str, payload: dict, label: str = "") -> bool:
    if not webhook:
        print(f"⚠️  未设置 Webhook，跳过 {label}")
        return False
    try:
        resp = SESSION.post(webhook, json=payload,
                            headers={"Content-Type": "application/json"}, timeout=15)
        data = resp.json()
        code = data.get("StatusCode", data.get("code", -1))
        ok = resp.status_code == 200 and code == 0
        print(f"{'✅' if ok else '❌'} {label} {'推送成功' if ok else f'失败: {resp.text[:120]}'}")
        return ok
    except Exception as e:
        print(f"❌ {label} 推送异常: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# JINSA 每日战情推送
# ══════════════════════════════════════════════════════════════════════════════

def jinsa_load_state() -> dict:
    try:
        if os.path.exists(JINSA_STATE_FILE):
            with open(JINSA_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"sent": []}


def jinsa_save_state(state: dict):
    try:
        with open(JINSA_STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  保存 JINSA 状态失败: {e}")


def jinsa_find_pdf():
    """从 JINSA 首页找最新的 Operations 每日更新 PDF，返回 (url, date_str)"""
    r = http_get("https://jinsa.org")
    if not r:
        return "", ""
    soup = BeautifulSoup(r.text, "lxml")
    pat = re.compile(r"Operations.{1,5}Epic.{1,5}Fury", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pat.search(href) and ".pdf" in href:
            fname = href.split("/")[-1].replace(".pdf", "")
            date_part = re.sub(
                r"Operations[-.]Epic[-.]Fury[-.]and[-.]Roaring[-.]Lion[-.]?",
                "", fname, flags=re.IGNORECASE
            ).strip("-").strip(".")
            return href, date_part
    return "", ""


def jinsa_extract_pdf(url: str) -> str:
    """直接下载 PDF 并用 pdfplumber 提取文本"""
    try:
        import pdfplumber, io
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            pages = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        text = "\n".join(pages)
        print(f"  PDF 提取成功，共 {len(text)} 字符")
        return text
    except Exception as e:
        print(f"  PDF 提取失败: {e}")
        return ""



JINSA_PROMPT = """\
从以下 JINSA 战报中提取所有涉及具体数字的信息，按分类整理，每条单独一行。
格式：「分类 | 描述：数字」
分类只能是：伊朗武器发射 / 美以打击 / 伤亡统计 / 能源经济 / 其他
只输出数据行，不要解释说明，繁体转简体，输出中文。

{text}
"""


def jinsa_numbers_via_claude(text: str) -> str:
    if not ANTHROPIC_API_KEY or not text:
        return ""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000,
                  "messages": [{"role": "user",
                                "content": JINSA_PROMPT.format(text=text[:10000])}]},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  Claude 提取数字失败: {e}")
        return ""


def jinsa_build_card(numbers: str, pdf_url: str, date_str: str) -> dict:
    bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    cats = {
        "伊朗武器发射": ("🚀", []),
        "美以打击":     ("🎯", []),
        "伤亡统计":     ("💀", []),
        "能源经济":     ("⛽", []),
        "其他":         ("📊", []),
    }
    for line in numbers.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        cat_raw, content = line.split("|", 1)
        matched = next((k for k in cats if k in cat_raw.strip()), "其他")
        cats[matched][1].append(f"· {content.strip()}")

    elements = []
    for cat, (emoji, lines) in cats.items():
        if not lines:
            continue
        elements.append({
            "tag":  "div",
            "text": {"tag": "lark_md", "content": f"**{emoji} {cat}**"},
        })
        elements.append({
            "tag":  "div",
            "text": {"tag": "lark_md", "content": "\n".join(lines)},
        })
        elements.append({"tag": "hr"})
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    if not elements:
        elements.append({
            "tag":  "div",
            "text": {"tag": "lark_md", "content": "（本次未提取到数字内容）"},
        })

    elements.append({
        "tag":  "div",
        "text": {"tag": "lark_md", "content": f"[查看原文 PDF]({pdf_url})"},
    })
    elements.append({
        "tag":      "note",
        "elements": [{"tag": "plain_text",
                      "content": f"JINSA · {date_str} · 北京时间 {bj}"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text",
                             "content": f"📊 JINSA 战情数字 · {date_str}"},
                "template": "wathet",
            },
            "elements": elements,
        },
    }


def main_jinsa():
    print(f"\n{'='*50}")
    print(f"JINSA 检查 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    pdf_url, date_str = jinsa_find_pdf()
    if not pdf_url:
        print("未找到 JINSA 每日更新 PDF，跳过")
        return
    print(f"  找到 PDF: {pdf_url}")

    state = jinsa_load_state()
    if pdf_url in state["sent"]:
        print("  今日 PDF 已推送过，跳过")
        return

    print("  下载并解析 PDF...")
    text = jinsa_extract_pdf(pdf_url)
    if not text:
        print("  文本提取失败，跳过")
        return

    print("  Claude 提取数字...")
    numbers = jinsa_numbers_via_claude(text)
    if not numbers:
        print("  数字提取失败，跳过")
        return

    payload = jinsa_build_card(numbers, pdf_url, date_str)
    if feishu_send(FEISHU_WEBHOOK, payload, "JINSA"):
        state["sent"].append(pdf_url)
        state["sent"] = state["sent"][-60:]
        jinsa_save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
# 每周业绩日历推送
# ══════════════════════════════════════════════════════════════════════════════

TECH_SECTORS  = {"Technology", "Communication Services"}
TECH_KEYWORDS = {"tech", "software", "semiconductor", "internet", "digital",
                 "cloud", "data", "ai", "cyber", "payment", "streaming",
                 "social", "gaming", "fintech"}

WEEKDAY_ZH = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五"}

HK_WATCH = [
    {"company": "阿里巴巴", "ticker": "9988.HK",
     "ir_url":  "https://www.alibabagroup.com/en-US/ir/results",
     "market_cap_approx": 300_000_000_000, "sector": "电商/云计算"},
    {"company": "腾讯控股", "ticker": "0700.HK",
     "ir_url":  "https://www.tencent.com/en-us/ir/financial-news.html",
     "market_cap_approx": 530_000_000_000, "sector": "游戏/社交/云"},
]


def is_tech_co(sector: str, industry: str) -> bool:
    if sector in TECH_SECTORS:
        return True
    combined = f"{sector} {industry}".lower()
    return any(kw in combined for kw in TECH_KEYWORDS)


def ew_login():
    """登录 Earnings Whispers，返回已登录的 Session；失败返回 None"""
    if not EARNINGSWHISPERS_EMAIL or not EARNINGSWHISPERS_PASSWORD:
        print("  未设置 EW 账号密码")
        return None
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        r0 = s.get("https://earningswhispers.com/", timeout=15)
        soup0 = BeautifulSoup(r0.text, "lxml")

        form_data = {
            "Email":      EARNINGSWHISPERS_EMAIL,
            "Password":   EARNINGSWHISPERS_PASSWORD,
            "RememberMe": "true",
        }
        tok = soup0.find("input", {"name": "__RequestVerificationToken"})
        if tok:
            form_data["__RequestVerificationToken"] = tok.get("value", "")

        r1 = s.post("https://earningswhispers.com/Account/SignIn",
                    data=form_data, timeout=15, allow_redirects=True)

        logged_in = ("signout" in r1.text.lower() or
                     r1.url.rstrip("/") != "https://earningswhispers.com/Account/SignIn")
        if logged_in:
            print("  EW 登录成功")
            return s
        print("  EW 登录失败（检查密码或页面结构是否变化）")
        return None
    except Exception as e:
        print(f"  EW 登录异常: {e}")
        return None


def ew_scrape_day(session, date_str: str) -> list:
    """
    抓取 Earnings Whispers 某天的公司列表。
    date_str 格式：20260323
    """
    results = []
    urls = [
        f"https://earningswhispers.com/calendar/{date_str}",
        f"https://old.earningswhispers.com/calendar?sb=p&d={date_str}&t=all",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")

            # 尝试多种 CSS 选择器（实际结构需根据页面调整）
            rows = (soup.select(".stockrow, .earnings-row, tr[data-ticker]") or
                    soup.select("li[data-ticker], div[data-ticker]"))

            for row in rows:
                # 获取 ticker
                ticker = row.get("data-ticker", "")
                if not ticker:
                    el = row.select_one(".ticker, .symbol")
                    ticker = el.get_text(strip=True) if el else ""
                if not ticker:
                    continue

                name_el = row.select_one(".company, .cname, .companyname")
                time_el = row.select_one(".time, .when, [class*='bmo'], [class*='amc']")
                call_el = row.select_one(".calltime, .call")

                name     = name_el.get_text(strip=True) if name_el else ticker
                time_raw = time_el.get_text(strip=True).upper() if time_el else ""
                time_badge = ("BMO" if "BMO" in time_raw or "BEFORE" in time_raw
                              else "AMC" if "AMC" in time_raw or "AFTER" in time_raw
                              else "")
                call_time = call_el.get_text(strip=True) if call_el else ""

                results.append({
                    "ticker":    ticker.upper(),
                    "company":   name,
                    "date":      f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
                    "time":      time_badge,
                    "call_time": call_time,
                    "source":    "EW",
                })

            if results:
                print(f"    {date_str}：找到 {len(results)} 家")
                break

        except Exception as e:
            print(f"    {date_str} 抓取失败: {e}")

    return results


def yf_get_info(ticker: str) -> dict:
    """用 yfinance 获取市值、行业、分析师覆盖数"""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return {
            "market_cap": info.get("marketCap", 0) or 0,
            "sector":     info.get("sector", ""),
            "industry":   info.get("industry", ""),
            "analysts":   info.get("numberOfAnalystOpinions", 0) or 0,
            "name":       info.get("shortName", "") or "",
        }
    except Exception:
        return {"market_cap": 0, "sector": "", "industry": "", "analysts": 0, "name": ""}


def filter_companies(companies: list) -> list:
    """筛选：科技相关 + 市值 ≥ $5B + 分析师 ≥ 5"""
    result = []
    print(f"  筛选 {len(companies)} 家...")
    for co in companies:
        info = yf_get_info(co["ticker"])
        if info["market_cap"] < 5_000_000_000:
            continue
        if not is_tech_co(info["sector"], info["industry"]):
            continue
        if info["analysts"] < 5:
            continue
        co.update({k: info[k] for k in ("market_cap", "sector", "industry")})
        if info["name"]:
            co["company"] = info["name"]
        result.append(co)
        time.sleep(0.3)
    return result


def fmt_cap(v: int) -> str:
    if v >= 1_000_000_000_000:
        return f"${v/1e12:.1f}T"
    if v >= 1_000_000_000:
        return f"${v/1e9:.0f}B"
    return f"${v/1e6:.0f}M"


def hk_check_ir(hk: dict):
    """
    检查港股 IR 页面，查找近 60 天内的业绩发布日期。
    返回含 date 字段的 dict，或 None（近期无公告）。
    """
    r = http_get(hk["ir_url"])
    if not r:
        return None

    text = r.text
    bj_now = datetime.now(timezone(timedelta(hours=8)))

    # 找页面中结果公告附近的日期
    date_pat = re.compile(
        r"(?:results?\s+(?:announcement|release|date)|interim|annual\s+results?)"
        r".{0,150}?(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\w+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE | re.DOTALL,
    )
    for m in date_pat.finditer(text):
        raw = m.group(1).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y",
                    "%B %d %Y",  "%b %d %Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                delta = (dt - bj_now.replace(tzinfo=None))
                if timedelta(0) <= delta <= timedelta(days=60):
                    return dict(hk,
                                date=dt.strftime("%Y-%m-%d"),
                                confirmed=True, time="", call_time="待定")
            except ValueError:
                continue

    # 未找到确切日期，仍显示占位（让用户知道系统在追踪）
    return dict(hk, date="待确认", confirmed=False, time="", call_time="待定")


def get_next_week_dates():
    """返回下周一到周五的日期列表（YYYYMMDD）及周描述"""
    bj = datetime.now(timezone(timedelta(hours=8)))
    offset = (7 - bj.weekday()) % 7 or 7
    mon = bj + timedelta(days=offset)
    dates = [(mon + timedelta(days=i)).strftime("%Y%m%d") for i in range(5)]
    week_str = (f"{mon.strftime('%m月%d日')} — "
                f"{(mon + timedelta(days=4)).strftime('%m月%d日')}")
    return dates, week_str


def build_earnings_card(us_cos: list, hk_cos: list, week_str: str) -> dict:
    bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    all_cos = us_cos + [dict(c, source="HK") for c in hk_cos if c]

    elements = []

    if not all_cos:
        elements.append({"tag": "div",
                          "text": {"tag": "lark_md",
                                   "content": "本周无符合条件的科技公司业绩发布。"}})
    else:
        # 按日期分组
        by_date = {}
        for co in all_cos:
            by_date.setdefault(co.get("date", "待确认"), []).append(co)

        confirmed = sorted(k for k in by_date if k != "待确认")
        pending   = ["待确认"] if "待确认" in by_date else []

        # 收集有公司的日期，用于后面生成备注
        dates_with_cos = set(confirmed)

        for dk in confirmed + pending:
            if dk != "待确认":
                try:
                    d = datetime.strptime(dk, "%Y-%m-%d")
                    label = f"**{WEEKDAY_ZH.get(d.weekday(), '')} · {d.strftime('%m月%d日')}**"
                except Exception:
                    label = f"**{dk}**"
            else:
                label = "**日期待确认**"

            elements.append({"tag": "div",
                              "text": {"tag": "lark_md", "content": label}})

            for co in by_date[dk]:
                is_hk  = co.get("source") == "HK"
                ticker = co["ticker"]
                name   = co.get("company", ticker)
                badges = ""
                if co.get("time"):
                    badges += f" `{co['time']}`"
                if not co.get("confirmed", True):
                    badges += " `待确认`"

                mkt    = "港股" if is_hk else "美股"
                cap    = fmt_cap(co.get("market_cap") or co.get("market_cap_approx", 0))
                sector = co.get("sector", "")

                extras = []
                call_t = co.get("call_time", "")
                if call_t and call_t not in ("", "待定"):
                    extras.append(f"电话会：{call_t}")
                if co.get("ir_url"):
                    extras.append(f"[IR 页面]({co['ir_url']})")

                line2 = f"{mkt} · {cap} · {sector}"
                if extras:
                    line2 += f"\n{' · '.join(extras)}"

                content = f"**{name}** `{ticker}`{badges}\n{line2}"
                elements.append({"tag": "div",
                                  "text": {"tag": "lark_md", "content": content}})

            elements.append({"tag": "hr"})

        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

        # 生成空白日备注
        bj_obj = datetime.now(timezone(timedelta(hours=8)))
        offset = (7 - bj_obj.weekday()) % 7 or 7
        mon    = bj_obj + timedelta(days=offset)
        empty_days = []
        for i in range(5):
            d  = (mon + timedelta(days=i))
            dk = d.strftime("%Y-%m-%d")
            if dk not in dates_with_cos:
                empty_days.append(WEEKDAY_ZH.get(d.weekday(), ""))
        if empty_days:
            note_msg = f"{'、'.join(empty_days)}无符合条件的科技公司发布业绩。"
        else:
            note_msg = ""

    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
        "content": "  ".join(filter(None, [
            "BMO=盘前 · AMC=盘后",
            note_msg if all_cos else "",
            f"更新于 {bj}",
            "来源：Earnings Whispers / IR 官网",
        ]))}]})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text",
                             "content": f"📅 下周业绩日历 · {week_str}"},
                "template": "green",
            },
            "elements": elements,
        },
    }


def main_earnings():
    print(f"\n{'='*50}")
    print(f"业绩日历推送 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    week_dates, week_str = get_next_week_dates()
    print(f"  目标周：{week_str}")

    # 1. 登录 Earnings Whispers 并抓取美股
    print("\n登录 Earnings Whispers...")
    ew_session = ew_login() or SESSION

    us_raw = []
    print("抓取美股业绩日历...")
    for d in week_dates:
        us_raw.extend(ew_scrape_day(ew_session, d))
        time.sleep(1.0)
    print(f"共抓到 {len(us_raw)} 家原始数据")

    us_cos = []
    if us_raw:
        print("筛选科技公司...")
        us_cos = filter_companies(us_raw)
        print(f"筛选后保留 {len(us_cos)} 家")

    # 2. 港股 IR 检查（阿里 + 腾讯）
    print("\n检查港股 IR...")
    hk_cos = []
    for hk in HK_WATCH:
        print(f"  {hk['company']}...")
        res = hk_check_ir(hk)
        if res:
            hk_cos.append(res)
        time.sleep(1.0)

    # 3. 构建并推送
    payload = build_earnings_card(us_cos, hk_cos, week_str)
    feishu_send(FEISHU_WEBHOOK_EARNINGS, payload, "业绩日历")

    print("\n完成！\n")


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "news"
    if mode == "jinsa":
        main_jinsa()
    elif mode == "earnings":
        main_earnings()
    else:
        main()
