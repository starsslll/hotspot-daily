import os
import json
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta
import requests
import glob
import difflib
import jieba
from urllib.parse import quote

# ---------- 配置 ----------
DATA_DIR = "data"
KEEP_DAYS = 30
SIMILARITY_THRESHOLD = 0.65
# 关键词不再写死，改为 auto_trending_keywords() 自动检测热度飙升词
# 无昨日数据时使用以下默认词
DEFAULT_KEYWORDS = ["AI", "芯片", "黄金", "裁员"]

# ---------- 抓取函数（不变）----------
def fetch_weibo():
    url = "https://weibo.com/ajax/statuses/hot_band"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://weibo.com/"
    }
    resp = requests.get(url, headers=headers, timeout=10)
    data = resp.json().get("data", {}).get("band_list", [])
    return [{"title": item.get("word"), "rank": idx + 1} for idx, item in enumerate(data) if item.get("word")][:20]

def fetch_baidu():
    url = "https://top.baidu.com/board?tab=realtime"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        # 百度热搜页面是SPA，数据在嵌入式JSON中
        import re
        # 匹配热搜标题（在 "query" 字段中）
        titles = re.findall(r'"query":"([^"]+)"', resp.text)
        result = []
        for idx, title in enumerate(titles[:20]):
            if title.strip():
                result.append({"title": title.strip(), "rank": idx+1})
        return result
    except Exception as e:
        print(f"百度热搜获取失败: {e}")
        return []

def fetch_douyin():
    url = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/?count=20"
    resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code == 200 and "word_list" in resp.json():
        items = resp.json()["word_list"]
        return [{"title": i.get("word"), "rank": idx + 1} for idx, i in enumerate(items)]
    else:
        print("抖音失效，切换至今日头条热榜兜底")
        return fetch_toutiao()

def fetch_toutiao():
    url = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
    resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
    data = resp.json().get("data", [])
    return [{"title": i["Title"], "rank": idx + 1} for idx, i in enumerate(data[:20])]

# ---------- RSS 抓取 & 翻译工具（关键词扩展搜索用）----------
def _fetch_news_rss(url, source_label, strip_suffixes=None):
    """通用 RSS 抓取，返回标题列表。strip_suffixes 用于去除 \" - Source\" 后缀"""
    import xml.etree.ElementTree as ET
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        resp = requests.get(url, headers=headers, timeout=(5, 8))
        if resp.status_code != 200:
            print(f"{source_label} RSS 返回 {resp.status_code}")
            return []
        root = ET.fromstring(resp.text)
        items = root.findall(".//item/title")
        titles = []
        for item in items:
            t = (item.text or "").strip()
            if not t:
                continue
            # 去除 \" - SourceName\" 后缀
            if strip_suffixes:
                for suffix in strip_suffixes:
                    if t.endswith(suffix):
                        t = t[:-len(suffix)].strip()
            if t and t not in titles:
                titles.append(t)
        return titles[:10]
    except Exception as e:
        print(f"{source_label} RSS 获取失败: {e}")
        return []


def _translate_titles(english_titles, api_key):
    """批量翻译英文标题为中文，无 key 或失败时返回原文"""
    if not english_titles or not api_key:
        return english_titles
    # 只翻译含英文字母的标题
    need_trans = [t for t in english_titles if any(c.isascii() and c.isalpha() for c in t)]
    if not need_trans:
        return english_titles

    joined = "\n".join(need_trans)
    prompt = f"将以下英文新闻标题翻译成简洁中文，保持原意，每行一条，不加编号：\n{joined}"
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是专业翻译。只输出翻译结果，每行一条，不要编号和解释。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 400
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=25)
        result = resp.json()["choices"][0]["message"]["content"].strip()
        translated = [line.strip() for line in result.split("\n") if line.strip()]
        # 用翻译结果替换原文
        final = []
        ti = 0
        for t in english_titles:
            if any(c.isascii() and c.isalpha() for c in t) and ti < len(translated):
                final.append(translated[ti])
                ti += 1
            else:
                final.append(t)
        return final
    except Exception as e:
        print(f"英文翻译失败: {e}")
        return english_titles


def fetch_bbc(api_key=None):
    """BBC Top News（Google News RSS → 翻译）"""
    url = "https://news.google.com/rss/search?q=site:bbc.com&hl=en-US&gl=US&ceid=US:en"
    titles = _fetch_news_rss(url, "BBC", strip_suffixes=[" - BBC.com", " - BBC News", " - BBC"])
    if api_key:
        titles = _translate_titles(titles, api_key)
    return [{"title": t, "rank": idx + 1} for idx, t in enumerate(titles)]


def fetch_cnn(api_key=None):
    """CNN Top News（Google News RSS → 翻译）"""
    url = "https://news.google.com/rss/search?q=site:cnn.com&hl=en-US&gl=US&ceid=US:en"
    titles = _fetch_news_rss(url, "CNN", strip_suffixes=[" - CNN", " - CNN.com"])
    if api_key:
        titles = _translate_titles(titles, api_key)
    return [{"title": t, "rank": idx + 1} for idx, t in enumerate(titles)]


def fetch_wsj(api_key=None):
    """华尔街日报 Top News（Google News RSS → 翻译）"""
    url = "https://news.google.com/rss/search?q=site:wsj.com&hl=en-US&gl=US&ceid=US:en"
    titles = _fetch_news_rss(url, "华尔街日报",
                             strip_suffixes=[" - WSJ", " - The Wall Street Journal"])
    if api_key:
        titles = _translate_titles(titles, api_key)
    return [{"title": t, "rank": idx + 1} for idx, t in enumerate(titles)]


def fetch_36kr():
    """36氪 RSS（中文科技财经，无需翻译）"""
    url = "https://36kr.com/feed"
    titles = _fetch_news_rss(url, "36氪")
    return [{"title": t, "rank": idx + 1} for idx, t in enumerate(titles)]


def fetch_reuters(api_key=None):
    """路透社 Top News（Google News RSS → 翻译）"""
    url = "https://news.google.com/rss/search?q=site:reuters.com&hl=en-US&gl=US&ceid=US:en"
    titles = _fetch_news_rss(url, "路透社", strip_suffixes=[" - Reuters", " - Reuters.com"])
    if api_key:
        titles = _translate_titles(titles, api_key)
    return [{"title": t, "rank": idx + 1} for idx, t in enumerate(titles)]


def fetch_ap(api_key=None):
    """美联社 Top News（Google News RSS → 翻译）"""
    url = "https://news.google.com/rss/search?q=site:apnews.com&hl=en-US&gl=US&ceid=US:en"
    titles = _fetch_news_rss(url, "美联社",
                             strip_suffixes=[" - The Associated Press", " - AP News", " - AP"])
    if api_key:
        titles = _translate_titles(titles, api_key)
    return [{"title": t, "rank": idx + 1} for idx, t in enumerate(titles)]


# ---------- 关键词扩展搜索 ----------
def search_keyword_news(keywords, api_key=None):
    """对检测到的热度关键词，搜索 Google News 获取更多相关标题"""
    all_titles = []
    for kw in keywords[:5]:
        encoded = quote(kw)
        url = f"https://news.google.com/rss/search?q={encoded}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        titles = _fetch_news_rss(url, f"关键词搜索:{kw}")
        all_titles.extend(titles)
    # 去重保序
    seen = set()
    unique = []
    for t in all_titles:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    if api_key:
        unique = _translate_titles(unique, api_key)
    return unique


# ---------- AI 去重总结 ----------
def _summarize_extra_news(titles, api_key):
    """用 DeepSeek 对扩展搜索标题去重，同事件合并为一句话要点"""
    if not titles or not api_key:
        return titles[:15]
    if len(titles) <= 5:
        return titles

    joined = "\n".join(titles[:40])
    prompt = f"""以下是关键词搜索返回的相关新闻标题，很多是同一事件的重复报道。请：
1. 去除重复——同一事件的多条报道合并为1条
2. 每条用一句话概括核心事实
3. 按重要性排序
4. 输出格式：每行一条，以 · 开头，不超过12条

标题列表：
{joined}"""

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是信息整理专家。只输出去重总结结果，每行一条要点，以 · 开头。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": 500
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        result = resp.json()["choices"][0]["message"]["content"].strip()
        lines = [l.strip() for l in result.split("\n") if l.strip().startswith("·")]
        return lines if lines else titles[:12]
    except Exception as e:
        print(f"扩展新闻总结失败: {e}")
        return titles[:15]


def auto_trending_keywords(today_platforms, yesterday_platforms):
    """
    自动检测各平台标题中热度骤升的关键词（基于n-gram词频对比），
    并跨平台追踪排名。无昨日数据时回退为默认关键词。
    """
    import re

    def all_titles(platforms):
        titles = []
        for items in platforms.values():
            for item in items:
                titles.append(item["title"])
        return titles

    today_titles = all_titles(today_platforms)
    yesterday_titles = all_titles(yesterday_platforms)

    # 无昨日数据 → 回退默认关键词，仅做今日排名
    if not yesterday_titles:
        return _format_keyword_report(
            today_platforms, yesterday_platforms, DEFAULT_KEYWORDS,
            "（首日运行，使用默认关键词）"
        ), DEFAULT_KEYWORDS

    # 中文分词频次统计 —— jieba 分词 + 完整中文片段双路入库
    def phrase_freq(titles):
        freq = {}
        for title in titles:
            # 路1：jieba 分词，取2字及以上中文词（正确识别词边界）
            words = jieba.lcut(title)
            for w in words:
                if re.match(r'^[一-鿿]{2,}$', w):
                    freq[w] = freq.get(w, 0) + 1
            # 路2：完整中文连续片段（兜底长专有名词，如"佛得角门将"整体）
            phrases = re.findall(r'[一-鿿]{2,}', title)
            for ph in phrases:
                if ph not in freq:
                    freq[ph] = freq.get(ph, 0) + 1
                else:
                    freq[ph] = freq[ph] + 1
        return freq

    today_freq = phrase_freq(today_titles)
    yesterday_freq = phrase_freq(yesterday_titles)

    # 得分 = 今日频次 × 增长率（增长率越高说明越"骤升"）
    scored = []
    for ng, tc in today_freq.items():
        if tc < 2:
            continue
        yc = yesterday_freq.get(ng, 0)
        growth = tc / max(yc, 0.5)
        scored.append((ng, tc * growth, tc, yc))

    # 去重：长词优先，添加长词时移除已被包含的短词
    scored.sort(key=lambda x: (-len(x[0]), -x[1]))  # 按长度降序、得分降序
    selected = []
    for ng, _score, tc, yc in scored:
        if any(ng in other or other in ng for other in selected):
            continue
        selected.append(ng)
        if len(selected) >= 8:
            break

    # 生成热度检测说明
    lines = ["📊 【自动检测热度骤升关键词】"]
    for ng in selected:
        tc = today_freq[ng]
        yc = yesterday_freq.get(ng, 0)
        if yc == 0:
            lines.append(f"  🆕 {ng}（今日首次高频出现，{tc}次）")
        else:
            lines.append(f"  🔥 {ng}（{tc}次 | 昨{yc}次，热度飙升）")

    return "\n".join(lines) + "\n" + _format_keyword_report(
        today_platforms, yesterday_platforms, selected, ""
    ), selected


def _format_keyword_report(today_platforms, yesterday_platforms, keywords, prefix):
    """跨平台追踪关键词排名"""
    lines = []
    if prefix:
        lines.append(prefix)

    tracked = set()
    for kw in keywords:
        today_info = []
        yesterday_info = []
        for pname in today_platforms:
            for item in today_platforms[pname]:
                if kw.lower() in item["title"].lower():
                    today_info.append(f"{pname}#{item['rank']}")
                    tracked.add(kw)
        for pname in yesterday_platforms:
            for item in yesterday_platforms[pname]:
                if kw.lower() in item["title"].lower():
                    yesterday_info.append(f"{pname}#{item['rank']}")

        if today_info:
            today_str = "、".join(today_info)
            if yesterday_info:
                yesterday_str = "、".join(yesterday_info)
                lines.append(f"📌 {kw}：今日 {today_str}，昨日 {yesterday_str}")
            else:
                lines.append(f"🆕 {kw}：今日新上榜 {today_str}")
        elif yesterday_info:
            yesterday_str = "、".join(yesterday_info)
            lines.append(f"⬇️ {kw}：昨日 {yesterday_str}，今日已跌出榜单")

    if len(tracked) == 0:
        lines.append("  暂无追踪关键词上榜")
    return "\n".join(lines)

# ---------- 议题基因共振检测 ----------
def check_issue_resonance(current_titles, historical_entries,
                          l1_threshold=0.85, l2_threshold=0.65, l3_threshold=0.50,
                          dormant_days=7, today_date=None):
    """
    对每个今日标题在30天历史库中找最佳匹配，按相似度输出 L1/L2/L3 分级，
    并检测沉寂>=dormant_days后重新出现的"复活议题"。
    historical_entries: [{"title": str, "date": "YYYY-MM-DD"}, ...] 允许重复
    """
    if today_date is None:
        today_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    l1_simple, l2_incremental, l3_structural = [], [], []
    dormant_resurrections = []

    # 长标题优先匹配，防止短标题误配抢占
    sorted_titles = sorted(current_titles, key=len, reverse=True)
    # 历史条目按日期降序、长度降序
    hist_sorted = sorted(historical_entries, key=lambda x: (-len(x["title"]),
                         -(datetime.strptime(x["date"], "%Y-%m-%d") - datetime(2000, 1, 1)).days))

    for current_title in sorted_titles:
        # 找全局最佳匹配
        best = None  # (hist_title, hist_date, similarity)
        for entry in hist_sorted:
            ratio = difflib.SequenceMatcher(None, current_title, entry["title"]).ratio()
            if best is None or ratio > best[2]:
                best = (entry["title"], entry["date"], ratio)
            if ratio >= 0.95:  # 近乎完全相同，无需继续搜索
                break

        if best is None or best[2] < l3_threshold:
            continue

        hist_title, hist_date_str, similarity = best
        hist_dt = datetime.strptime(hist_date_str, "%Y-%m-%d")
        days_gap = (today_date - hist_dt).days

        # 分级
        if similarity >= l1_threshold:
            level, level_label = "L1", "简单重复"
            target_list = l1_simple
        elif similarity >= l2_threshold:
            level, level_label = "L2", "增量重复"
            target_list = l2_incremental
        else:
            level, level_label = "L3", "结构共振"
            target_list = l3_structural

        # 沉寂检测：最佳匹配 >7天前 且 最近7天内无任何相似条目
        dormant = False
        if days_gap > dormant_days:
            recent_cutoff = today_date - timedelta(days=dormant_days)
            has_recent = any(
                datetime.strptime(e["date"], "%Y-%m-%d") >= recent_cutoff
                and difflib.SequenceMatcher(None, current_title, e["title"]).ratio() >= l3_threshold
                for e in historical_entries
            )
            if not has_recent:
                dormant = True

        alert = {
            "current": current_title,
            "history": hist_title,
            "date": hist_date_str,
            "similarity": round(similarity, 2),
            "level": level,
            "level_label": level_label,
            "dormant": dormant,
            "days_gap": days_gap
        }
        target_list.append(alert)
        if dormant:
            dormant_resurrections.append(alert)

    total = len(l1_simple) + len(l2_incremental) + len(l3_structural)
    return {
        "l1_simple": l1_simple,
        "l2_incremental": l2_incremental,
        "l3_structural": l3_structural,
        "dormant_resurrections": dormant_resurrections,
        "total_matches": total,
        "has_resonance": total > 0
    }


def _build_resonance_context(resonance):
    """将结构化共振数据转为 DeepSeek prompt 中的紧凑文本上下文"""
    lines = []

    if resonance["dormant_resurrections"]:
        lines.append("[沉寂议题复活] >7天前消失、今日重现——属于强烈信号：")
        for d in resonance["dormant_resurrections"]:
            lines.append(
                f"  {d['level']} | \"{d['current']}\" <- {d['date']} \"{d['history']}\" "
                f"(相似度{d['similarity']}, 沉寂{d['days_gap']}天)"
            )

    if resonance["l3_structural"]:
        lines.append("[L3结构共振] 不同事件但底层议题基因相同——请强制归入隐线分析：")
        for d in resonance["l3_structural"]:
            tag = " [沉寂复活]" if d["dormant"] else ""
            lines.append(f"  \"{d['current']}\" <> {d['date']} \"{d['history']}\" ({d['similarity']}){tag}")

    if resonance["l2_incremental"]:
        lines.append("[L2增量重复] 同话题但新事实/数据/角色，需更新生命周期阶段：")
        for d in resonance["l2_incremental"]:
            tag = " [沉寂复活]" if d["dormant"] else ""
            lines.append(f"  \"{d['current']}\" ~ {d['date']} \"{d['history']}\" ({d['similarity']}){tag}")

    if resonance["l1_simple"]:
        lines.append(f"[L1简单重复] 同事件同来源无新信息 x{len(resonance['l1_simple'])}条（归档即可，勿占分析篇幅）")

    if not resonance["has_resonance"]:
        lines.append("[无历史共振] 今日热点与近30天历史无显著共振信号。")

    return "\n".join(lines)

def analyze_changes(today_data, yesterday_data):
    if not yesterday_data:
        return "【首日运行】暂无昨日对比数据。"
    yesterday_map = {item["title"]: item["rank"] for item in yesterday_data}
    changes = []
    for item in today_data:
        title = item["title"]
        today_rank = item["rank"]
        if title in yesterday_map:
            diff = yesterday_map[title] - today_rank
            if diff > 0:
                changes.append(f"📈 {title} (上升{diff}名, 今日第{today_rank})")
            elif diff < 0:
                changes.append(f"📉 {title} (下降{-diff}名, 今日第{today_rank})")
        else:
            changes.append(f"🆕 {title} (新上榜, 今日第{today_rank})")
    return "\n".join(changes[:15])

# ---------- 核心分析（四大框架 + 历史共振）----------
def call_deepseek(platforms, change_text, resonance, user_field, api_key, extra_news_titles=None):
    # 整理今日榜单全貌
    flat_text = ""
    for name, items in platforms.items():
        flat_text += f"\n[{name} Top5]\n"
        for i in items[:5]:
            flat_text += f"#{i['rank']} {i['title']}\n"

    # 关键词扩展阅读上下文
    extra_context = ""
    if extra_news_titles:
        extra_context = "\n[关键词扩展阅读（AI去重总结）]\n" + "\n".join(f"  {t}" for t in extra_news_titles[:20])

    # 历史共振上下文
    resonance_context = _build_resonance_context(resonance)

    prompt = f"""你是冷静深刻只说干货的战略分析师。基于以下数据输出4段分析，每段以【标签】起始，总字数控制在1000字以内，适合手机阅读。

[今日热榜]
{flat_text}
[排名变化]
{change_text}
{extra_context}

[历史共振档案]
{resonance_context}

---
请按顺序输出4段（每段必答，每段<=250字）：

【生命周期坐标】
对比今日Top3与历史轨迹，各自处于哪个阶段：爆发期/发酵期/反转期/消退期。若历史共振中有匹配，标注当前是对历史轨迹的\"延续\"\"加速\"还是\"转折\"。若有L3结构共振，说明其与历史事件的共同议题基因是什么。

【情绪温差校准】
比较各平台标题措辞烈度差异，给出温差评级：温和/剧烈/极端。若评级为剧烈以上，判断是否属于\"情绪资产型\"事件（事实影响小但舆论影响大），给出应对原则（如：紧盯行为转化，忽略口水战）。

【关联熵减归因】
总结今日热点共同指向的隐线，用4-6字命名（如\"AI焦虑外溢\"\"消费降级信号\"）。若历史共振中有L3结构共振或沉寂复活议题，务必将其串入隐线分析。

【决策倒推沙盘】
结合关注领域（{user_field}），基于今日信号+历史共振，给出1件可执行小事。必须具体可落地，100字以内。重复出现本身就是重要性信号。

输出格式：每段以【标签】起始，段间无空行。总字数不超过1000字。"""

    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是冷静、深刻、只说干货的战略分析师。拒绝套话和风险警告。输出简洁，适合手机阅读。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.4,
        "max_tokens": 900
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=40)
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"【AI分析失败】{str(e)}"

def send_email(subject, body, sender, password, recipients_list):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(recipients_list)
    
    try:
        # 改为 587 端口 + STARTTLS，比 465 更稳定
        server = smtplib.SMTP("smtp.qq.com", 587)
        server.starttls()
        server.set_debuglevel(0)  # 安静模式
        import time
        time.sleep(1)  # 关键：延时1秒，让服务器准备就绪
        server.login(sender, password)
        server.sendmail(sender, recipients_list, msg.as_string())
        server.quit()
        print("邮件发送成功")
    except Exception as e:
        print(f"邮件发送失败: {e}")
        
def format_platform(items):
    """Top10 多行格式，适合手机阅读"""
    if not items:
        return "  （暂无数据）"
    return "\n".join([f"  #{i['rank']:>2}  {i['title']}" for i in items[:10]])
# ---------- 主流程 ----------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # 清理旧数据
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    for f in glob.glob(f"{DATA_DIR}/*.json"):
        try:
            dt = datetime.strptime(os.path.basename(f)[-14:-5], "%Y-%m-%d")
            if dt < cutoff:
                os.remove(f)
        except:
            continue

    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"开始抓取 {today_str} 数据...")
    ds_key = os.getenv("DEEPSEEK_API_KEY")
    platforms = {
        "微博": fetch_weibo(),
        "百度热搜": fetch_baidu(),
        "抖音/头条": fetch_douyin(),
        "路透社": fetch_reuters(ds_key),
        "美联社": fetch_ap(ds_key),
        "BBC": fetch_bbc(ds_key),
        "CNN": fetch_cnn(ds_key),
        "华尔街日报": fetch_wsj(ds_key),
        "36氪": fetch_36kr(),
    }
    
    # ---- 议题基因共振检测（L1/L2/L3 + 沉寂复活）----
    today_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # 构建历史条目列表（保留重复，供沉寂检测用）
    historical_entries = []
    cutoff_dt = datetime.now() - timedelta(days=KEEP_DAYS)
    for f in glob.glob(f"{DATA_DIR}/*.json"):
        try:
            dt = datetime.strptime(os.path.basename(f)[-14:-5], "%Y-%m-%d")
            if dt < cutoff_dt or dt >= datetime.now():
                continue
        except:
            continue
        with open(f, "r", encoding="utf-8") as file:
            data = json.load(file)
            for platform_items in data.values():
                for item in platform_items:
                    title = item.get("title")
                    if title:
                        historical_entries.append({"title": title, "date": os.path.basename(f)[-14:-5]})

    today_all_titles = []
    for items in platforms.values():
        for item in items:
            if item["title"] not in today_all_titles:
                today_all_titles.append(item["title"])

    resonance = check_issue_resonance(today_all_titles, historical_entries,
                                      today_date=today_date)

    # 昨日数据（微博对比 + 关键词跨平台追踪）
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_path = f"{DATA_DIR}/{yesterday_str}.json"
    yesterday_platforms = {}
    if os.path.exists(yesterday_path):
        with open(yesterday_path, "r", encoding="utf-8") as f:
            yesterday_platforms = json.load(f)
    yesterday_weibo = yesterday_platforms.get("微博", [])
    today_weibo = platforms["微博"]
    change_text = analyze_changes(today_weibo, yesterday_weibo)

    # ---- 关键词热度追踪（自动检测 + 跨平台排名）----
    keyword_report, keywords = auto_trending_keywords(platforms, yesterday_platforms)

    # ---- 关键词扩展搜索（Google News RSS → AI去重总结）----
    extra_news_raw = search_keyword_news(keywords, ds_key) if ds_key else []
    extra_news = _summarize_extra_news(extra_news_raw, ds_key) if ds_key else extra_news_raw[:15]

    # 调用DeepSeek（传入议题共振数据 + 扩展新闻）
    user_interest = "股市/基金投资、科技产品消费、泛社会趋势"
    ai_summary = call_deepseek(platforms, change_text, resonance, user_interest, ds_key,
                               extra_news_titles=extra_news) if ds_key else "未配置DeepSeek Key"

    # ---- 组装手机优化邮件 ----
    # 议题共振卡片
    if resonance["has_resonance"]:
        resonance_card = "-- 议题共振 --\n"
        if resonance["dormant_resurrections"]:
            resonance_card += "!! 沉寂议题复活 !!\n"
            for d in resonance["dormant_resurrections"]:
                resonance_card += f"  [{d['level']}] {d['current']}\n"
                resonance_card += f"      <- {d['date']} (沉寂{d['days_gap']}天)\n"
        parts = []
        if resonance["l1_simple"]:
            parts.append(f"L1简单重复 x{len(resonance['l1_simple'])}")
        if resonance["l2_incremental"]:
            parts.append(f"L2增量重复 x{len(resonance['l2_incremental'])}")
        if resonance["l3_structural"]:
            parts.append(f"L3结构共振 x{len(resonance['l3_structural'])}")
        resonance_card += "  " + "  ".join(parts) + "\n"
    else:
        resonance_card = "-- 议题共振 --\n  今日无显著历史共振\n"

    body = f"[{today_str}] 深度洞察日报\n"
    body += "\n" + resonance_card
    body += f"\n-- AI分析 --\n{ai_summary}\n"
    body += f"\n-- 关键词追踪 --\n{keyword_report}\n"
    if extra_news:
        body += f"\n-- 关键词扩展（AI去重总结）--\n"
        body += "\n".join(f"  {t}" for t in extra_news[:15]) + "\n"
    body += "\n-- 平台快照 --\n"
    for pname, ptitle in [("微博", "微博"), ("百度热搜", "百度"), ("抖音/头条", "抖音"),
                           ("路透社", "路透社"), ("美联社", "美联社"),
                           ("BBC", "BBC"), ("CNN", "CNN"),
                           ("华尔街日报", "华尔街日报"), ("36氪", "36氪")]:
        body += f"\n【{ptitle} Top10】\n{format_platform(platforms.get(pname, []))}\n"
    body += f"\n-- 排名变化 --\n{change_text}\n"
    body += "\n-- GitHub Actions 自动生成 --"

    # 发送邮件
    recipients = os.getenv("RECIPIENTS", "").split(",")
    sender = os.getenv("SENDER_EMAIL")
    password = os.getenv("SENDER_PASSWORD")
    if sender and password and recipients:
        send_email(f"深度洞察日报 {today_str}", body, sender, password, recipients)
    else:
        print("邮件配置缺失，跳过发送")

    # 保存今日数据
    with open(f"{DATA_DIR}/{today_str}.json", "w", encoding="utf-8") as f:
        json.dump(platforms, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
