"""
新闻抓取 + 飞书推送
来源：BBC中文、路透中文、晚点LatePost、机器之心、爱意若、
      TechCrunch、Wired、The Verge、MIT Technology Review、
      钛媒体、36Kr、虎嗅、华尔街见闻、Bloomberg、WSJ、FT
推送：飞书富文本卡片，每天 08:00 / 12:00 / 18:00
所有内容统一翻译为中文后推送
"""

import os
import json
import hashlib
import time
import feedparser
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

# ─── 配置 ──────────────────────────────────────────────────────────────────────
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
SEEN_FILE = "seen_ids.json"
MAX_PER_SOURCE = 5

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

# 标记为英文的来源，需要翻译
ENGLISH_SOURCES = {
    "TechCrunch", "Wired", "The Verge", "MIT科技评论",
    "Bloomberg", "WSJ", "FT", "BBC中文", "路透中文",
}


# ─── 翻译 ──────────────────────────────────────────────────────────────────────

_translator = GoogleTranslator(source="auto", target="zh-CN")


def translate(text: str) -> str:
    """翻译单条文本，失败时返回原文"""
    if not text or not text.strip():
        return text
    try:
        # GoogleTranslator 单次上限 5000 字符
        return _translator.translate(text[:4000])
    except Exception as e:
        print(f"    翻译失败，保留原文: {e}")
        return text


def translate_item(item: dict) -> dict:
    """如果来源是英文，翻译标题和摘要"""
    if item["source"] not in ENGLISH_SOURCES:
        return item
    item["title"]   = translate(item["title"])
    item["summary"] = translate(item["summary"]) if item["summary"] else ""
    return item


# ─── 工具 ──────────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False, indent=2)


def make_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def clean(text: str, max_len: int = 100) -> str:
    if not text:
        return ""
    text = BeautifulSoup(text, "html.parser").get_text(separator=" ").strip()
    text = " ".join(text.split())
    return text[:max_len] + "…" if len(text) > max_len else text


def http_get(url: str, timeout: int = 12):
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  GET {url[:60]} 失败: {e}")
        return None


def make_item(source: str, emoji: str, title: str, link: str, summary: str = "") -> dict:
    return {
        "id":      make_id(link),
        "source":  source,
        "emoji":   emoji,
        "title":   title.strip(),
        "summary": clean(summary),
        "link":    link.strip(),
    }


def from_rss(url: str, source: str, emoji: str) -> list:
    try:
        feed = feedparser.parse(url)
        results = []
        for e in feed.entries[:MAX_PER_SOURCE]:
            title   = getattr(e, "title",   "").strip()
            link    = getattr(e, "link",    "").strip()
            summary = getattr(e, "summary", getattr(e, "description", ""))
            if title and link:
                results.append(make_item(source, emoji, title, link, summary))
        print(f"  [{source}] {len(results)} 条 (RSS)")
        return results
    except Exception as e:
        print(f"  [{source}] RSS 失败: {e}")
        return []


# ─── 各来源解析器 ──────────────────────────────────────────────────────────────

def fetch_bbc():
    return from_rss("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "BBC中文", "🌍")

def fetch_reuters():
    return from_rss("https://cn.reuters.com/rssfeed/topnews", "路透中文", "📡")

def fetch_latepost():
    r = http_get("https://www.latepost.com")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    cards = (soup.select(".article-item") or soup.select("article")
             or soup.select("[class*='article']"))
    for card in cards:
        if len(results) >= MAX_PER_SOURCE:
            break
        h = card.find(["h1","h2","h3","h4"])
        a = (h.find("a", href=True) if h else None) or card.find("a", href=True)
        if not a:
            continue
        title_text = a.get_text(strip=True)
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.latepost.com" + href
        if href in seen_links or len(title_text) < 5:
            continue
        seen_links.add(href)
        p = card.find("p")
        results.append(make_item("晚点LatePost", "🌃", title_text, href,
                                 p.get_text(strip=True) if p else ""))
    if not results:
        for a in soup.select("a[href]"):
            if len(results) >= MAX_PER_SOURCE:
                break
            href = a["href"]
            if not any(kw in href for kw in ["/news/","/post/","/article/","/story/"]):
                continue
            if not href.startswith("http"):
                href = "https://www.latepost.com" + href
            if href in seen_links:
                continue
            title_text = a.get_text(strip=True)
            if len(title_text) < 8:
                continue
            seen_links.add(href)
            results.append(make_item("晚点LatePost", "🌃", title_text, href))
    print(f"  [晚点LatePost] {len(results)} 条")
    return results

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
        if len(results) >= MAX_PER_SOURCE:
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

def fetch_aiera():
    r = http_get("https://aiera.com.cn/")
    if not r:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    results, seen_links = [], set()
    for sel in ["article h2 a, article h3 a", ".post-title a, .entry-title a",
                "h2 a[href], h3 a[href]", ".card-title a"]:
        for a in soup.select(sel):
            if len(results) >= MAX_PER_SOURCE:
                break
            title_text = a.get_text(strip=True)
            href = a.get("href", "")
            if not href or len(title_text) < 5:
                continue
            if not href.startswith("http"):
                href = "https://aiera.com.cn" + href
            if href in seen_links:
                continue
            seen_links.add(href)
            parent = a.find_parent(["article","div","li"])
            p = parent.find("p") if parent else None
            results.append(make_item("爱意若", "💡", title_text, href,
                                     p.get_text(strip=True) if p else ""))
        if results:
            break
    print(f"  [爱意若] {len(results)} 条")
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
        if len(results) >= MAX_PER_SOURCE:
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
        if len(results) >= MAX_PER_SOURCE:
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
        if len(results) >= MAX_PER_SOURCE:
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
            if len(results) >= MAX_PER_SOURCE:
                break
            title_text = a.get_text(strip=True)
            href = a.get("href","")
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

def fetch_ft():
    return from_rss("https://www.ft.com/rss/home", "FT", "🏦")


# ─── 飞书推送 ──────────────────────────────────────────────────────────────────

def build_card(items: list) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    groups: dict = {}
    for it in items:
        groups.setdefault(it["source"], []).append(it)

    elements = []
    for source, news_list in groups.items():
        emoji = news_list[0]["emoji"]
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{emoji} {source}**"}
        })
        for n in news_list:
            summary_md = (f"\n<font color='grey'>{n['summary']}</font>"
                          if n["summary"] else "")
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"· [{n['title']}]({n['link']}){summary_md}"
                }
            })
        elements.append({"tag": "hr"})

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": f"共 {len(items)} 条新内容 · {now}"}]
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📰 新闻速递 · {now}"},
                "template": "wathet"
            },
            "elements": elements
        }
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
        resp = SESSION.post(FEISHU_WEBHOOK, json=payload,
                            headers={"Content-Type": "application/json"}, timeout=15)
        data = resp.json()
        code = data.get("StatusCode", data.get("code", -1))
        if resp.status_code == 200 and code == 0:
            print(f"✅ 推送成功：{len(items)} 条")
        else:
            print(f"❌ 推送失败：{resp.status_code}  {resp.text}")
    except Exception as e:
        print(f"❌ 推送异常：{e}")


# ─── 来源列表 ──────────────────────────────────────────────────────────────────

FETCHERS = [
    ("BBC中文",      fetch_bbc),
    ("路透中文",     fetch_reuters),
    ("晚点LatePost", fetch_latepost),
    ("机器之心",     fetch_jiqizhixin),
    ("爱意若",       fetch_aiera),
    ("TechCrunch",  fetch_techcrunch),
    ("Wired",       fetch_wired),
    ("The Verge",   fetch_theverge),
    ("MIT科技评论",  fetch_mit),
    ("钛媒体",       fetch_tmtpost),
    ("36Kr",        fetch_36kr),
    ("虎嗅",        fetch_huxiu),
    ("华尔街见闻",   fetch_wallstreetcn),
    ("Bloomberg",   fetch_bloomberg),
    ("WSJ",         fetch_wsj),
    ("FT",          fetch_ft),
]


# ─── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"开始抓取 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}")

    seen = load_seen()
    all_items = []

    # 1. 抓取
    for name, fetcher in FETCHERS:
        try:
            results = fetcher()
            all_items.extend(results)
        except Exception as e:
            print(f"  [{name}] 异常: {e}")
        time.sleep(1.5)

    # 2. 过滤已推送
    new_items = [it for it in all_items if it["id"] not in seen]
    print(f"\n合计 {len(all_items)} 条，新内容 {len(new_items)} 条")

    # 3. 翻译（只翻译英文来源的新条目）
    if new_items:
        en_count = sum(1 for it in new_items if it["source"] in ENGLISH_SOURCES)
        if en_count:
            print(f"翻译 {en_count} 个英文来源的条目...")
        translated = []
        for it in new_items:
            translated.append(translate_item(it))
            if it["source"] in ENGLISH_SOURCES:
                time.sleep(0.3)  # 避免触发 Google 翻译限速
        new_items = translated

    # 4. 推送
    send_to_feishu(new_items)

    # 5. 保存去重记录
    if new_items:
        seen.update(it["id"] for it in new_items)
        save_seen(seen)

    print("完成！\n")


if __name__ == "__main__":
    main()
