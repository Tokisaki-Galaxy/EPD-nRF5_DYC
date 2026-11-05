# -*- coding: utf-8 -*-
"""
epd_update.py — 采集数据并渲染成 800x480 的 PNG

依赖:
    pip install requests
    pip install lunardate      # 可选，装了才有农历

用法:
    python epd_update.py               # 输出 epd.png
    python epd_update.py out.png       # 指定输出文件

流程:
    天气 / 行情 / 突发 / Token / 日历 → data.json
    → Edge 无头截图 → PNG

任何一路数据失败都会退回到上一次 data.json 里的值，
第一次运行失败则退回页面内置的示例数据。
"""

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
HTML = BASE / "epd-dashboard.html"
DATA = BASE / "data.json"
DEFAULT_PNG = BASE / "epd.png"

CITY = "辽宁本溪溪湖区"
CITY_CODE = "101070501"          # 中国天气网城市编码，本溪市
LAT, LON = 41.33, 123.76         # 溪湖区，给 Open-Meteo 补体感/气压/紫外线

MARKETS = [
    ("沪深300", "sh000300"),
    ("中证500", "sh000905"),
    ("黄金ETF", "sh518880"),
]

RSS_SOURCES = [
    "https://feedx.net/rss/thepaper.xml",
]

# 突发关键词，命中任一就进突发条
BREAK_WORDS = [
    "开火", "交火", "空袭", "袭击", "战争", "冲突", "封锁", "制裁",
    "地震", "海啸", "台风", "暴雨", "洪水", "溃堤", "山体滑坡",
    "爆炸", "坠机", "失联", "停运", "熔断", "停牌",
    "降准", "降息", "加息", "暴发", "紧急", "突发",
]

CC_DB = Path.home() / ".cc-switch" / "cc-switch.db"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

TIMEOUT = 12
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 法定节假日，官方每年会调整，跨年后记得更新这张表
HOLIDAYS = [
    (date(2026, 1, 1), "元旦"),
    (date(2026, 2, 17), "春节"),
    (date(2026, 4, 5), "清明"),
    (date(2026, 5, 1), "劳动节"),
    (date(2026, 6, 19), "端午"),
    (date(2026, 9, 25), "中秋"),
    (date(2026, 10, 1), "国庆"),
    (date(2027, 1, 1), "元旦"),
    (date(2027, 2, 6), "春节"),
    (date(2027, 4, 5), "清明"),
    (date(2027, 5, 1), "劳动节"),
    (date(2027, 6, 9), "端午"),
    (date(2027, 9, 15), "中秋"),
    (date(2027, 10, 1), "国庆"),
]

SOLAR_TERMS = [
    "小寒", "大寒", "立春", "雨水", "惊蛰", "春分",
    "清明", "谷雨", "立夏", "小满", "芒种", "夏至",
    "小暑", "大暑", "立秋", "处暑", "白露", "秋分",
    "寒露", "霜降", "立冬", "小雪", "大雪", "冬至",
]
TERM_MINUTES = [
    0, 21208, 42467, 63836, 85337, 107014, 128867, 150921,
    173149, 195551, 218072, 240693, 263343, 285989, 308563, 331033,
    353350, 375494, 397447, 419210, 440795, 462224, 483532, 504758,
]

try:
    from lunardate import LunarDate
except ImportError:
    LunarDate = None


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


# ----------------------------------------------------------------------------
# 网络
# ----------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})


def get(url, decode=None, timeout=TIMEOUT):
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            if decode:
                r.encoding = decode
            return r
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise last


def get_json(url, timeout=TIMEOUT):
    return get(url, timeout=timeout).json()


# ----------------------------------------------------------------------------
# 天气
# ----------------------------------------------------------------------------

ICON_RULES = [
    ("雷", "thunder"),
    ("雪", "snow"),
    ("雨", "rain"),
    ("雾", "fog"),
    ("霾", "fog"),
    ("阴", "overcast"),
    ("沙", "fog"),
    ("尘", "fog"),
]


def icon_of(text):
    for key, icon in ICON_RULES:
        if key in text:
            return icon
    if "晴" in text and "云" not in text:
        return "sun"
    return "cloud"


def num(text):
    m = re.search(r"-?\d+", str(text))
    return int(m.group()) if m else 0


def uv_text(value):
    if value is None:
        return "--"
    if value <= 2:
        return "弱"
    if value <= 5:
        return "中等"
    if value <= 7:
        return "强"
    if value <= 10:
        return "很强"
    return "极强"


def collect_weather():
    raw = get_json(f"http://t.weather.itboy.net/api/weather/city/{CITY_CODE}")
    data = raw["data"]
    today = data["forecast"][0]

    temp = int(round(float(data.get("wendu") or 0)))
    high = num(today.get("high"))
    low = num(today.get("low"))
    aqi_value = int(today.get("aqi") or data.get("pm25") or 0)
    quality = data.get("quality") or "--"

    feels, pressure, uv = temp, None, None
    try:
        om = get_json(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}"
            "&current=apparent_temperature,surface_pressure"
            "&daily=uv_index_max&forecast_days=1&timezone=Asia%2FShanghai",
            timeout=8,
        )
        feels = int(round(om["current"]["apparent_temperature"]))
        pressure = int(round(om["current"]["surface_pressure"]))
        uv = om["daily"]["uv_index_max"][0]
    except Exception as exc:
        log(f"体感/气压/紫外线补全失败，用温度顶替: {exc}")

    days = [{
        "label": "今天",
        "text": today.get("type") or "",
        "icon": icon_of(today.get("type") or ""),
        "high": high,
        "low": low,
    }]
    for idx, label in ((1, "明天"), (2, "后天")):
        f = data["forecast"][idx]
        days.append({
            "label": label,
            "text": f.get("type") or "",
            "icon": icon_of(f.get("type") or ""),
            "high": num(f.get("high")),
            "low": num(f.get("low")),
        })

    # 按日期留一份原始预报, 抓取失败跨天时用它挑当天的数据
    saved = []
    for f in data["forecast"]:
        saved.append({
            "date": f.get("ymd") or "",
            "text": f.get("type") or "",
            "high": num(f.get("high")),
            "low": num(f.get("low")),
            "wind": f"{f.get('fx', '')} {f.get('fl', '')}".strip(),
            "aqi": int(f.get("aqi") or 0),
        })

    return {
        "temp": temp,
        "feels": feels,
        "text": today.get("type") or "",
        "icon": icon_of(today.get("type") or ""),
        "high": high,
        "low": low,
        "device": None,
        "humidity": num(data.get("shidu")),
        "wind": f"{today.get('fx', '')} {today.get('fl', '')}".strip(),
        "uv": uv_text(uv),
        "pressure": pressure,
        "aqi": {
            "text": quality,
            "value": aqi_value,
            "bad": aqi_value > 100,
        },
        "days": days,
        "forecast": saved,
    }


def aqi_text(value):
    if value <= 50:
        return "优"
    if value <= 100:
        return "良"
    if value <= 150:
        return "轻度"
    if value <= 200:
        return "中度"
    if value <= 300:
        return "重度"
    return "严重"


def weather_from_forecast(prev, today):
    """抓取失败时, 用之前按日期存下的预报重建当天天气. 找不到今天则返回 None."""
    entries = prev.get("forecast") or []
    if not entries:
        return None
    by_date = {e.get("date"): e for e in entries}

    def entry_for(day):
        return by_date.get(day.strftime("%Y-%m-%d"))

    e = entry_for(today)
    if e is None:
        return None

    days = []
    for offset, label in ((0, "今天"), (1, "明天"), (2, "后天")):
        f = entry_for(today + timedelta(days=offset))
        if f is None:
            break
        days.append({
            "label": label,
            "text": f.get("text") or "",
            "icon": icon_of(f.get("text") or ""),
            "high": f.get("high"),
            "low": f.get("low"),
        })

    high, low = e.get("high"), e.get("low")
    temp = int(round((high + low) / 2)) if high is not None and low is not None else (prev.get("temp") or 0)
    aqi_value = int(e.get("aqi") or 0)
    return {
        "temp": temp,
        "feels": temp,
        "text": e.get("text") or "",
        "icon": icon_of(e.get("text") or ""),
        "high": high,
        "low": low,
        "device": prev.get("device"),
        "humidity": prev.get("humidity"),
        "wind": e.get("wind") or prev.get("wind"),
        "uv": prev.get("uv"),
        "pressure": prev.get("pressure"),
        "aqi": {"text": aqi_text(aqi_value), "value": aqi_value, "bad": aqi_value > 100},
        "days": days,
        "forecast": entries,
        "stale": True,
    }


# ----------------------------------------------------------------------------
# 行情
# ----------------------------------------------------------------------------
def collect_market(name, code):
    daily_raw = get_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={code},day,,,30,qfq"
    )
    node = daily_raw["data"][code]
    bars = node.get("day") or node.get("qfqday") or []
    bars = bars[-30:]

    minute_raw = get_json(
        f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    )
    rows = minute_raw["data"][code]["data"]["data"]
    minutes = []
    for row in rows:
        parts = row.split()
        if len(parts) < 2:
            continue
        # 黄金 ETF 会多出 15:00-15:30 的盘后交易，截掉保证三个横轴一致
        if int(parts[0]) <= 1500:
            minutes.append(float(parts[1]))

    quote = get(f"https://qt.gtimg.cn/q={code}", decode="gbk").text
    parts = quote.split("~")
    price = parts[3]
    prev = float(parts[4] or 0)
    change = round((float(price) / prev - 1) * 100, 2) if prev else 0.0

    return {
        "name": name,
        "price": price,
        "change": change,
        "prevClose": prev,
        "daily": bars,
        "minute": minutes,
    }


def collect_markets():
    out = []
    for name, code in MARKETS:
        out.append(collect_market(name, code))
        log(f"行情 {name} {out[-1]['price']} {out[-1]['change']:+.2f}%")
    return out


# ----------------------------------------------------------------------------
# 突发新闻
# ----------------------------------------------------------------------------

def fmt_pubdate(text):
    try:
        dt = parsedate_to_datetime(text)
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return datetime.now().strftime("%H:%M")


def collect_breaking():
    found = []
    for url in RSS_SOURCES:
        try:
            content = get(url).content
            root = ET.fromstring(content)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                if not title:
                    continue
                if any(word in title for word in BREAK_WORDS):
                    found.append({
                        "title": title,
                        "time": fmt_pubdate(item.findtext("pubDate") or ""),
                    })
        except Exception as exc:
            log(f"RSS 失败 {url}: {exc}")

    seen, items = set(), []
    for item in found:
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        items.append(item)
    return items[:3]


# ----------------------------------------------------------------------------
# Token
# ----------------------------------------------------------------------------

def collect_tokens():
    if not CC_DB.exists():
        raise FileNotFoundError(f"找不到 {CC_DB}")

    con = sqlite3.connect(f"file:{CC_DB.as_posix()}?mode=ro", uri=True, timeout=8)
    try:
        cur = con.cursor()
        cur.execute(
            """
            select
              coalesce(sum(case when date(created_at,'unixepoch','localtime')
                    = date('now','localtime')
                    then input_tokens + output_tokens
                         + cache_read_tokens + cache_creation_tokens
                    else 0 end), 0),
              coalesce(sum(input_tokens + output_tokens
                           + cache_read_tokens + cache_creation_tokens), 0)
            from proxy_request_logs
            where date(created_at,'unixepoch','localtime')
                  >= date('now','localtime','-6 days')
            """
        )
        today, week = cur.fetchone()

        cur.execute(
            """
            select model, count(*) c
            from proxy_request_logs
            where date(created_at,'unixepoch','localtime')
                  >= date('now','localtime','-6 days')
            group by model
            order by c desc
            limit 1
            """
        )
        row = cur.fetchone()
        model = row[0] if row else "-"
    finally:
        con.close()

    return {
        "today": int(today),
        "week": int(week),
        "avg": int(week / 7),
        "model": model,
    }


# ----------------------------------------------------------------------------
# 日历
# ----------------------------------------------------------------------------

LUNAR_MONTH = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
LUNAR_DAY = [
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
]


def lunar_text(day):
    if LunarDate is None:
        return ""
    try:
        try:
            ld = LunarDate.from_solar_date(day.year, day.month, day.day)
        except AttributeError:
            ld = LunarDate.fromSolarDate(day.year, day.month, day.day)
        month = ("闰" if getattr(ld, "isLeapMonth", False) else "") + LUNAR_MONTH[ld.month - 1] + "月"
        return f"农历{month}{LUNAR_DAY[ld.day - 1]}"
    except Exception as exc:
        log(f"农历计算失败: {exc}")
        return ""


def term_datetime(year, index):
    base = datetime(1900, 1, 6, 2, 5)
    return base + timedelta(minutes=525948.76 * (year - 1900) + TERM_MINUTES[index])


def next_term(now):
    for year in (now.year, now.year + 1):
        for index, name in enumerate(SOLAR_TERMS):
            moment = term_datetime(year, index)
            if moment > now:
                return name, (moment.date() - now.date()).days
    return "", 0


def collect_calendar(now):
    today = now.date()
    year_end = date(today.year, 12, 31)
    days_left = (year_end - today).days

    holiday = {"name": "", "days": 0}
    for day, name in HOLIDAYS:
        if day > today:
            holiday = {"name": name, "days": (day - today).days}
            break

    term_name, term_days = next_term(now)
    parts = [p for p in (lunar_text(today), f"距{term_name} {term_days} 天" if term_name else "") if p]

    return {
        "daysLeft": days_left,
        "holiday": holiday,
        "lunar": " · ".join(parts),
    }


# ----------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------

def find_browser():
    for path in BROWSERS:
        if Path(path).exists():
            return path
    raise SystemExit("找不到 Edge 或 Chrome，请手动改 BROWSERS 列表")


def shoot(png_path):
    if not HTML.exists():
        raise SystemExit(f"找不到 {HTML}")

    browser = find_browser()
    profile = Path(tempfile.mkdtemp(prefix="epd-shot-"))
    url = f"{HTML.as_uri()}?clean=1&data=data.json"
    cmd = [
        browser,
        "--headless",
        "--disable-gpu",
        "--disable-lcd-text",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=800,480",
        "--virtual-time-budget=6000",
        "--allow-file-access-from-files",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--screenshot={png_path}",
        url,
    ]

    try:
        subprocess.run(cmd, check=True, timeout=120,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if not Path(png_path).exists():
        raise SystemExit("截图失败，没有生成 PNG")


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------

def market_is_open(now):
    """交易日且处在交易时段内才认为今天的日 K 还在动."""
    if now.weekday() >= 5:
        return False
    if now.date() in {h for h, _ in HOLIDAYS}:
        return False
    t = now.hour * 60 + now.minute
    return 570 <= t <= 690 or 780 <= t <= 900


def main():
    out_png = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PNG
    now = datetime.now()

    previous = {}
    if DATA.exists():
        try:
            previous = json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    result = dict(previous)
    result["city"] = CITY
    result["marketOpen"] = market_is_open(now)
    result["date"] = f"{now.month}月{now.day:02d}日"
    result["week"] = "周" + "一二三四五六日"[now.weekday()]
    # 无头浏览器的虚拟时钟是冻结的, 页面取不到真实时间, 刷新时刻由这里写入
    result["slot"] = now.strftime("%H:%M")

    cal = collect_calendar(now)
    result["calendar"] = {
        "daysLeft": cal["daysLeft"],
        "holiday": cal["holiday"],
    }
    result["lunar"] = cal["lunar"]
    log(f"日历 今年剩 {cal['daysLeft']} 天，下一个 {cal['holiday']['name']} {cal['holiday']['days']} 天后")

    try:
        result["weather"] = collect_weather()
        # 保留设备状态字段, 它是刷新前从设备读的
        old_dev = (previous.get("weather") or {}).get("device")
        if old_dev is not None:
            result["weather"]["device"] = old_dev
        log(f"天气 {result['weather']['temp']}° {result['weather']['text']}")
    except Exception as exc:
        prev_w = previous.get("weather") or {}
        rebuilt = weather_from_forecast(prev_w, now.date())
        if rebuilt is not None:
            result["weather"] = rebuilt
            log(f"天气失败，改用 {now.month}月{now.day:02d}日 的预报数据: {exc}")
        else:
            log(f"天气失败，沿用旧数据: {exc}")

    try:
        result["markets"] = collect_markets()
    except Exception as exc:
        log(f"行情失败，沿用旧数据: {exc}")

    try:
        result["breaking"] = collect_breaking()
        log(f"突发 {len(result['breaking'])} 条")
    except Exception as exc:
        log(f"突发失败，沿用旧数据: {exc}")

    try:
        # token 在页面最底部, 每 15 分钟都变会把差分框拉成整条竖带,
        # 所以只在距上次采集满 55 分钟时才更新
        last_token = previous.get("tokenAt") or 0
        if previous.get("tokens") and time.time() - last_token < 3300:
            log("Token 沿用上次, 未到整点更新")
        else:
            result["tokens"] = collect_tokens()
            result["tokenAt"] = int(time.time())
            log(f"Token 今日 {result['tokens']['today']:,}，模型 {result['tokens']['model']}")
    except Exception as exc:
        log(f"Token 失败，沿用旧数据: {exc}")

    DATA.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"数据写入 {DATA.name}")

    shoot(out_png)
    log(f"已输出 {out_png}")


if __name__ == "__main__":
    main()
