import os
import json
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta
import requests
import glob
import difflib

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
        )

    # 中文连续片段（2+字）频次统计 —— 比滑动窗口n-gram更干净
    def phrase_freq(titles):
        freq = {}
        for title in titles:
            # 提取2字及以上的纯中文连续片段
            phrases = re.findall(r'[一-鿿]{2,}', title)
            for ph in phrases:
                freq[ph] = freq.get(ph, 0) + 1
                # 较长片段同时拆为2-4字子串入库，兼顾短关键词（如"黄金"）
                if len(ph) > 4:
                    for n in (2, 3):
                        for i in range(len(ph) - n + 1):
                            sub = ph[i:i + n]
                            freq[sub] = freq.get(sub, 0) + 1
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
    )


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

# ---------- 历史查重 ----------
def load_historical_titles(days=30):
    history = {}
    cutoff = datetime.now() - timedelta(days=days)
    for f in glob.glob(f"{DATA_DIR}/*.json"):
        try:
            date_str = f[-14:-5]
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except:
            continue
        if dt >= cutoff and dt < datetime.now():
            with open(f, "r", encoding="utf-8") as file:
                data = json.load(file)
                for platform, items in data.items():
                    for item in items:
                        title = item.get("title")
                        if title and title not in history:
                            history[title] = date_str
    return history

def check_historical_overlap(current_titles, historical_map, threshold=0.65):
    alerts = []
    sorted_titles = sorted(current_titles, key=len, reverse=True)
    hist_items = list(historical_map.items())
    for current_title in sorted_titles:
        for hist_title, hist_date in hist_items:
            ratio = difflib.SequenceMatcher(None, current_title, hist_title).ratio()
            if ratio >= threshold:
                alerts.append({
                    "current": current_title,
                    "history": hist_title,
                    "date": hist_date,
                    "similarity": round(ratio, 2)
                })
                break
    return alerts

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

# ---------- 核心分析（四大框架）----------
def call_deepseek(platforms, change_text, alert_text, user_field, api_key):
    # 整理今日榜单全貌
    flat_text = ""
    for name, items in platforms.items():
        flat_text += f"\n--- {name} Top5 ---\n"
        for i in items[:5]:
            flat_text += f"#{i['rank']} {i['title']}\n"

    prompt = f"""
【角色】你是兼具媒体洞察与商业决策力的资深分析师。

【任务】基于以下今日热榜数据，执行4步强制分析。输出需严格分段，每段带小标题。

【今日数据】：
{flat_text}

【昨日微博变化参考】：
{change_text}

【历史重复预警】：
{alert_text}

--- 请按以下4步输出分析（每步必答）---

**第一步：议题生命周期预判**
- 从今日Top3中，分别给出所处的阶段：【爆发期/发酵期/反转期/消退期】。
- 若有处于【爆发期】的事件，请列出“为确认此事真伪/走向，接下来必须盯紧的3个关键证据或时间节点”。

**第二步：情绪温差校准**
- 对比“知乎（偏理性/官方）”与“微博/抖音（偏民间）”的标题措辞烈度。
- 给出温差评级：【温和/剧烈/极端】。
- 若评级为剧烈以上，请判断：此事是否属于“情绪资产”型事件（即事实影响小但舆论影响大）？并给出应对原则（例如：紧盯行为转化，忽略口水战）。

**第三步：关联熵减归因（寻找隐线）**
- 强行总结：今日前3个热点，共同指向哪一条“社会潜流”或“经济隐线”？
- 为该隐线取一个4-6个字的名称（如“消费降级信号”、“AI焦虑外溢”），并写入今日的“隐线档案”。

**第四步：决策倒推沙盘（100字以内）**
- 假设你负责相关业务/投资/生活决策，基于今日信号，请给出**一件具体可执行的小事**。
- 结合用户的关注领域：{user_field}（包括股市/基金投资、科技产品消费、泛社会趋势）。
- 输出必须可落地，例如：“若持仓消费电子，明日考虑减仓5%”或“本周内暂停购买新款AI硬件，待观察竞品动向”或“减少刷短视频时间，转为阅读行业白皮书”。

**输出格式要求**：每步以 `### 步骤X` 开头，清晰分段。
"""
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是冷静、深刻、只说干货的战略分析师，拒绝套话和风险警告。"},
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
    if not items:
        return "  （暂无数据）"
    return "\n".join([f"  #{i['rank']} {i['title']}" for i in items[:5]])
# ---------- 主流程 ----------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # 清理旧数据
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    for f in glob.glob(f"{DATA_DIR}/*.json"):
        try:
            dt = datetime.strptime(f[-14:-5], "%Y-%m-%d")
            if dt < cutoff:
                os.remove(f)
        except:
            continue

    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"开始抓取 {today_str} 数据...")
    platforms = {
        "微博": fetch_weibo(),
        "百度热搜": fetch_baidu(),
        "抖音/头条": fetch_douyin()
    }
    
    # 历史查重
    historical_map = load_historical_titles(KEEP_DAYS)
    today_all_titles = []
    for items in platforms.values():
        for item in items:
            if item["title"] not in today_all_titles:
                today_all_titles.append(item["title"])
    alerts = check_historical_overlap(today_all_titles, historical_map, SIMILARITY_THRESHOLD)
    
    alert_text = ""
    if alerts:
        alert_text = "⚠️⚠️⚠️ 【重点重复/持续高热预警】⚠️⚠️⚠️\n"
        for a in alerts:
            alert_text += f"• “{a['current']}” 与 {a['date']} 的 “{a['history']}” 高度相似 (相似度{a['similarity']})\n"
        alert_text += "\n（该话题可能为老梗重提或持续发酵，建议深挖背后动因）\n\n"
    else:
        alert_text = "✅ 【历史查重】今日热点与过去30天内容无高度重复，多为新鲜话题。\n\n"

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
    keyword_report = auto_trending_keywords(platforms, yesterday_platforms)

    # 调用DeepSeek（传入用户领域）
    user_interest = "股市/基金投资、科技产品消费、泛社会趋势"
    api_key = os.getenv("DEEPSEEK_API_KEY")
    ai_summary = call_deepseek(platforms, change_text, alert_text, user_interest, api_key) if api_key else "未配置DeepSeek Key"

    # ---- 组装邮件（修改了两个地方） ----
    body = f"""
📰 {today_str} 深度洞察日报
{'='*50}

【🔴 历史重复预警】
{alert_text}

--- AI四维深度分析 ---
{ai_summary}

【🔥 关键词热度追踪】
{keyword_report}

{'='*50}
📊 各平台 Top5 快照
{'='*50}

【微博 Top5】
{format_platform(platforms.get('微博', []))}

【百度热搜 Top5】
{format_platform(platforms.get('百度热搜', []))}

【抖音/头条 Top5】
{format_platform(platforms.get('抖音/头条', []))}

{'='*50}
📈 微博排名变化（昨日对比）
{change_text}

-- 本报告由 GitHub Actions 每日自动生成 --
"""

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
