# -*- coding: utf-8 -*-
"""
股票辅助网站（A股 沪深）
- 查询：实时行情 + 近120日 K线/折线（可切换）+ 公司新闻
- 行情/K线：腾讯财经公开接口（无限流）
- 新闻：AkShare（东方财富）
- 技术：Flask + requests + AkShare + ECharts
- 运行：python 股票.py  →  浏览器打开 http://127.0.0.1:5000
"""

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import requests
from flask import Flask, render_template, request

app = Flask(__name__)

# ------------------------------------------------------------------
# 本地缓存
# ------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache = {}


def cached(key: str, ttl: int, producer):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (datetime.now() - hit[0]).total_seconds() < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (datetime.now(), value)
    return value


def tencent_prefix(symbol: str) -> str:
    """6 开头 → sh，0/3 开头 → sz，其余按 6 开头处理。"""
    return "sh" if symbol.startswith(("6", "9", "5")) else "sz"


# ------------------------------------------------------------------
# 数据获取
# ------------------------------------------------------------------

def fetch_kline(symbol: str, days: int = 750):
    """腾讯日 K 线（前复权）。失败返回 None。"""
    def _do():
        try:
            code = tencent_prefix(symbol) + symbol
            url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   f"?param={code},day,,,{days},qfq")
            data = requests.get(url, timeout=15).json()
            node = data["data"][code]
            rows = node.get("qfqday") or node.get("day")
            if not rows:
                return None
            return [
                {"date": r[0], "open": float(r[1]), "close": float(r[2]),
                 "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])}
                for r in rows
            ]
        except Exception as e:
            print(f"[kline {symbol}] 失败: {e}")
            return None
    return cached(f"kline:{symbol}", 600, _do)


def fetch_quote(symbol: str):
    """腾讯实时行情。失败返回 None。"""
    def _do():
        try:
            code = tencent_prefix(symbol) + symbol
            text = requests.get(
                f"https://qt.gtimg.cn/q={code}", timeout=10
            ).content.decode("gbk", errors="replace")
            m = re.search(r'v_\w+="(.*)"', text)
            if not m:
                return None
            fields = m.group(1).split("~")
            if len(fields) < 5:
                return None
            price = float(fields[3])
            prev_close = float(fields[4])
            pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0

            def _f(i, default=None):
                """安全取第 i 个字段转 float，空/越界/非数返回 default。"""
                try:
                    v = fields[i].strip()
                    return float(v) if v else default
                except (IndexError, ValueError):
                    return default

            return {
                "name": fields[1],
                "price": price,
                "open": float(fields[5]),
                "high": float(fields[33]) if fields[33] else price,
                "low": float(fields[34]) if fields[34] else price,
                "prev_close": prev_close,
                "pct_change": pct,
                "date": fields[30][:8] if len(fields) > 30 else "",
                # 腾讯扩展字段：38 换手率%、39 PE(TTM)、44 流通市值(亿)、
                # 45 总市值(亿)、46 PB、47 涨停价、48 跌停价
                "turnover": _f(38),
                "pe": _f(39),
                "float_mv": _f(44),
                "total_mv": _f(45),
                "pb": _f(46),
                "limit_up": _f(47),
                "limit_down": _f(48),
            }
        except Exception as e:
            print(f"[quote {symbol}] 失败: {e}")
            return None
    return cached(f"quote:{symbol}", 300, _do)


def _classify_news(title: str, summary: str) -> str:
    """按关键词把新闻归到大事件类别。"""
    text = (title or "") + " " + (summary or "")
    rules = [
        ("人事", ["辞职", "离任", "聘任", "任命", "当选", "董事会", "监事会",
                 "总经理", "副总裁", "高管", "换届", "辞任", "董事长"]),
        ("财报", ["净利润", "营业收入", "业绩预告", "财报", "年报", "季报",
                 "中报", "业绩快报", "分红", "派息", "每股收益"]),
        ("战略", ["战略合作", "签约", "达成合作", "框架协议", "联手", "生态",
                 "布局", "进军", "切入", "并购", "收购", "重组"]),
        ("产品", ["发布", "推出", "上线", "新品", "新一代", "首发", "量产",
                 "交付", "投产", "交付量", "订单", "中标"]),
        ("政策", ["政策", "监管", "处罚", "立案", "调查", "问询", "警示函",
                 "违规", "停牌", "复牌", "退市", "摘帽", "ST"]),
        ("资本", ["增发", "回购", "定增", "配股", "股权激励", "员工持股",
                 "解禁", "质押", "冻结", "减持", "增持"]),
    ]
    for label, kws in rules:
        for kw in kws:
            if kw in text:
                return label
    return "动态"


def _fetch_demon_page(keyword: str, page_index: int, page_size: int = 50):
    """东财搜索 API，按页拉最新新闻（接口只存最近约 6 个月）。"""
    cbk = f"cb{page_index}"
    inner_param = {
        "uid": "", "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "default",
            "pageIndex": page_index, "pageSize": page_size,
            "preTag": "<em>", "postTag": "</em>",
        }},
    }
    r = requests.get(
        "http://search-api-web.eastmoney.com/search/jsonp",
        params={"cb": cbk, "param": json.dumps(inner_param, ensure_ascii=False)},
        headers={"User-Agent": "Mozilla/5.0",
                 "Referer": f"https://so.eastmoney.com/news/s?keyword={keyword}"},
        timeout=15,
    )
    text = r.text.replace(cbk, "").strip().strip("()")
    try:
        data = json.loads(text)
    except Exception:
        return []
    items = data.get("result", {}).get("cmsArticleWebOld") or []
    for it in items:
        it["title"] = it.get("title", "").replace("<em>", "").replace("</em>", "")
        it["content"] = it.get("content", "").replace("<em>", "").replace("</em>", "")
    return items


def _clean_text(t: str) -> str:
    """反转 unicode 转义 + 去掉 <em> 标签。"""
    try:
        t = json.loads(f'"{t}"')
    except Exception:
        pass
    return re.sub(r"</?em>", "", t).strip()


def _fetch_sogou(keyword: str):
    """搜狗新闻搜索，返回 [{title, time, source, url}]。"""
    r = requests.get(
        "https://news.sogou.com/news?query=" + keyword + "&mode=1&tsn=1",
        headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    r.encoding = "utf-8"
    text = r.text
    items = []
    titles = re.findall(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S)
    times = re.findall(r'<span[^>]*class="[^"]*time[^"]*"[^>]*>(.*?)</span>', text, re.S)
    for i, (href, title_html) in enumerate(titles):
        clean = re.sub(r"<.*?>", "", title_html).strip()
        if len(clean) < 8:
            continue
        tm = re.sub(r"<.*?>", "", times[i]).strip() if i < len(times) else ""
        items.append({"title": clean, "time": tm, "source": "搜狗新闻", "url": href})
    return items


def _fetch_toutiao(keyword: str):
    """今日头条移动搜索，返回 [{title, summary, time, source, url}]。"""
    r = requests.get(
        "https://m.toutiao.com/search",
        params={"keyword": keyword, "pd": "information"},
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                               "AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"},
        timeout=15, allow_redirects=True)
    r.encoding = "utf-8"
    text = r.text
    items = []
    for m in re.finditer(r'\{"auth_type"', text):
        block = text[m.start():m.start() + 3000]
        title_m = re.search(r'"title"\s*:\s*"([^"]+)"', block)
        abstract_m = re.search(r'"abstract"\s*:\s*"([^"]*)"', block)
        datetime_m = re.search(r'"datetime"\s*:\s*"([^"]+)"', block)
        media_m = re.search(r'"media_name"\s*:\s*"([^"]+)"', block)
        group_m = re.search(r'"group_id"\s*:\s*"(\d+)"', block)
        if not title_m:
            continue
        title = _clean_text(title_m.group(1))
        if len(title) < 8:
            continue
        items.append({
            "title": title,
            "summary": _clean_text(abstract_m.group(1))[:120] if abstract_m else "",
            "time": datetime_m.group(1) if datetime_m else "",
            "source": media_m.group(1) if media_m else "今日头条",
            "url": f"http://m.toutiao.com/group/{group_m.group(1)}/" if group_m else "",
        })
    return items


def fetch_news(symbol: str, limit: int = 60, days: int = 180, per_cat: int = 15):
    """公司大事：多源新闻（东财搜索 + 搜狗 + 今日头条）→ 去重 → 分类均衡采样。

    数据源：
      - 东财搜索 API（550 条 / 近 6 个月，标签为各媒体名）
      - 搜狗新闻搜索（腾讯系，独立于东财）
      - 今日头条移动搜索（头条自有 + 第一财经 / 证券日报等）
    所有条目标记来源，页面上可见"来自哪家"。

    返回 (items, total)：items 为采样后的列表（每类最多 per_cat 条、
    总计最多 limit 条，按时间倒序），total 为去重后命中的原始总条数。
    排序/筛选由前端按钮完成，这里不再做"每类只留 3 条"的强压缩。
    """
    def _do():
        import time as _time
        all_items = []
        seen_titles = set()
        now = datetime.now()
        cutoff = now - timedelta(days=days)

        # 数据源1：东财搜索 API（逐页拉，最多 11 页）
        for page in range(1, 12):
            try:
                items = _fetch_demon_page(symbol, page)
            except Exception as e:
                print(f"[news {symbol}] 东财第 {page} 页失败: {e}")
                break
            if not items:
                break
            for row in items:
                title = str(row.get("title", "")).strip()
                content = str(row.get("content", "")).strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                t_raw = str(row.get("date", ""))
                try:
                    t = datetime.strptime(t_raw, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    t = now
                if t < cutoff:
                    continue
                all_items.append({
                    "title": title,
                    "summary": content[:120],
                    "time": t_raw,
                    "source": str(row.get("mediaName", row.get("source", "东财"))),
                    "url": str(row.get("url", row.get("link", ""))),
                    "category": _classify_news(title, content),
                })
            _time.sleep(0.2)

        # 数据源2：搜狗新闻
        try:
            for it in _fetch_sogou(symbol):
                if it["title"] in seen_titles:
                    continue
                seen_titles.add(it["title"])
                all_items.append({
                    "title": it["title"],
                    "summary": "",
                    "time": it["time"],
                    "source": it["source"],
                    "url": it["url"],
                    "category": _classify_news(it["title"], it["title"]),
                })
        except Exception as e:
            print(f"[news {symbol}] 搜狗失败: {e}")

        # 数据源3：今日头条
        try:
            for it in _fetch_toutiao(symbol):
                if it["title"] in seen_titles:
                    continue
                seen_titles.add(it["title"])
                all_items.append({
                    "title": it["title"],
                    "summary": it["summary"],
                    "time": it["time"],
                    "source": it["source"],
                    "url": it["url"],
                    "category": _classify_news(it["title"], it["summary"]),
                })
        except Exception as e:
            print(f"[news {symbol}] 头条失败: {e}")

        # 降级：三个源都没数据时，用 AkShare 兜底
        if not all_items:
            try:
                df = ak.stock_news_em(symbol=symbol)
                if df is not None and not df.empty:
                    for _, rr in df.iterrows():
                        title = str(rr.get("新闻标题", "")).strip()
                        if not title or title in seen_titles:
                            continue
                        seen_titles.add(title)
                        content = str(rr.get("新闻内容", "")).strip()
                        all_items.append({
                            "title": title,
                            "summary": content[:120],
                            "time": str(rr.get("发布时间", "")),
                            "source": str(rr.get("文章来源", "")),
                            "url": str(rr.get("新闻链接", "")),
                            "category": _classify_news(title, content),
                        })
            except Exception as e:
                print(f"[news {symbol}] 降级 AkShare 也失败: {e}")

        # 分类均衡采样：每类最多保留 per_cat 条（组内时间倒序），
        # 合并后整体按时间倒序，总量截到 limit。
        # （具体排序/筛选交给页面上的按钮，这里只做"去重 + 限量"。）
        total = len(all_items)
        by_cat = {}
        for it in all_items:
            by_cat.setdefault(it["category"], []).append(it)
        result = []
        for label, group in by_cat.items():
            group.sort(key=lambda x: x["time"], reverse=True)
            result.extend(group[:per_cat])
        result.sort(key=lambda x: x["time"], reverse=True)
        return result[:limit], total
    return cached(f"news:{symbol}", 3600, _do)


def annotate_news_movers(news: list, kline: list):
    """给每条新闻标注发布当天的股价变动方向 + 事件后 1/3/5/20 日累计涨跌幅。

    方法（事件研究法，日收益口径）：
      新闻日收盘价 vs 前一交易日收盘价。
    新闻可能发布在非交易日（周末/节假日），此时取 ≤ 新闻日的最近一个交易日，
    其前一根 K 线即"前一交易日"。无当日行情数据（太早/拉取失败）则不标。
    """
    # 日期升序排列，建立 date -> close 映射
    bars = sorted(kline, key=lambda x: x["date"])
    dates = [b["date"] for b in bars]
    closes = {b["date"]: b["close"] for b in bars}

    import bisect
    for it in news:
        it["mover"] = None
        news_date = (it.get("time") or "")[:10]
        if not news_date or len(news_date) != 10:
            continue
        idx = bisect.bisect_right(dates, news_date) - 1  # ≤ 新闻日的最近交易日
        if idx < 1:
            continue  # 没有当天或没有前一交易日数据
        d_today, d_prev = dates[idx], dates[idx - 1]
        c_today, c_prev = closes[d_today], closes[d_prev]
        if c_prev <= 0:
            continue
        pct = round((c_today - c_prev) / c_prev * 100, 2)
        if pct > 0:
            d = "up"
        elif pct < 0:
            d = "down"
        else:
            d = "flat"
        # 事件后 N 个交易日累计涨跌幅（相对事件日收盘），不足 N 天则跳过
        after = {}
        for n in (1, 3, 5, 20):
            j = idx + n
            if j < len(dates):
                cn = closes[dates[j]]
                after[n] = {
                    "pct": round((cn - c_today) / c_today * 100, 2) if c_today else 0.0,
                    "date": dates[j],
                    "close": cn,
                }
        it["mover"] = {"dir": d, "pct": pct, "date": d_today,
                       "close": c_today, "prev": c_prev, "after": after}
    return news


def stock_name(symbol: str) -> str:
    """从腾讯实时行情接口取股票简称，失败显示代码。"""
    quote = fetch_quote(symbol)
    return quote["name"] if quote and quote.get("name") else symbol


def fetch_recommend(limit: int = 10):
    """腾讯批量行情拉 60 只热门股，按涨跌幅/成交额/换手率分榜。失败返回空列表。"""
    def _do():
        try:
            pool = [
                'sh600519','sz000001','sz300750','sh688981','sz002594','sh600036',
                'sh601318','sz000333','sh600030','sz002415','sh601899','sz300059',
                'sh600028','sz000725','sh600111','sz002028','sh601012','sz300015',
                'sh600036','sz000625','sh601668','sz300033','sh688111','sz002714',
                'sh603288','sz002475','sh601127','sz000063','sh600585','sz300274',
                'sh601633','sz002460','sh600000','sh601398','sz000002','sh600887',
                'sh600900','sz000858','sh600276','sz300122','sh688185','sz002049',
                'sh601088','sz000568','sh600048','sh601888','sh600809','sz002074',
                'sh600360','sz000338','sh601988','sz300438','sh603986','sz002195',
                'sh688390','sz300999','sh601138','sz000065','sh600196','sz300124',
            ]
            url = "https://qt.gtimg.cn/q=" + ",".join(pool)
            text = requests.get(url, timeout=15).content.decode("gbk", errors="replace")
            rows = []
            for line in text.split(";"):
                if "~" not in line:
                    continue
                f = line.split("~")
                if len(f) < 39:
                    continue
                try:
                    code = f[2].zfill(6)[-6:]
                    rows.append({
                        "symbol": code,
                        "name": f[1],
                        "price": float(f[3]) if f[3] else 0.0,
                        "pct": float(f[32]) if f[32] else 0.0,
                        "turnover": float(f[37]) if f[37] else 0.0,
                        "turnover_rate": float(f[38]) if f[38] else 0.0,
                    })
                except (ValueError, IndexError):
                    continue
            if not rows:
                return {}
            # 四个榜各自独立拷贝，避免 rank 字段互相覆盖
            boards = {
                "gain": sorted(rows, key=lambda x: x["pct"], reverse=True)[:limit],
                "lose": sorted(rows, key=lambda x: x["pct"])[:limit],
                "volume": sorted(rows, key=lambda x: x["turnover"], reverse=True)[:limit],
                "turnover": sorted(rows, key=lambda x: x["turnover_rate"], reverse=True)[:limit],
            }
            result = {}
            for board, data in boards.items():
                picked = []
                for i, r in enumerate(data):
                    item = dict(r)          # 浅拷贝，每个榜互不影响
                    item["rank"] = i + 1
                    item["board"] = board
                    picked.append(item)
                result[board] = picked
            return result
        except Exception as e:
            print(f"[recommend] 失败: {e}")
            return {}
    return cached("recommend", 600, _do)


def fetch_code_name_table():
    """全市场代码-名称映射（缓存 1 小时）。

    三层降级，保证名称搜索可用：
    ① 东财 clist 直连（全市场约 5570 只，并发拉 56 页，约 5 秒）
    ② AkShare stock_info_a_code_name（东财，偶尔限流）
    ③ 腾讯 60 只热门股池（保证搜索基本可用）
    """
    def _fetch_eastmoney_direct():
        """东财 clist 逐页拉全市场 A 股（每页 100，约 56 页）。"""
        import concurrent.futures
        base = ("https://82.push2.eastmoney.com/api/qt/clist/get?"
                "pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
                "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14")

        def fetch_page(pn):
            r = requests.get(base.format(pn=pn),
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            rows = (r.json().get("data") or {}).get("diff") or []
            return [(str(x.get("f12", "")).zfill(6)[-6:], str(x.get("f14", "")))
                    for x in rows]

        name_to_code, code_to_name = {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch_page, pn) for pn in range(1, 57)]
            for f in futures:
                try:
                    for c, n in f.result():
                        if c and n:
                            name_to_code[n] = c
                            code_to_name[c] = n
                except Exception:
                    continue
        return name_to_code, code_to_name

    def _do():
        # ① 东财直连
        try:
            n2c, c2n = _fetch_eastmoney_direct()
            if len(c2n) > 1000:
                print(f"[code_name] 东财直连成功: {len(c2n)} 只")
                return {"name_to_code": n2c, "code_to_name": c2n}
            print(f"[code_name] 东财直连只拿到 {len(c2n)} 只，降级 AkShare")
        except Exception as e:
            print(f"[code_name] 东财直连失败: {e}")

        # ② AkShare
        try:
            df = ak.stock_info_a_code_name()
            if df is not None and not df.empty:
                n2c, c2n = {}, {}
                for _, r in df.iterrows():
                    c = str(r["code"]).zfill(6)[-6:]
                    n = str(r["name"])
                    n2c[n] = c
                    c2n[c] = n
                print(f"[code_name] AkShare 备源成功: {len(c2n)} 只")
                return {"name_to_code": n2c, "code_to_name": c2n}
        except Exception as e:
            print(f"[code_name] AkShare 备源失败: {e}")

        # ③ 腾讯热门股池兜底
        try:
            pool = [
                'sh600519','sz000001','sz300750','sh688981','sz002594','sh600036',
                'sh601318','sz000333','sh600030','sz002415','sh601899','sz300059',
                'sh600028','sz000725','sh600111','sz002028','sh601012','sz300015',
                'sh600036','sz000625','sh601668','sz300033','sh688111','sz002714',
                'sh603288','sz002475','sh601127','sz000063','sh600585','sz300274',
                'sh601633','sz002460','sh600000','sh601398','sz000002','sh600887',
                'sh600900','sz000858','sh600276','sz300122','sh688185','sz002049',
                'sh601088','sz000568','sh600048','sh601888','sh600809','sz002074',
                'sh600360','sz000338','sh601988','sz300438','sh603986','sz002195',
                'sh688390','sz300999','sh601138','sz000065','sh600196','sz300124',
            ]
            url = "https://qt.gtimg.cn/q=" + ",".join(pool)
            text = requests.get(url, timeout=15).content.decode("gbk", errors="replace")
            n2c, c2n = {}, {}
            for line in text.split(";"):
                if "~" not in line:
                    continue
                f = line.split("~")
                if len(f) >= 3:
                    name, code = f[1], f[2].zfill(6)[-6:]
                    if name and code:
                        c2n[code] = name
                        n2c[name] = code
            print(f"[code_name] 腾讯热门股池兜底: {len(c2n)} 只")
            return {"name_to_code": n2c, "code_to_name": c2n}
        except Exception as e:
            print(f"[code_name] 全部源失败: {e}")
            return None
    return cached("code_name", 3600, _do)


def resolve_code(query: str):
    """把用户输入（代码或名称）解析为 6 位代码。
    返回 (symbol, matched_name, error_msg)：
      - 输入是 6 位数字 → 直接作为代码
      - 输入是名称     → 先精确、再模糊匹配
      - 都失败         → symbol 为 None，error_msg 非空
    """
    query = query.strip()
    if re.fullmatch(r"\d{6}", query):
        table = fetch_code_name_table() or {}
        name = (table.get("code_to_name") or {}).get(query, query)
        return query, name, None

    table = fetch_code_name_table()
    if not table:
        return None, None, "名称数据源暂不可用，请直接输入 6 位代码"

    exact = (table.get("name_to_code") or {}).get(query)
    if exact:
        return exact, query, None

    # 模糊匹配：名称包含输入，或输入包含名称
    candidates = [
        (n, c) for c, n in (table.get("name_to_code") or {}).items()
        if query in n or n in query
    ]
    if len(candidates) == 1:
        return candidates[0][1], candidates[0][0], None
    if len(candidates) > 1:
        # 取代码排序稳定的前 5 个，供用户选择
        top = sorted(candidates, key=lambda x: x[1])[:5]
        return None, None, (
            f"找到多个名称含“{query}”的股票，请选一个："
            + " · ".join(f"{n}({c})" for n, c in top)
        )
    return None, None, f"未找到名称包含“{query}”的 A 股股票"


def suggest(q: str, limit: int = 8):
    """搜索建议：名称包含 q，或代码以 q 开头。返回 [{symbol, name}] 列表。"""
    q = q.strip()
    if not q:
        return []
    table = fetch_code_name_table()
    if not table:
        return []
    name_to_code = table.get("name_to_code") or {}
    code_to_name = table.get("code_to_name") or {}
    seen = set()
    results = []
    if re.fullmatch(r"\d+", q):
        # 输入是纯数字：优先代码前缀匹配
        for c, n in code_to_name.items():
            if c.startswith(q) and c not in seen:
                seen.add(c)
                results.append({"symbol": c, "name": n})
                if len(results) >= limit:
                    return results
    for n, c in name_to_code.items():
        if q in n and c not in seen:
            seen.add(c)
            results.append({"symbol": c, "name": n})
            if len(results) >= limit:
                break
    return results


def build_dict():
    """全市场股票按拼音首字母 A-Z 分组。返回 {letter: [{symbol, name}]}。"""
    def _do():
        table = fetch_code_name_table()
        if not table:
            return {}
        code_to_name = table.get("code_to_name") or {}
        if not code_to_name:
            return {}
        # pypinyin 不可用时，退化为按代码排序（首字母归入 "A" 或 "?"）
        try:
            from pypinyin import pinyin, Style
            use_py = True
        except Exception:
            use_py = False

        groups = {}
        for code, name in code_to_name.items():
            if use_py:
                py = pinyin(name[0], style=Style.FIRST_LETTER, heteronym=False)
                letter = py[0][0].upper() if py and py[0] else "?"
            else:
                letter = "?"
            if not letter.isalpha():
                letter = "?"
            groups.setdefault(letter, []).append({"symbol": code, "name": name})
        # 每组内按代码升序
        for lst in groups.values():
            lst.sort(key=lambda x: x["symbol"])
        return groups
    return cached("dict_groups", 3600, _do)


# ------------------------------------------------------------------
# 路由
# ------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", recommend=fetch_recommend(10),
                           sim_overview=simdata.get_overview(),
                           watchlist=watchlist())


@app.route("/dict")
def dict_page():
    groups = build_dict()
    total = sum(len(v) for v in groups.values())
    # 按字母顺序输出：[(letter, items), ...]
    ordered = []
    for k in sorted(groups.keys()):
        if k != "?":
            ordered.append((k, groups[k]))
    if "?" in groups:
        ordered.append(("?", groups["?"]))
    return render_template("dict.html", ordered=ordered, total=total)


@app.route("/suggest")
def suggest_api():
    q = request.args.get("q", "").strip()
    return json.dumps(suggest(q), ensure_ascii=False), 200, {"Content-Type": "application/json"}


@app.route("/query", methods=["GET", "POST"])
def query():
    q = (request.args.get("q", "").strip()
         if request.method == "GET"
         else request.form.get("q", "").strip())
    symbol, matched_name, err = resolve_code(q)
    if symbol is None:
        return (render_template("index.html",
                                error=err or "请输入 6 位代码或股票名称，例如 600519 / 贵州茅台",
                                recommend=fetch_recommend(10),
                                sim_overview=simdata.get_overview(),
                                watchlist=watchlist()), 400)

    kline = fetch_kline(symbol)
    if kline is None:
        return (render_template("index.html",
                                error=f"代码 {symbol} 暂无行情数据，请确认输入正确",
                                recommend=fetch_recommend(10),
                                sim_overview=simdata.get_overview(),
                                watchlist=watchlist()), 404)

    raw_news, news_total = fetch_news(symbol, days=180)
    news = annotate_news_movers(raw_news, kline)
    # 类别统计（供前端筛选按钮显示数量），按固定优先级排序
    cat_priority = ["人事", "财报", "战略", "产品", "资本", "政策", "动态"]
    cat_counts = {}
    for it in news:
        cat_counts[it["category"]] = cat_counts.get(it["category"], 0) + 1
    news_cats = [c for c in cat_priority if c in cat_counts]
    news_cats += sorted(c for c in cat_counts if c not in cat_priority)
    return render_template("result.html",
                           symbol=symbol,
                           name=matched_name or stock_name(symbol),
                           quote=fetch_quote(symbol),
                           chart_data=json.dumps(kline, ensure_ascii=False),
                           news=news,
                           news_total=news_total,
                           news_days=180,
                           news_cats=news_cats,
                           news_cat_counts=cat_counts,
                           watched=symbol in _watch_load(),
                           error=None)


# ------------------------------------------------------------------
# 自选股（本地 JSON：watchlist.json = ["600519", ...]）
# ------------------------------------------------------------------

WATCH_FILE = Path(__file__).parent / "watchlist.json"
_watch_lock = threading.Lock()


def _watch_load() -> list:
    if WATCH_FILE.exists():
        try:
            v = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


def watchlist() -> list:
    """自选股列表（含实时价/涨跌幅），按加入顺序。"""
    codes = _watch_load()
    if not codes:
        return []
    table = fetch_code_name_table() or {}
    name_map = table.get("code_to_name") or {}
    out = []
    for code in codes:
        q = fetch_quote(code) or {}
        out.append({
            "symbol": code,
            "name": q.get("name") or name_map.get(code, code),
            "price": q.get("price"),
            "pct": q.get("pct_change"),
        })
    return out


@app.route("/watch/add", methods=["POST"])
def watch_add():
    code = (request.form.get("symbol") or request.args.get("symbol") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return json.dumps({"ok": False, "msg": "代码无效"}), 400, {"Content-Type": "application/json"}
    with _watch_lock:
        codes = _watch_load()
        if code not in codes:
            codes.append(code)
            WATCH_FILE.write_text(json.dumps(codes, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return json.dumps({"ok": True, "watched": True, "count": len(codes)}), 200, {"Content-Type": "application/json"}


@app.route("/watch/remove", methods=["POST"])
def watch_remove():
    code = (request.form.get("symbol") or request.args.get("symbol") or "").strip()
    with _watch_lock:
        codes = [c for c in _watch_load() if c != code]
        WATCH_FILE.write_text(json.dumps(codes, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return json.dumps({"ok": True, "watched": False, "count": len(codes)}), 200, {"Content-Type": "application/json"}


# ------------------------------------------------------------------
# 数据导出（CSV，UTF-8 BOM 供 Excel 直接打开）
# ------------------------------------------------------------------

def _csv_response(text: str, filename: str):
    from flask import Response
    resp = Response("\ufeff" + text, mimetype="text/csv")
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


@app.route("/export/kline/<symbol>")
def export_kline(symbol: str):
    kline = fetch_kline(symbol.strip())
    if not kline:
        return "无数据", 404
    period = request.args.get("period", "")
    rows = kline
    if period == "1y":
        rows = kline[-250:]
    elif period == "3m":
        rows = kline[-63:]
    name = stock_name(symbol)
    lines = ["日期,开盘,收盘,最高,最低,成交量"]
    for b in rows:
        lines.append(f"{b['date']},{b['open']},{b['close']},{b['high']},{b['low']},{b['volume']}")
    header = f"# {name}({symbol}) 前复权日K 共{len(rows)}条 导出于{datetime.now():%Y-%m-%d %H:%M}\n"
    return _csv_response(header + "\n".join(lines) + "\n", f"{symbol}_kline.csv")


@app.route("/export/news/<symbol>")
def export_news(symbol: str):
    kline = fetch_kline(symbol.strip()) or []
    items, _ = fetch_news(symbol.strip(), days=180)
    items = annotate_news_movers(items, kline)
    if not items:
        return "无数据", 404

    def esc(s):
        s = str(s or "")
        return '"' + s.replace('"', '""') + '"' if ("," in s or '"' in s or "\n" in s) else s

    lines = ["发布时间,类别,标题,来源,当日涨跌幅%,后1日%,后5日%,链接"]
    for it in items:
        mv = it.get("mover") or {}
        after = mv.get("after") or {}
        lines.append(",".join([
            esc(it.get("time", "")), esc(it.get("category", "")), esc(it.get("title", "")),
            esc(it.get("source", "")),
            str(mv["pct"]) if mv else "",
            str((after.get(1) or {}).get("pct", "")),
            str((after.get(5) or {}).get("pct", "")),
            esc(it.get("url", "")),
        ]))
    header = f"# {stock_name(symbol)}({symbol}) 新闻{len(items)}条 导出于{datetime.now():%Y-%m-%d %H:%M}\n"
    return _csv_response(header + "\n".join(lines) + "\n", f"{symbol}_news.csv")


@app.route("/export/trades")
def export_trades():
    ts = simdata.get_statements(10000)
    lines = ["时间,方向,代码,名称,价格,数量,成交额,手续费,现金流,本笔盈亏"]
    for t in ts:
        lines.append(",".join([
            t.get("time", ""), "买入" if t.get("side") == "buy" else "卖出",
            t.get("symbol", ""), t.get("name", ""),
            str(t.get("price", "")), str(t.get("qty", "")), str(t.get("amount", "")),
            str(t.get("fee", "")), str(t.get("cash_flow", "")),
            "" if t.get("realized") is None else str(t.get("realized")),
        ]))
    header = f"# 模拟盘成交记录 {len(ts)}笔 导出于{datetime.now():%Y-%m-%d %H:%M}\n"
    return _csv_response(header + "\n".join(lines) + "\n", "sim_trades.csv")


# ------------------------------------------------------------------
# 模拟盘：持仓实时盈亏
# ------------------------------------------------------------------

@app.route("/api/sim_pnl")
def api_sim_pnl():
    """持仓按最新价计算的市值/浮动盈亏，供前端刷新总资产。"""
    ov = simdata.get_overview()
    rows, live_mv, live_pnl = [], 0.0, 0.0
    for p in simdata.get_positions():
        q = fetch_quote(p["symbol"])
        price = (q or {}).get("price") or p["cost"]
        mv = round(price * p["qty"], 2)
        pnl = round(mv - p["cost_total"], 2)
        live_mv += mv
        live_pnl += pnl
        rows.append({
            "symbol": p["symbol"], "name": p["name"],
            "price": round(price, 3), "mv": mv, "pnl": pnl,
            "pnl_pct": round(pnl / p["cost_total"] * 100, 2) if p["cost_total"] else 0.0,
        })
    total = round(ov["cash"] + live_mv, 2)
    return json.dumps({
        "positions": rows,
        "cash": ov["cash"],
        "market_value": round(live_mv, 2),
        "total": total,
        "pnl": round(total - simdata.INITIAL_CASH, 2),
        "pnl_pct": round((total - simdata.INITIAL_CASH) / simdata.INITIAL_CASH * 100, 2),
        "float_pnl": round(live_pnl, 2),
    }, ensure_ascii=False), 200, {"Content-Type": "application/json"}


# ------------------------------------------------------------------
# 模拟炒股路由
# ------------------------------------------------------------------

import simdata


@app.route("/sim")
def sim_page():
    """模拟盘交易页。URL 带 q 参数时预填代码。"""
    prefill_symbol = request.args.get("symbol", "").strip()
    prefill_name = ""
    prefill_price = None
    if re.fullmatch(r"\d{6}", prefill_symbol):
        table = fetch_code_name_table() or {}
        prefill_name = (table.get("code_to_name") or {}).get(prefill_symbol, "")
        q = fetch_quote(prefill_symbol)
        if q:
            prefill_price = q["price"]
    return render_template(
        "sim.html",
        overview=simdata.get_overview(),
        positions=simdata.get_positions(),
        trade_count=simdata.get_overview()["trade_count"],
        prefill_symbol=prefill_symbol,
        prefill_name=prefill_name,
        prefill_price=prefill_price,
    )


@app.route("/sim/trade", methods=["POST"])
def sim_trade():
    symbol = request.form.get("symbol", "").strip()
    name = request.form.get("name", "").strip()
    side = request.form.get("side", "").strip()
    try:
        price = float(request.form.get("price", "0"))
        qty = int(request.form.get("qty", "0"))
    except ValueError:
        price, qty = 0, 0

    # 取实时昨收（用于涨跌停校验），失败则 prev_close=0 跳过校验
    prev_close = 0.0
    if re.fullmatch(r"\d{6}", symbol):
        q = fetch_quote(symbol)
        if q and q.get("prev_close"):
            prev_close = q["prev_close"]

    ok, msg = simdata.trade(symbol, name, side, price, qty, prev_close=prev_close)
    # 预填：成交后保留代码/名称，方便连续下单；算涨跌停区间供前端显示
    sym = symbol if re.fullmatch(r"\d{6}", symbol) else ""
    lo, hi = simdata.price_limits(sym, prev_close)
    return render_template(
        "sim.html",
        overview=simdata.get_overview(),
        positions=simdata.get_positions(),
        trade_count=simdata.get_overview()["trade_count"],
        prefill_symbol=sym,
        prefill_name=name,
        prefill_price=price if price > 0 else None,
        limit_lo=lo,
        limit_hi=hi,
        flash_ok=ok,
        flash_msg=msg,
    )


@app.route("/sim/statements")
def sim_statements():
    return render_template(
        "statements.html",
        statements=simdata.get_statements(100),
        overview=simdata.get_overview(),
    )


@app.route("/sim/reset", methods=["POST"])
def sim_reset():
    simdata.reset()
    return render_template(
        "sim.html",
        overview=simdata.get_overview(),
        positions=simdata.get_positions(),
        trade_count=0,
        prefill_symbol="",
        prefill_name="",
        flash_ok=True,
        flash_msg="已重置为初始 10 万元",
    )


if __name__ == "__main__":
    import os
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
