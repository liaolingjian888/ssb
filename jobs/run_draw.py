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
import json
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


def review(tickets, draw):
    win = set(draw["r"])
    res = []
    for t in tickets:
        rh = len(set(t["r"]) & win)
        bh = int(t["b"] == draw["b"])
        tier = tier_of(rh, bh)
        res.append({"name": t["n"], "r": t["r"], "b": t["b"],
                    "rh": rh, "bh": bh, "tier": tier, "prize": amount_of(tier, draw)})
    return res


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


def struct_ok(nums):
    s = sum(nums)
    if not (92 <= s <= 112):
        return False
    o = sum(x % 2 for x in nums)
    if o in (0, 1, 5, 6):
        return False
    z = [sum(1 for x in nums if x <= 11), sum(1 for x in nums if 12 <= x <= 22),
         sum(1 for x in nums if x >= 23)]
    if max(z) > 3 or min(z) == 0:
        return False
    return sum(1 for i in range(5) if nums[i + 1] - nums[i] == 1) <= 1


def make_sets(history, seed):
    pools = build_pools(history)
    drawn = {tuple(h["r"]) for h in history}
    rng = random.Random(seed)
    sets, sums = [], set()

    bh = sorted(range(1, 17), key=lambda b: -pools["fall"].get(b, 0))
    bo = sorted(range(1, 17), key=lambda b: -pools["om"][b])
    blues = []
    for b in [bo[0], bh[0], bh[1], bh[2], bo[1]]:
        if b not in blues:
            blues.append(b)

    for name, pool in pools["pools"]:
        pool = sorted(set(pool))
        if len(pool) < 6:
            continue
        pick = None
        for _ in range(60000):
            cand = sorted(rng.sample(pool, 6))
            if not struct_ok(cand) or tuple(cand) in drawn:
                continue
            if sum(cand) in sums:
                continue
            if any(len(set(cand) & set(s["r"])) > 2 for s in sets):
                continue
            pick = cand
            break
        if pick:
            sets.append({"n": name, "r": pick, "b": blues[len(sets)]})
            sums.add(sum(pick))
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
    key = os.environ.get("WECOM_WEBHOOK")
    sck = os.environ.get("SERVERCHAN_KEY")
    sent = []
    if key:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
        body = json.dumps({"msgtype": "text", "text": {"content": f"{title}\n{text}"}}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sent.append("wecom:" + r.read().decode()[:40])
    if sck:
        url = f"https://sctapi.ftqq.com/{sck}.send"
        data = urllib.parse.urlencode({"title": title, "desp": text}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            sent.append("serverchan:" + r.read().decode()[:40])
    return sent


# ---------------- 主流程 ----------------
def main():
    st = load_state()
    if len(st.get("history") or []) < 1000:
        print("[bootstrap] 历史库不足，开始全量自举…")
        st["history"] = bootstrap_history()
        print(f"[bootstrap] 完成，共 {len(st['history'])} 期")
    print(f"[state] 历史 {len(st['history'])} 期 / 已下注 {len(st['tickets'])} 期 / 台账 {len(st['ledger'])} 条")

    try:
        recent = fetch_recent(6)
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
    msg = []          # 本期复盘段
    detail = None

    # 复盘
    if latest["p"] in st["tickets"]:
        if any(x["p"] == latest["p"] for x in st["ledger"]):
            print(f"[复盘] {latest['p']} 已复盘，跳过")
            detail = [x for x in st["ledger"] if x["p"] == latest["p"]][0]
        else:
            res = review(st["tickets"][latest["p"]], latest)
            cost = len(res) * 2
            prize = sum(r["prize"] for r in res)
            best = min((r["tier"] for r in res if r["tier"]), default=0)
            detail = {
                "p": latest["p"], "d": latest["d"], "r": latest["r"], "b": latest["b"],
                "cost": cost, "prize": prize, "pl": prize - cost, "best": TIER[best],
                "res": res, "ts": time.strftime("%Y-%m-%d %H:%M"),
            }
            st["ledger"].append(detail)
            print(f"[复盘] {latest['p']} {TIER[best]} 盈亏 {prize-cost:+}")
    else:
        print(f"[复盘] {latest['p']} 无投注记录")

    # 生成下期
    nxt, nd = next_period(latest)
    if nxt in st["tickets"]:
        sets = st["tickets"][nxt]
        print(f"[选号] {nxt} 沿用")
    else:
        sets = make_sets(st["history"], int(nxt))
        st["tickets"][nxt] = sets
        print(f"[选号] 生成 {nxt}（{nd}）{len(sets)} 组")

    # 统计
    cost_all = sum(x["cost"] for x in st["ledger"])
    prize_all = sum(x["prize"] for x in st["ledger"])
    strat = {}
    for e in st["ledger"]:
        for r in e["res"]:
            a = strat.setdefault(r["name"], {"n": 0, "hit": 0, "prize": 0})
            a["n"] += 1
            a["hit"] += r["rh"]
            a["prize"] += r["prize"]

    # 输出
    payload = {
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "next": {"p": nxt, "d": nd, "sets": sets},
        "latest": ({"p": latest["p"], "d": latest["d"], "r": latest["r"], "b": latest["b"],
                    "p1": latest["p1"], "p1amt": latest["p1amt"],
                    "sales": latest["sales"], "pool": latest["pool"]}),
        "last_review": st["ledger"][-1] if st["ledger"] else None,
        "stats": {
            "periods": len(st["ledger"]), "cost": cost_all, "prize": prize_all,
            "pl": prize_all - cost_all,
            "rate": round(prize_all / cost_all * 100, 1) if cost_all else 0,
            "strategy": [{"name": k, "n": v["n"],
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

    # 微信推送文案
    def build_message():
        L = []
        L.append(f"【下期 {nxt}】{nd}")
        for s in sets:
            L.append(f"{s['n']}  " + " ".join(f"{x:02d}" for x in s["r"]) + f" + {s['b']:02d}")
        if detail:
            L.append("")
            L.append(f"—— {detail['p']} 复盘 ——")
            L.append("开奖 " + " ".join(f"{x:02d}" for x in detail["r"]) + f" + {detail['b']:02d}")
            L.append("｜".join(f"{r['name']}中{r['rh']}红" for r in detail["res"]))
            L.append(f"投入{detail['cost']} 回收{detail['prize']} "
                     f"盈亏{detail['pl']:+} 最高{detail['best']}")
            if cost_all:
                rate = prize_all / cost_all * 100
                L.append(f"累计{len(st['ledger'])}期 成本{cost_all} 回收{prize_all} "
                         f"盈亏{prize_all - cost_all:+} 返奖率{rate:.1f}%")
        L.append("")
        L.append("单期未中是数学常态，复盘只改进过程，不提高中奖概率。")
        return "\n".join(L)

    title = f"双色球 {nxt} 期号码" + (f" · {detail['p']} 复盘" if detail else "")
    try:
        r = push(title, build_message())
        print("[推送]", "已发送" if r else "未配置推送（缺 WECOM_WEBHOOK / SERVERCHAN_KEY）")
    except Exception as e:
        print("[推送] 失败", e, file=sys.stderr)


if __name__ == "__main__":
    main()
