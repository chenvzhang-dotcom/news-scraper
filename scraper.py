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
API_NINJAS_KEY             = os.environ.get("API_NINJAS_KEY", "")

JINSA_STATE_FILE           = "jinsa_sent.json"
EARNINGS_SCHEDULE_FILE     = "earnings_schedule.json"

MAX_TOTAL         = 30      # 最终推送总条数上限
MAX_PER_SOURCE    = 5       # 单个来源最多推送条数
FETCH_LIMIT       = 10      # 每来源最多抓取条数
CLAUDE_BATCH      = 8       # Claude 每批处理条数
MAX_CONTENT_CHARS = 1500    # 传给 Claude 的正文最大字符数
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
    link = re.sub(r'\s+', '', link.strip())  # 清除 URL 中的换行/空格
    return {
        "id":          make_id(link),
        "source":      source,
        "emoji":       emoji,
        "title":       title.strip(),
        "content":     content,
        "rss_summary": rss_summary,
        "summary":     "",
        "link":        link,
        "importance":  1,
    }

def make_ai_item(source: str, emoji: str, title: str, link: str,
                 content: str = "", rss_summary: str = "") -> dict:
    """AI 公司官网来源，默认 importance=2，确保不被老源挤掉"""
    item = make_item(source, emoji, title, link, content, rss_summary)
    item["importance"] = 2
    return item

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


# ─── AI 公司官网新闻源 ─────────────────────────────────────────────────────────

def _extract_pub_date(url: str):
    """
    从文章页面提取发布时间，返回 datetime 或 None。
    优先级：
    1. <meta property="article:published_time">
    2. <time datetime="...">
    3. JSON-LD datePublished
    """
    r = http_get(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")

    # 1. Open Graph meta
    meta = soup.find("meta", property="article:published_time")
    if meta and meta.get("content"):
        try:
            from dateutil import parser as dateparser
            return dateparser.parse(meta["content"])
        except Exception:
            pass

    # 2. <time datetime="">
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        try:
            from dateutil import parser as dateparser
            return dateparser.parse(time_tag["datetime"])
        except Exception:
            pass

    # 3. JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            pub = data.get("datePublished") or data.get("dateCreated")
            if pub:
                from dateutil import parser as dateparser
                return dateparser.parse(pub)
        except Exception:
            pass

    return None


def _scrape_blog(url: str, source: str, emoji: str,
                 must_contain: str = "", ai_source: bool = False) -> list:
    """
    通用博客页面爬取：
    1. Jina Reader 获取页面，解析文章链接列表
    2. 逐篇进入文章页面提取发布时间
    3. 24小时内 → 保留；超过24小时 → 停止（假设列表按时间倒序）
    4. 降级：BeautifulSoup 直接解析链接
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    _make = make_ai_item if ai_source else make_item

    # ── 第一步：拿链接列表 ──
    candidates = []
    seen_links = set()

    content = jina_fetch(url)
    if not content:
        print(f"  [{source}] Jina 返回空，尝试 BeautifulSoup 降级")
        r = http_get(url)
        if not r:
            print(f"  [{source}] BeautifulSoup 降级也失败（HTTP 错误）")
            return []
        soup = BeautifulSoup(r.text, "lxml")
        all_as = soup.find_all("a", href=True)
        print(f"  [{source}] BeautifulSoup 找到 {len(all_as)} 个 <a> 标签")
        for a in all_as:
            href = a["href"]
            if not href.startswith("http"):
                href = origin + href
            if must_contain and must_contain not in href:
                continue
            if href in seen_links or href == url:
                continue
            title = a.get_text(strip=True)
            if len(title) < 8:
                continue
            seen_links.add(href)
            candidates.append((title, href))
            if len(candidates) >= FETCH_LIMIT * 2:
                break
    else:
        all_links = re.findall(r'\[([^\]]{5,120})\]\((https?://[^\)\s\"\']+)\)', content)
        print(f"  [{source}] Jina 找到 {len(all_links)} 个链接，过滤条件: '{must_contain}'")
        for title, link in all_links:
            link = re.sub(r'\s+', '', link).rstrip(")")
            if must_contain and must_contain not in link:
                continue
            if link in seen_links or link == url:
                continue
            seen_links.add(link)
            candidates.append((title, link))
            if len(candidates) >= FETCH_LIMIT * 2:
                break

    print(f"  [{source}] 过滤后候选链接 {len(candidates)} 个，开始逐篇检查发布时间...")

    # ── 第二步：逐篇检查发布时间 ──
    items = []
    for title, link in candidates:
        pub_date = _extract_pub_date(link)
        if pub_date is None:
            # 拿不到时间就保留（宁可多推）
            print(f"    无法获取日期，保留: {link[:60]}")
            items.append(_make(source, emoji, title, link))
        elif is_within_24h(pub_date):
            print(f"    ✅ 24h内: {link[:60]}")
            items.append(_make(source, emoji, title, link))
        else:
            print(f"    ⏹ 超过24h，停止: {link[:60]}")
            break  # 假设列表按时间倒序，后面的更旧
        time.sleep(0.5)
        if len(items) >= FETCH_LIMIT:
            break

    print(f"  [{source}] 最终 {len(items)} 条")
    return items


# ── 第一批：有官方 RSS ──────────────────────────────────────────────────────────

def fetch_openai_blog():
    return from_rss("https://openai.com/news/rss.xml", "OpenAI", "🤖")

def fetch_deepmind_blog():
    results = from_rss("https://deepmind.google/blog/rss.xml", "DeepMind", "🧠")
    if not results:
        results = _scrape_blog(
            "https://deepmind.google/blog/",
            "DeepMind", "🧠", must_contain="deepmind.google/blog/", ai_source=True
        )
    return results

def fetch_huggingface_blog():
    return from_rss("https://huggingface.co/blog/feed.xml", "HuggingFace", "🤗")

def fetch_github_changelog():
    results = from_rss("https://github.blog/changelog/feed/", "GitHub Changelog", "🐙")
    if not results:
        results = _scrape_blog(
            "https://github.blog/changelog/",
            "GitHub Changelog", "🐙", must_contain="github.blog", ai_source=True
        )
    return results

def fetch_aws_ml_blog():
    results = from_rss("https://aws.amazon.com/blogs/machine-learning/feed/", "AWS ML", "☁️")
    if not results:
        results = from_rss("https://aws.amazon.com/blogs/machine-learning/feed", "AWS ML", "☁️")
    return results

def fetch_langchain_blog():
    results = from_rss("https://blog.langchain.dev/rss/", "LangChain", "⛓️")
    if not results:
        results = from_rss("https://blog.langchain.dev/feed/", "LangChain", "⛓️")
    if not results:
        results = _scrape_blog(
            "https://blog.langchain.dev/",
            "LangChain", "⛓️", must_contain="blog.langchain.dev", ai_source=True
        )
    return results

def fetch_meta_ai_blog():
    results = _scrape_blog(
        "https://ai.meta.com/blog/",
        "Meta AI", "🌐", must_contain="ai.meta.com/blog", ai_source=True
    )
    return results

def fetch_microsoft_ai_blog():
    results = from_rss("https://blogs.microsoft.com/ai/feed/", "Microsoft AI", "🪟")
    if not results:
        results = _scrape_blog(
            "https://blogs.microsoft.com/ai/",
            "Microsoft AI", "🪟", must_contain="blogs.microsoft.com/ai", ai_source=True
        )
    return results

def fetch_replit_blog():
    results = from_rss("https://blog.replit.com/rss", "Replit", "💻")
    if not results:
        results = from_rss("https://blog.replit.com/feed", "Replit", "💻")
    if not results:
        results = _scrape_blog(
            "https://blog.replit.com/",
            "Replit", "💻", must_contain="blog.replit.com", ai_source=True
        )
    return results


# ── 第二批：无 RSS，爬取页面 ───────────────────────────────────────────────────

def fetch_anthropic_news():
    return _scrape_blog(
        "https://www.anthropic.com/news",
        "Anthropic", "🧬", must_contain="anthropic.com/news", ai_source=True
    )

def fetch_xai_blog():
    content = jina_fetch("https://x.ai/blog")
    if not content:
        print("  [xAI] Jina 返回空")
        return []
    items, seen = [], set()
    all_links = re.findall(r'\[([^\]]{5,120})\]\((https?://[^\)\s\"\']+)\)', content)
    print(f"  [xAI] Jina 找到 {len(all_links)} 个链接")
    for title, link in all_links:
        link = re.sub(r'\s+', '', link).rstrip(")")
        if "x.ai/blog" not in link:
            continue
        if link in seen:
            continue
        seen.add(link)
        items.append(make_ai_item("xAI", "⚡", title.strip(), link))
        if len(items) >= FETCH_LIMIT:
            break
    print(f"  [xAI] 最终 {len(items)} 条")
    return items

def fetch_mistral_news():
    return _scrape_blog(
        "https://mistral.ai/news/",
        "Mistral AI", "💫", must_contain="mistral.ai", ai_source=True
    )

def fetch_cursor_blog():
    return _scrape_blog(
        "https://www.cursor.com/blog",
        "Cursor", "🖱️", must_contain="cursor.com/blog", ai_source=True
    )


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
                time.sleep(20)

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
    # ── AI 公司官网（优先处理，确保进入 Claude 前几批）────────────────────────
    ("OpenAI",           fetch_openai_blog),
    ("Anthropic",        fetch_anthropic_news),
    ("DeepMind",         fetch_deepmind_blog),
    ("xAI",              fetch_xai_blog),
    ("Mistral AI",       fetch_mistral_news),
    ("HuggingFace",      fetch_huggingface_blog),
    ("GitHub Changelog", fetch_github_changelog),
    ("Meta AI",          fetch_meta_ai_blog),
    ("Microsoft AI",     fetch_microsoft_ai_blog),
    ("AWS ML",           fetch_aws_ml_blog),
    ("LangChain",        fetch_langchain_blog),
    ("Replit",           fetch_replit_blog),
    ("Cursor",           fetch_cursor_blog),
    # ── 综合科技与财经 ────────────────────────────────────────────────────────
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
        print(f"{'✅' if ok else '❌'} {label} {'推送成功' if ok else f'失败: {resp.text[:300]}'}")
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
        "伊朗武器发射": ("【导弹】", []),
        "美以打击":     ("【打击】", []),
        "伤亡统计":     ("【伤亡】", []),
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
        for line in lines:
            elements.append({
                "tag":  "div",
                "text": {"tag": "lark_md", "content": line},
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
                             "content": f"JINSA 战情数字 · {date_str}"},
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
# 每两周业绩日历推送
# ══════════════════════════════════════════════════════════════════════════════

WEEKDAY_ZH = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五"}

# 美股白名单（tier: 1=重点, 2=关注）
US_WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "AMZN", "TSLA", "AVGO", "ORCL",
    "AMD", "INTC", "MU", "MRVL",
    "CRM", "DDOG",
    "NFLX", "UBER", "ABNB", "SNAP", "RDDT",
    "ASML", "SMCI",
    "RBLX", "LITE", "COHR", "SNDK", "HOOD", "NOK", "PDD", "BABA", "TCEHY", "APP",
    "PYPL", "SQ",
]
US_TIER1 = {
    "MSFT", "NVDA", "GOOG", "META", "AMZN",
    "AMD", "MU", "LITE", "SNDK", "BABA",
}

# 港股 + 韩股白名单（tier: 1=重点, 2=关注）
INTL_WATCHLIST = [
    {"company": "腾讯控股",  "ticker": "0700.HK",   "market": "港股", "freq": "季报",   "tier": 2},
    {"company": "阿里巴巴",  "ticker": "9988.HK",   "market": "港股", "freq": "季报",   "tier": 2},
    {"company": "小米集团",  "ticker": "1810.HK",   "market": "港股", "freq": "季报",   "tier": 2},
    {"company": "快手",      "ticker": "1024.HK",   "market": "港股", "freq": "季报",   "tier": 1},
    {"company": "泡泡玛特",  "ticker": "9992.HK",   "market": "港股", "freq": "半年报", "tier": 2},
    {"company": "MiniMax",   "ticker": "00100.HK",  "market": "港股", "freq": "季报",   "tier": 1},
    {"company": "智谱",      "ticker": "02513.HK",  "market": "港股", "freq": "季报",   "tier": 1},
    {"company": "壁仞科技",  "ticker": "06082.HK",  "market": "港股", "freq": "季报",   "tier": 1},
    {"company": "三星电子",  "ticker": "005930.KS", "market": "韩股", "freq": "季报",   "tier": 1},
    {"company": "SK 海力士", "ticker": "000660.KS", "market": "韩股", "freq": "季报",   "tier": 1},
]


CURRENCY_SYMBOL = {"HKD": "HK$", "KRW": "₩", "USD": "$"}

def fmt_cap(v: int, currency: str = "USD") -> str:
    """按原始货币显示市值，加正确货币符号"""
    sym = CURRENCY_SYMBOL.get(currency, "$")
    if v >= 1_000_000_000_000:
        return f"{sym}{v/1e12:.1f}T"
    if v >= 1_000_000_000:
        return f"{sym}{v/1e9:.0f}B"
    if v > 0:
        return f"{sym}{v/1e6:.0f}M"
    return ""


def yf_fetch_earnings(tickers: list, target_dates: set) -> list:
    """批量查美股白名单，筛出 target_dates 范围内有业绩的公司"""
    import yfinance as yf
    results = []
    print(f"  批量查询 {len(tickers)} 家美股...")
    yf_tickers = yf.Tickers(" ".join(tickers))

    for ticker in tickers:
        try:
            t = yf_tickers.tickers.get(ticker)
            if not t:
                continue
            cal = t.calendar
            if not cal or "Earnings Date" not in cal:
                continue
            dates = cal["Earnings Date"]
            if not isinstance(dates, list):
                dates = [dates]
            for d in dates:
                d_str = str(d)[:10]
                if d_str not in target_dates:
                    continue
                info       = t.info
                market_cap = info.get("marketCap", 0) or 0
                currency   = info.get("currency", "USD") or "USD"
                sector     = info.get("sector", "") or ""
                name       = info.get("shortName", ticker) or ticker
                results.append({
                    "ticker":     ticker,
                    "company":    name,
                    "date":       d_str,
                    "time":       "",
                    "market_cap": market_cap,
                    "currency":   currency,
                    "sector":     sector,
                    "market":     "美股",
                    "confirmed":  True,
                    "tier":       1 if ticker in US_TIER1 else 2,
                })
                break
        except Exception:
            continue

    print(f"  美股找到 {len(results)} 家")
    return results


def yf_fetch_intl_earnings(watchlist: list, target_dates: set) -> list:
    """
    查港股/韩股白名单。
    - ticker 为待确认的跳过
    - 找不到日期或日期不在窗口内的，直接不显示
    """
    import yfinance as yf
    results = []
    valid = [w for w in watchlist if w["ticker"] != "待确认"]

    for w in valid:
        try:
            t = yf.Ticker(w["ticker"])
            cal = t.calendar
            if not cal or "Earnings Date" not in cal:
                continue
            dates = cal["Earnings Date"]
            if not isinstance(dates, list):
                dates = [dates]
            for d in dates:
                d_str = str(d)[:10]
                if d_str in target_dates:
                    info       = t.info
                    market_cap = info.get("marketCap", 0) or 0
                    currency   = info.get("currency", "USD") or "USD"
                    results.append(dict(w,
                        date=d_str,
                        confirmed=True,
                        time="",
                        market_cap=market_cap,
                        currency=currency,
                    ))
                    break
        except Exception:
            continue

    print(f"  港韩股找到 {len(results)} 家（有确认日期）")
    return results


def get_next_two_weeks_dates():
    """返回未来 14 天的日期集合（YYYY-MM-DD）及描述"""
    bj    = datetime.now(timezone(timedelta(hours=8)))
    today = bj.date()
    dates = {str(today + timedelta(days=i)) for i in range(1, 15)}
    start = today + timedelta(days=1)
    end   = today + timedelta(days=14)
    week_str = f"{start.strftime('%m月%d日')} — {end.strftime('%m月%d日')}"
    return dates, week_str


def build_earnings_card(us_cos: list, intl_cos: list, week_str: str) -> dict:
    bj      = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    all_cos = us_cos + intl_cos
    elements = []

    # 按日期分组，只保留有确认日期的
    by_date = {}
    for co in all_cos:
        dk = co.get("date", "")
        if dk:
            by_date.setdefault(dk, []).append(co)

    for dk in sorted(by_date):
        try:
            d     = datetime.strptime(dk, "%Y-%m-%d")
            label = f"**{WEEKDAY_ZH.get(d.weekday(), '')} · {d.strftime('%m月%d日')}**"
        except Exception:
            label = f"**{dk}**"

        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": label}})

        for co in by_date[dk]:
            ticker   = co["ticker"]
            name     = co.get("company", ticker)
            market   = co.get("market", "美股")
            time_tag = f" `{co['time']}`" if co.get("time") else ""
            cap      = fmt_cap(co.get("market_cap", 0), co.get("currency", "USD"))
            sector   = co.get("sector", "") or co.get("freq", "")

            # ticker 颜色：美股蓝，港股绿，韩股紫
            ticker_color = "blue" if market == "美股" else "green" if market == "港股" else "purple"
            ticker_colored = f"<font color='{ticker_color}'>{ticker}</font>"

            # 第一行：公司名 ticker · 市场 · 市值 · 行业
            tier_badge = "⭐ " if co.get("tier") == 1 else ""
            parts = [market]
            if cap:
                parts.append(cap)
            if sector:
                parts.append(sector)
            meta = " · ".join(parts)
            line1 = f"{tier_badge}**{name}** {ticker_colored}  {meta}"

            # 第二行：业绩发布日期
            try:
                date_display = datetime.strptime(dk, "%Y-%m-%d").strftime("%m月%d日")
            except Exception:
                date_display = dk
            timing = f" {co['time']}" if co.get("time") else ""
            line2 = f"业绩发布：{date_display}{timing}"

            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line1}})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": line2}})

        elements.append({"tag": "hr"})

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                          "content": "未来两周内无业绩发布。"}})

    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
        "content": f"BMO=盘前 · AMC=盘后  更新于 {bj}  来源：yfinance / Yahoo Finance"}]})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text",
                             "content": f"下两周业绩日历 · {week_str}"},
                "template": "green",
            },
            "elements": elements,
        },
    }


def main_earnings():
    print(f"\n{'='*50}")
    print(f"业绩日历推送 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    target_dates, week_str = get_next_two_weeks_dates()
    print(f"  目标范围：{week_str}")

    print("\n查询美股白名单...")
    us_cos = yf_fetch_earnings(US_WATCHLIST, target_dates)

    print("\n查询港韩股白名单...")
    intl_cos = yf_fetch_intl_earnings(INTL_WATCHLIST, target_dates)

    payload = build_earnings_card(us_cos, intl_cos, week_str)
    feishu_send(FEISHU_WEBHOOK_EARNINGS, payload, "业绩日历")

    # 保存 Tier 1 公司的业绩日程，供轮询任务使用
    save_earnings_schedule(us_cos, intl_cos)

    print("\n完成！\n")


# ══════════════════════════════════════════════════════════════════════════════
# 4. 业绩快报 + 电话会纪要（Tier 1 实时轮询）
# ══════════════════════════════════════════════════════════════════════════════

# ─── 默认轮询起始时间（UTC）─────────────────────────────────────────────────
# BMO（盘前）→ UTC 10:00 | AMC（盘后）→ UTC 20:30 | 未知 → BMO 窗口
POLL_START_UTC = {"BMO": 10, "AMC": 20, "": 10}
POLL_TIMEOUT_HOURS = 6

# ─── 日程管理 ──────────────────────────────────────────────────────────────

def save_earnings_schedule(us_cos: list, intl_cos: list):
    """从双周日历结果中提取 Tier 1 公司，写入 earnings_schedule.json"""
    # 加载已有日程（保留尚未完成的条目）
    schedule = load_earnings_schedule()

    tier1 = [c for c in us_cos + intl_cos if c.get("tier") == 1]
    for co in tier1:
        ticker = co["ticker"]
        date_str = co["date"]
        time_tag = (co.get("time") or "").upper()  # BMO / AMC / ""

        # 计算轮询开始时间（UTC）
        hour = POLL_START_UTC.get(time_tag, 10)
        poll_start = f"{date_str}T{hour:02d}:00:00Z"

        # 如果这个 ticker+date 已经处理完了，跳过
        key = f"{ticker}_{date_str}"
        existing = schedule.get(key, {})
        if existing.get("earnings_status") == "done":
            continue

        schedule[key] = {
            "ticker":             ticker,
            "company":            co.get("company", ticker),
            "date":               date_str,
            "time_tag":           time_tag,
            "poll_start_utc":     poll_start,
            "market":             co.get("market", "美股"),
            "earnings_status":    existing.get("earnings_status", "pending"),
        }

    with open(EARNINGS_SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, ensure_ascii=False, indent=2)
    print(f"  📅 已保存 {len(tier1)} 家 Tier 1 公司的业绩日程")


def load_earnings_schedule() -> dict:
    try:
        with open(EARNINGS_SCHEDULE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_schedule_entry(key: str, updates: dict):
    schedule = load_earnings_schedule()
    if key in schedule:
        schedule[key].update(updates)
        with open(EARNINGS_SCHEDULE_FILE, "w") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)


# ─── yfinance 业绩数据查询 ─────────────────────────────────────────────────

def yf_check_earnings_released(ticker: str, expected_date: str):
    """
    检查某家公司是否已发布业绩（通过 earnings_dates 的 Reported EPS 是否有值）。
    返回 {actual_eps, estimated_eps, surprise_pct} 或 None（尚未发布）。
    """
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return None
        # 在 earnings_dates 里找匹配日期的行
        for idx, row in ed.iterrows():
            row_date = str(idx.date()) if hasattr(idx, 'date') else str(idx)[:10]
            if row_date != expected_date:
                continue
            reported = row.get("Reported EPS")
            if reported is None or (hasattr(reported, '__float__') and str(reported) == 'nan'):
                return None  # 还没发布
            return {
                "actual_eps":    float(reported),
                "estimated_eps": float(row.get("EPS Estimate", 0) or 0),
                "surprise_pct":  float(row.get("Surprise(%)", 0) or 0),
            }
        return None
    except Exception as e:
        print(f"  ⚠️ yfinance earnings_dates 查询失败 ({ticker}): {e}")
        return None


def yf_get_quarterly_financials(ticker: str):
    """获取最新一季度的关键财务数据"""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        qi = t.quarterly_income_stmt
        if qi is None or qi.empty:
            return None
        latest = qi.iloc[:, 0]  # 最新一季度
        prev   = qi.iloc[:, 1] if qi.shape[1] > 1 else None  # 上一季度（用于环比）

        def safe_get(series, key):
            v = series.get(key)
            if v is not None and str(v) != 'nan':
                return float(v)
            return None

        result = {
            "period":          str(qi.columns[0].date()) if hasattr(qi.columns[0], 'date') else str(qi.columns[0]),
            "revenue":         safe_get(latest, "Total Revenue"),
            "gross_profit":    safe_get(latest, "Gross Profit"),
            "operating_income": safe_get(latest, "Operating Income"),
            "net_income":      safe_get(latest, "Net Income"),
            "ebitda":          safe_get(latest, "EBITDA"),
            "eps":             safe_get(latest, "Diluted EPS"),
            "basic_eps":       safe_get(latest, "Basic EPS"),
            "r_and_d":         safe_get(latest, "Research And Development"),
        }
        if result["revenue"] and result["gross_profit"]:
            result["gross_margin"] = round(result["gross_profit"] / result["revenue"] * 100, 1)
        if result["revenue"] and result["operating_income"]:
            result["operating_margin"] = round(result["operating_income"] / result["revenue"] * 100, 1)

        # 上季度营收（用于计算环比）
        if prev is not None:
            prev_rev = safe_get(prev, "Total Revenue")
            if prev_rev and result["revenue"]:
                result["revenue_qoq"] = round((result["revenue"] / prev_rev - 1) * 100, 1)

        return result
    except Exception as e:
        print(f"  ⚠️ yfinance quarterly_income_stmt 查询失败 ({ticker}): {e}")
        return None


# ─── Claude 生成 ──────────────────────────────────────────────────────────

EARNINGS_SUMMARY_PROMPT = """\
你是一位专业的股票分析师。请根据以下财务数据，为 {company} ({ticker}) 生成一份简洁的业绩快报。

## 数据

### EPS（每股收益）
- 实际 EPS: {actual_eps}
- 预期 EPS: {estimated_eps}
- Surprise: {surprise_pct}%

### 最新季度财务数据
{financials_json}

## 要求
请用中文输出，格式如下：

**核心数据**
- 营收：金额（环比变化，如数据可得）
- EPS：实际值 vs 预期值（beat/miss）
- 毛利率、经营利润率（如数据可得）

**业绩亮点**（3-5 条要点）

**业绩点评**（2-3 段，从投资者视角分析这份财报意味着什么，关注增长趋势、利润率变化、和未来展望）

保持简洁专业，每个要点一行。数据缺失则跳过，不要编造。金额用 B（十亿）或 M（百万）为单位。
"""



def claude_generate(prompt: str):
    """调用 Claude API 生成内容"""
    if not ANTHROPIC_API_KEY:
        print("⚠️  未设置 ANTHROPIC_API_KEY")
        return None
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
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  ❌ Claude API 调用失败: {e}")
        return None


# ─── 飞书卡片构建 ──────────────────────────────────────────────────────────

def build_earnings_alert_card(company: str, ticker: str, market: str, summary: str) -> dict:
    """构建业绩快报飞书卡片"""
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": f"⭐ 业绩快报 · {company} ({ticker})"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                    "content": f"{market} · 来源：yfinance + Claude · {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')} 北京时间"}]},
            ],
        },
    }




# ─── 主函数：业绩快报轮询 ─────────────────────────────────────────────────

def main_earnings_alert():
    """轮询 Tier 1 公司业绩数据，生成快报并推送"""
    now_utc = datetime.now(timezone.utc)
    print(f"\n{'='*50}")
    print(f"业绩快报轮询 · {now_utc.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}")

    schedule = load_earnings_schedule()
    if not schedule:
        print("  无日程，退出")
        return

    for key, entry in schedule.items():
        status = entry.get("earnings_status", "pending")
        if status != "pending":
            continue

        # 检查是否到了轮询开始时间
        poll_start = datetime.fromisoformat(entry["poll_start_utc"].replace("Z", "+00:00"))
        if now_utc < poll_start:
            print(f"  ⏳ {entry['ticker']} 未到轮询时间（{entry['poll_start_utc']}）")
            continue

        # 检查是否超时
        hours_elapsed = (now_utc - poll_start).total_seconds() / 3600
        if hours_elapsed > POLL_TIMEOUT_HOURS:
            print(f"  ⏰ {entry['ticker']} 超时（>{POLL_TIMEOUT_HOURS}h），标记 timeout")
            update_schedule_entry(key, {"earnings_status": "timeout"})
            continue

        ticker = entry["ticker"]
        company = entry["company"]
        market = entry.get("market", "美股")
        print(f"\n  🔍 查询 {company} ({ticker}) ...")

        # 用 yfinance 检查是否已发布业绩
        surprise = yf_check_earnings_released(ticker, entry["date"])
        if not surprise:
            print(f"  ❌ 尚未发布（Reported EPS 为空）")
            continue

        print(f"  ✅ 找到业绩数据！EPS actual={surprise['actual_eps']} vs est={surprise['estimated_eps']} (surprise={surprise['surprise_pct']}%)")

        # 拉取完整季度财务数据
        financials = yf_get_quarterly_financials(ticker)

        # Claude 生成业绩快报
        prompt = EARNINGS_SUMMARY_PROMPT.format(
            company=company,
            ticker=ticker,
            actual_eps=surprise["actual_eps"],
            estimated_eps=surprise["estimated_eps"],
            surprise_pct=surprise["surprise_pct"],
            financials_json=json.dumps(financials, ensure_ascii=False, indent=2) if financials else "无数据",
        )
        summary = claude_generate(prompt)
        if not summary:
            print(f"  ❌ Claude 生成失败，下次重试")
            continue

        # 推送飞书
        card = build_earnings_alert_card(company, ticker, market, summary)
        if feishu_send(FEISHU_WEBHOOK_EARNINGS, card, f"业绩快报·{ticker}"):
            update_schedule_entry(key, {"earnings_status": "done"})
            print(f"  ✅ {ticker} 业绩快报已推送")

    print("\n轮询完成\n")


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
    elif mode in ("earnings-alert", "earnings-poll"):
        main_earnings_alert()
    else:
        main()
