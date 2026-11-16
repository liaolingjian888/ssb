#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双色球每期任务（纯标准库，可在 GitHub Actions / 云函数里直接跑）

一次运行完成：
  1. 抓取中国福利彩票官网最新开奖，增量补进 history
  2. 对已登记的上一期 5 组号码判定奖级与盈亏，写入 ledger
  3. 按历史结构约束生成下一期 5 组号码
  4. 写出 data.json 供网页读取
  5. 可选：把结果推送到微信（环境变量 WECOM_WEBHOOK / SERVERCHAN_KEY）

数据全部存在 data/state.json，随仓库一起提交，无需数据库。
"""
import itertools
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"
OUT = ROOT / "data.json"

API = "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TIER = {1: "一等奖", 2: "二等奖", 3: "三等奖", 4: "四等奖", 5: "五等奖", 6: "六等奖", 0: "未中奖"}
FIXED = {4: 200, 5: 10, 6: 5}
DRAW_WEEKDAYS = {1, 3, 6}


# ---------------- 数据 ----------------
def now_cn():
    """北京时间字符串（GitHub runner 默认 UTC）"""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))


def load_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"history": [], "tickets": {}, "ledger": []}


def http_get(url, referer=None, tries=4):
    headers = {"User-Agent": UA, "Accept": "application/json, text/javascript, */*; q=0.01"}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 + i * 2)
    raise RuntimeError(f"请求失败 {url}: {last}")


def fetch_recent_500(year=None, count=6):
    """官网不可用时，用 500.com 当年开奖表兜底"""
    y = year or date.today().year
    url = ("https://datachart.500.com/ssq/history/newinc/history.php"
           f"?start={str(y)[2:]}001&end={str(y)[2:]}999")
    raw = http_get(url, referer="https://datachart.500.com/ssq/history/history.shtml")
    html = raw.decode("gb18030", "replace")
    out = []
    for tr in re.findall(r'<tr class="t_tr1">(.*?)</tr>', html, re.S):
        c = [re.sub(r"<[^>]+>", "", x).replace("\xa0", "").strip()
             for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(c) < 17:
            continue

        def n(v):
            v = re.sub(r"\D", "", v)
            return int(v) if v else 0

        code = "20" + c[1][:2] + c[1][2:]
        out.append({"p": code, "d": c[16],
                    "r": sorted(int(x) for x in c[2:8]), "b": int(c[8]),
                    "p1": n(c[11]), "p1amt": n(c[12]),
                    "p2": n(c[13]), "p2amt": n(c[14]),
                    "sales": n(c[15]), "pool": n(c[10])})
    out.sort(key=lambda x: x["p"])
    return out[-count:]


def fetch_recent(count=6):
    q = (f"{API}?name=ssq&issueCount={count}&issueStart=&issueEnd=&dayStart=&dayEnd="
         f"&pageNo=1&pageSize=30&week=&systemType=PC")
    raw = http_get(q, referer="https://www.cwl.gov.cn/ygkj/wqkjgg/ssq/")
    d = json.loads(raw.decode("utf-8"))
    out = []
    for row in d.get("result") or []:
        pg = {p["type"]: p for p in (row.get("prizegrades") or [])}

        def num(p, k):
            try:
                return int(float(p.get(k) or 0))
            except Exception:
                return 0

        out.append({
            "p": row["code"], "d": row["date"][:10],
            "r": sorted(int(x) for x in row["red"].split(",")),
            "b": int(row["blue"]),
            "p1": num(pg.get(1, {}), "typenum"), "p1amt": num(pg.get(1, {}), "typemoney"),
            "p2": num(pg.get(2, {}), "typenum"), "p2amt": num(pg.get(2, {}), "typemoney"),
            "sales": num(row, "sales"), "pool": num(row, "poolmoney"),
        })
    out.sort(key=lambda x: x["p"])
    return out


# ---------------- 全量历史自举 ----------------
def bootstrap_history():
    """首次运行时在云端自建 3527 期历史：2003-2012 走 500.com，2013 起走福彩官网"""
    hist = {}
    ua500 = {"User-Agent": UA, "Referer": "https://datachart.500.com/ssq/history/history.shtml"}
    for year in range(2003, 2013):
        url = ("https://datachart.500.com/ssq/history/newinc/history.php"
               f"?start={str(year)[2:]}001&end={str(year)[2:]}999")
        try:
            req = urllib.request.Request(url, headers=ua500)
            with urllib.request.urlopen(req, timeout=40) as r:
                html = r.read().decode("gb18030", "replace")
        except Exception as e:
            print(f"  [bootstrap] 500.com {year} 失败 {e}", file=sys.stderr)
            continue
        for tr in re.findall(r'<tr class="t_tr1">(.*?)</tr>', html, re.S):
            c = [re.sub(r"<[^>]+>", "", x).replace("\xa0", "").strip()
                 for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(c) < 17:
                continue

            def n(v):
                v = re.sub(r"\D", "", v)
                return int(v) if v else 0

            code = "20" + c[1][:2] + c[1][2:]
            hist[code] = {"p": code, "d": c[16],
                          "r": sorted(int(x) for x in c[2:8]), "b": int(c[8]),
                          "p1": n(c[11]), "p1amt": n(c[12]),
                          "p2": n(c[13]), "p2amt": n(c[14]),
                          "sales": n(c[15]), "pool": n(c[10])}
        print(f"  [bootstrap] 500.com {year}: 累计 {len(hist)} 期")

    try:
        q = (f"{API}?name=ssq&issueCount=&issueStart=&issueEnd=&dayStart=&dayEnd="
             f"&pageNo=1&pageSize=3000&week=&systemType=PC")
        d = json.loads(http_get(q, referer="https://www.cwl.gov.cn/ygkj/wqkjgg/ssq/").decode("utf-8"))
        rows = d.get("result") or []
        for row in rows:
            pg = {p["type"]: p for p in (row.get("prizegrades") or [])}

            def num(p, k):
                try:
                    return int(float(p.get(k) or 0))
                except Exception:
                    return 0

            hist[row["code"]] = {
                "p": row["code"], "d": row["date"][:10],
                "r": sorted(int(x) for x in row["red"].split(",")), "b": int(row["blue"]),
                "p1": num(pg.get(1, {}), "typenum"), "p1amt": num(pg.get(1, {}), "typemoney"),
                "p2": num(pg.get(2, {}), "typenum"), "p2amt": num(pg.get(2, {}), "typemoney"),
                "sales": num(row, "sales"), "pool": num(row, "poolmoney")}
        print(f"  [bootstrap] 官网补充 {len(rows)} 期")
    except Exception as e:
        print(f"  [bootstrap] 官网失败 {e}", file=sys.stderr)

    return [hist[k] for k in sorted(hist)]


# ---------------- 规则 ----------------
def tier_of(rh, bh):
    if rh == 6:
        return 1 if bh else 2
    if rh == 5:
        return 3 if bh else 4
    if rh == 4:
        return 4 if bh else 5
    if rh == 3:
        return 5 if bh else 0
    return 6 if bh else 0


def amount_of(t, draw):
    if t in FIXED:
        return FIXED[t]
    if t == 1:
        return draw["p1amt"]
    if t == 2:
        return draw["p2amt"]
    return 0


FORM_LIBRARY = {
    # 单张票：红球 6 个 + 蓝球 13 个 = 13 注 × 2 元 = 26 元
    # 蓝球覆盖 13/16 = 81.3%，是 20-26 元内"每期能见到钱"概率最高的单票结构
    "A": {"desc": "单张 6+13（红球 6 个 + 蓝球 13 个）", "budget": 26,
          "forms": [("6+13", "全蓝", 6, 13, "综合评分")]},
}


def choose_form(ledger, window=6):
    """预算 24 元、单张票、结构固定。
    票型切换只改成本与体验，不改中奖概率；预算锁定后没有切换余地。"""
    return "A", "固定 26 元单票方案：红球 6 个 + 蓝球 13 个（蓝球覆盖 81.3%）"


FORMS = [("6+13", "全蓝", 6, 13, "综合评分")]


def blues_of(t):
    b = t.get("b", [])
    return b if isinstance(b, list) else [b]


def notes_of(t):
    """复式拆成多少注"""
    return math.comb(len(t["r"]), 6) * len(blues_of(t))


def expand(t):
    """展开成所有单注: (6红, 蓝)"""
    for rs in itertools.combinations(sorted(t["r"]), 6):
        for b in blues_of(t):
            yield list(rs), b


def review(tickets, draw):
    """按票展开逐注判定，返回每票汇总 + 全局汇总"""
    win = set(draw["r"])
    per, total_cost, total_prize = [], 0, 0
    for t in tickets:
        hits = []
        for rs, b in expand(t):
            rh = len(set(rs) & win)
            bh = int(b == draw["b"])
            tier = tier_of(rh, bh)
            hits.append({"r": rs, "b": b, "rh": rh, "bh": bh,
                         "tier": tier, "prize": amount_of(tier, draw)})
        prize = sum(h["prize"] for h in hits)
        cost = len(hits) * 2
        best = min((h["tier"] for h in hits if h["tier"]), default=0)
        per.append({"n": t["n"], "type": t.get("type", f'{len(t["r"])}+{len(blues_of(t))}'),
                    "r": sorted(t["r"]), "b": blues_of(t), "notes": len(hits),
                    "cost": cost, "prize": prize, "best": TIER[best],
                    "win_notes": sum(1 for h in hits if h["tier"]),
                    "max_rh": max(h["rh"] for h in hits),
                    "blue_hits": sum(h["bh"] for h in hits),
                    "hits": hits})
        total_cost += cost
        total_prize += prize
    best_all = min((p["best"] for p in per if p["best"] != "未中奖"), default="未中奖",
                   key=lambda x: list(TIER.values()).index(x) if x in TIER.values() else 99)
    return {"tickets": per, "cost": total_cost, "prize": total_prize,
            "pl": total_prize - total_cost, "best": best_all,
            "win_notes": sum(p["win_notes"] for p in per),
            "notes": sum(p["notes"] for p in per),
            "blue_hits": sum(p["blue_hits"] for p in per),
            "max_rh": max(p["max_rh"] for p in per)}


# ---------------- 选号 ----------------
def build_pools(history):
    n = len(history)
    f50, f100, fall = {}, {}, {}
    for h in history[-50:]:
        for x in h["r"]:
            f50[x] = f50.get(x, 0) + 1
    for h in history[-100:]:
        for x in h["r"]:
            f100[x] = f100.get(x, 0) + 1
    for h in history:
        for x in h["r"]:
            fall[x] = fall.get(x, 0) + 1

    om = {}
    for ball in range(1, 34):
        gap = n
        for i in range(n - 1, -1, -1):
            if ball in history[i]["r"]:
                gap = n - 1 - i
                break
        om[ball] = gap

    score = {}
    for b in range(1, 34):
        hot = (f100.get(b, 0) - 100 * 6 / 33) / (100 * 6 / 33)
        score[b] = hot * 100 + (om[b] - 35) * 0.5

    last = history[-1]["r"]
    zone = (sorted(range(1, 12), key=lambda x: -fall.get(x, 0))[:5]
            + sorted(range(12, 23), key=lambda x: -fall.get(x, 0))[:5]
            + sorted(range(23, 34), key=lambda x: -fall.get(x, 0))[:5])
    return {
        "f50": f50, "f100": f100, "fall": fall, "om": om, "score": score, "last": last,
        "pools": [
            ("热号延续", sorted(range(1, 34), key=lambda x: -(f100.get(x, 0) * 2 + f50.get(x, 0) * 3))[:14]),
            ("冷号回补", sorted(range(1, 34), key=lambda x: -om[x])[:14]),
            ("综合评分", sorted(range(1, 34), key=lambda x: -score[x])[:14]),
            ("上期邻号", sorted(set(last + [x + 1 for x in last if x < 33] + [x - 1 for x in last if x > 1]))),
            ("区间均衡", sorted(zone)),
        ],
    }


def struct_ok(nums, n_red):
    s = sum(nums)
    if n_red == 6:
        if not (92 <= s <= 112):
            return False
        o = sum(x % 2 for x in nums)
        if o in (0, 1, 5, 6):
            return False
        zmax = 3
        cons = 1
    else:                      # 7 红：均值 119，取 25%-75% 分位
        if not (105 <= s <= 133):
            return False
        o = sum(x % 2 for x in nums)
        if o not in (3, 4, 5):
            return False
        zmax = 4
        cons = 2
    z = [sum(1 for x in nums if x <= 11), sum(1 for x in nums if 12 <= x <= 22),
         sum(1 for x in nums if x >= 23)]
    if max(z) > zmax or min(z) == 0:
        return False
    return sum(1 for i in range(len(nums) - 1) if nums[i + 1] - nums[i] == 1) <= cons


def make_sets(history, seed, forms=None):
    """按给定票型生成号码；forms 缺省为 FORMS"""
    forms = forms or FORMS
    pools = build_pools(history)
    drawn = {tuple(h["r"]) for h in history}
    rng = random.Random(seed)

    bh = sorted(range(1, 17), key=lambda b: -pools["fall"].get(b, 0))
    bo = sorted(range(1, 17), key=lambda b: -pools["om"][b])

    pool_map = dict((name, pool) for name, pool in pools["pools"])

    # 蓝球候选序列：热号、冷号交替，保证 4 张票的 6 个蓝球互不相同
    order = []
    for i in range(16):
        for b in (bh[i % 16], bo[i % 16]):
            if b not in order:
                order.append(b)

    sets = []
    for typ, tag, n_red, n_blue, strategy in forms:
        pool = sorted(set(pool_map.get(strategy, [])))
        if len(pool) < n_red:
            continue
        pick = None
        for _ in range(80000):
            cand = sorted(rng.sample(pool, n_red))
            if not struct_ok(cand, n_red):
                continue
            if n_red == 6 and tuple(cand) in drawn:
                continue
            if any(len(set(cand) & set(s["r"])) > 3 for s in sets):
                continue
            pick = cand
            break
        if not pick:
            continue
        # 蓝球：按热度/遗漏交替顺序顺次取 n_blue 个
        slot = sum(len(x["b"]) for x in sets)
        blues = order[slot:slot + n_blue] or order[:n_blue]
        sets.append({"n": f"{typ} {tag}", "type": typ, "r": pick,
                     "b": sorted(blues), "strategy": strategy})
    return sets


def next_period(draw):
    d = date.fromisoformat(draw["d"])
    nd = d
    for k in range(1, 8):
        nd = d + timedelta(days=k)
        if nd.weekday() in DRAW_WEEKDAYS:
            break
    if nd.year > d.year:
        return f"{nd.year}001", nd.isoformat()
    return f"{draw['p'][:4]}{int(draw['p'][4:]) + 1:03d}", nd.isoformat()


# ---------------- 推送 ----------------
def push(title, text):
    """优先读环境变量（GitHub Secrets），其次读 data/push.json；text 为 Markdown"""
    key = os.environ.get("WECOM_WEBHOOK", "")
    sck = os.environ.get("SERVERCHAN_KEY", "")
    cfg_f = ROOT / "data" / "push.json"
    if (not key or not sck) and cfg_f.exists():
        try:
            cfg = json.loads(cfg_f.read_text(encoding="utf-8"))
            key = key or cfg.get("wecom_webhook", "")
            sck = sck or cfg.get("serverchan_key", "")
        except Exception as e:
            print(f"[推送] 读 data/push.json 失败：{e}", file=sys.stderr)

    sent = []
    if sck:
        url = f"https://sctapi.ftqq.com/{sck}.send"
        data = urllib.parse.urlencode({
            "title": title,
            "desp": text,
            "short": title,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
        sent.append("serverchan:" + body[:60])
    if key:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
        payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{text}"}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
        sent.append("wecom:" + body[:60])
    return sent


# ---------------- 主流程 ----------------
def main():
    st = load_state()
    if len(st.get("history") or []) < 1000:
        print("[bootstrap] 历史库不足，开始全量自举…")
        st["history"] = bootstrap_history()
        print(f"[bootstrap] 完成，共 {len(st['history'])} 期")
    if st.get("ver", 1) < 2:
        last_drawn = st["history"][-1]["p"] if st.get("history") else ""
        stale = [k for k in list(st.get("tickets", {})) if k > last_drawn]
        for k in stale:
            st["tickets"].pop(k, None)
        st["ver"] = 2
        print(f"[迁移] 升级为复式玩法(7+1/6+2)，已清理未开奖的旧单式票 {stale}")
    print(f"[state] 历史 {len(st['history'])} 期 / 已下注 {len(st['tickets'])} 期 / 台账 {len(st['ledger'])} 条")

    today = date.today().isoformat()
    is_draw_day = date.today().weekday() in DRAW_WEEKDAYS
    recent = None
    for attempt in range(5):
        try:
            recent = fetch_recent(6)
        except Exception as e:
            recent = None
            if attempt == 0:
                print(f"[warn] 官网开奖接口失败：{e}", file=sys.stderr)
        if recent and (not is_draw_day or recent[-1]["d"] >= today):
            break
        if attempt < 4:
            print(f"[等待] 第 {attempt+1} 次抓取未取得今日开奖，80 秒后重试…")
            time.sleep(80)
    try:
        if not recent:
            raise RuntimeError("多次抓取均为空")
    except Exception as e:
        print(f"[warn] 官网开奖接口失败：{e}", file=sys.stderr)
        recent = []
        for y in (date.today().year, date.today().year - 1):
            try:
                recent = fetch_recent_500(y, 6)
                print(f"[warn] 已回退到 500.com（{y} 年）")
                break
            except Exception as e2:
                print(f"[warn] 500.com {y} 也失败：{e2}", file=sys.stderr)
    have = {h["p"] for h in st["history"]}
    added = [x for x in recent if x["p"] not in have]
    st["history"].extend(added)
    st["history"].sort(key=lambda x: x["p"])
    print(f"[抓取] 新增 {len(added)} 期，最新 {st['history'][-1]['p']}")

    latest = st["history"][-1]
    detail = None
    reviewed_now = False

    # 复盘
    if latest["p"] in st["tickets"]:
        if any(x["p"] == latest["p"] for x in st["ledger"]):
            print(f"[复盘] {latest['p']} 已复盘，跳过")
            detail = [x for x in st["ledger"] if x["p"] == latest["p"]][0]
        else:
            rv = review(st["tickets"][latest["p"]], latest)
            detail = {
                "p": latest["p"], "d": latest["d"], "r": latest["r"], "b": latest["b"],
                "cost": rv["cost"], "prize": rv["prize"], "pl": rv["pl"], "best": rv["best"],
                "notes": rv["notes"], "win_notes": rv["win_notes"],
                "tickets": [{k: v for k, v in t.items() if k != "hits"} for t in rv["tickets"]],
                "hits": [h for t in rv["tickets"] for h in t["hits"] if h["tier"]],
                "ts": now_cn(),
            }
            st["ledger"].append(detail)
            reviewed_now = True
            print(f"[复盘] {latest['p']} {rv['best']} 中{rv['win_notes']}注 盈亏 {rv['pl']:+}")
    else:
        print(f"[复盘] {latest['p']} 无投注记录")

    # 生成下期
    nxt, nd = next_period(latest)
    form_key, form_why = choose_form(st["ledger"])
    form = FORM_LIBRARY[form_key]
    if nxt in st["tickets"]:
        sets = st["tickets"][nxt]
        saved_key = st.get("form_pick", {}).get(nxt)
        if saved_key and saved_key in FORM_LIBRARY:
            form_key = saved_key
            form = FORM_LIBRARY[form_key]
            form_why = st.get("form_why", {}).get(nxt, form_why)
        else:
            st.setdefault("form_pick", {})[nxt] = form_key
            st.setdefault("form_why", {})[nxt] = form_why
        print(f"[选号] {nxt} 沿用（票型 {form_key}）")
    else:
        sets = make_sets(st["history"], int(nxt), form["forms"])
        st["tickets"][nxt] = sets
        st.setdefault("form_pick", {})[nxt] = form_key
        st.setdefault("form_why", {})[nxt] = form_why
        print(f"[选号] 生成 {nxt}（{nd}）{len(sets)} 张 · 票型 {form_key} — {form_why}")

    # 统计
    cost_all = sum(x["cost"] for x in st["ledger"])
    prize_all = sum(x["prize"] for x in st["ledger"])
    strat = {}
    for e in st["ledger"]:
        for t in e.get("tickets", []):
            a = strat.setdefault(t["n"], {"n": 0, "hit": 0, "prize": 0, "notes": 0})
            a["n"] += 1
            a["notes"] += t["notes"]
            a["hit"] += t["max_rh"]
            a["prize"] += t["prize"]

    # 输出
    payload = {
        "updated": now_cn(),
        "next": {"p": nxt, "d": nd, "sets": sets,
                 "form": form_key, "form_desc": form["desc"], "form_why": form_why},
        "latest": ({"p": latest["p"], "d": latest["d"], "r": latest["r"], "b": latest["b"],
                    "p1": latest["p1"], "p1amt": latest["p1amt"],
                    "sales": latest["sales"], "pool": latest["pool"]}),
        "last_review": st["ledger"][-1] if st["ledger"] else None,
        "stats": {
            "periods": len(st["ledger"]), "cost": cost_all, "prize": prize_all,
            "pl": prize_all - cost_all,
            "rate": round(prize_all / cost_all * 100, 1) if cost_all else 0,
            "strategy": [{"name": k, "n": v["n"], "notes": v["notes"],
                          "avg_hit": round(v["hit"] / v["n"], 2),
                          "prize": v["prize"]} for k, v in
                         sorted(strat.items(), key=lambda kv: -kv[1]["hit"] / kv[1]["n"])],
        },
        "ledger": list(reversed(st["ledger"]))[:60],
        "history": st["history"],
    }
    STATE.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[输出] {OUT}")

    _wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    nxt_week = _wd[date.fromisoformat(nd).weekday()]

    # 微信推送（Markdown，Server酱/企微机器人都能渲染）
    def fmt_ticket(t, hit=None):
        reds = " ".join(f"{x:02d}" for x in t["r"])
        blues = " ".join(f"{b:02d}" for b in t["b"])
        return f"**{reds}**　＋　**{blues}**"

    def build_message():
        total_notes = sum(notes_of(t) for t in sets)
        md = [f"### 第 {nxt} 期　{nxt_week}", ""]
        md.append(f"{len(sets)} 张票 · {total_notes} 注 · {total_notes * 2} 元 · 开奖 21:15")
        md.append(f"票型 {form_key}：{form['desc']}")
        md.append("")
        md.append(f"> {form_why}")
        md.append("")
        for t in sets:
            md.append(f"- **{t['n']}**（{notes_of(t)} 注）　{fmt_ticket(t)}")
        if detail:
            md += ["", "---", "", f"### 第 {detail['p']} 期复盘", ""]
            md.append("开奖　**" + " ".join(f"{x:02d}" for x in detail["r"]) + "**　＋　**"
                      + f"{detail['b']:02d}" + "**")
            md.append("")
            if latest.get("p1amt"):
                md.append(f"一等奖 {latest['p1']} 注 · 单注 {latest['p1amt'] / 10000:.1f} 万")
                md.append("")
            for t in detail.get("tickets", []):
                mark = f"　中奖 {t['win_notes']} 注（{t['best']}）" if t["win_notes"] else "　未中奖"
                md.append(f"- **{t['n']}**　{t['notes']} 注　单注最多中 {t['max_rh']} 个红球{mark}")
            md.append("")
            sign = "+" if detail["pl"] >= 0 else ""
            md.append(f"投入 **{detail['cost']}** 元　回收 **{detail['prize']}** 元　"
                      f"盈亏 **{sign}{detail['pl']}** 元")
            if cost_all:
                md.append("")
                md.append(f"累计 {len(st['ledger'])} 期　投入 {cost_all} 元　回收 {prize_all} 元　"
                          f"盈亏 **{prize_all - cost_all:+}** 元　返奖率 {prize_all / cost_all * 100:.1f}%")
        md += ["", "---", "", "*复式只是把多注装进一张票，每元钱的期望不变；"
                   "复盘不改变中奖概率。*"]
        return "\n".join(md)

    if detail:
        title = f"{nxt} 期号码已出 · {detail['p']} {detail['best']}"
    else:
        title = f"{nxt} 期号码已出 · {len(sets)} 张票"

    force = "--force-push" in sys.argv
    try:
        if not (added or reviewed_now or force):
            print("[推送] 本期无新开奖也无新复盘，跳过（避免重复打扰）")
        else:
            r = push(title, build_message())
            print("[推送]", "已发送" if r else "未配置推送（缺 WECOM_WEBHOOK / SERVERCHAN_KEY）")
    except Exception as e:
        print("[推送] 失败", e, file=sys.stderr)


if __name__ == "__main__":
    main()
