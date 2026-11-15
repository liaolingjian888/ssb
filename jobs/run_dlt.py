#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大乐透每期任务（纯标准库）：抓取 → 复盘 → 生成下期 → 写 data_dlt.json

规则：前区 35 选 5 + 后区 12 选 2，单注一等奖 1/21,425,712
奖级：5+2 一等 / 5+1 二等 / 5+0 三等 / 4+2 四等 / 4+1 五等 / 3+2 六等
      4+0 七等 / 3+1·2+2 八等 / 3+0·1+2·2+1·0+2 九等
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
STATE = ROOT / "data" / "dlt_state.json"
OUT = ROOT / "data_dlt.json"

API = "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 固定奖（元）；一、二等奖为浮动奖
FIXED = {3: 10000, 4: 3000, 5: 300, 6: 200, 7: 100, 8: 15, 9: 5}
TIER = {1: "一等奖", 2: "二等奖", 3: "三等奖", 4: "四等奖", 5: "五等奖",
        6: "六等奖", 7: "七等奖", 8: "八等奖", 9: "九等奖", 0: "未中奖"}
DRAW_WEEKDAYS = {0, 2, 5}          # 周一 / 周三 / 周六


def now_cn():
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))


def num(v):
    s = str(v or "0").replace(",", "").strip()
    try:
        return int(float(s))
    except Exception:
        return 0


# ---------------- 数据 ----------------
def load_state():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"history": [], "tickets": {}, "ledger": [], "ver": 2}


def http_get(url, referer=None, tries=4):
    h = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
    if referer:
        h["Referer"] = referer
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2 + i * 2)
    raise RuntimeError(f"请求失败 {url}: {last}")


def parse_row(r):
    nums = [int(x) for x in r["lotteryDrawResult"].split()]
    pg = {x["prizeLevel"]: x for x in (r.get("prizeLevelList") or [])}
    p1, p2 = pg.get("一等奖", {}), pg.get("二等奖", {})
    return {"p": r["lotteryDrawNum"], "d": r["lotteryDrawTime"],
            "f": sorted(nums[:5]), "b": sorted(nums[5:7]),
            "p1": num(p1.get("stakeCount")), "p1amt": num(p1.get("stakeAmount")),
            "p2": num(p2.get("stakeCount")), "p2amt": num(p2.get("stakeAmount")),
            "sales": num(r.get("totalSaleAmount")),
            "pool": num(r.get("poolBalanceAfterdraw"))}


def fetch_page(page_no, size=100):
    q = (f"{API}?gameNo=85&provinceId=0&pageSize={size}"
         f"&isVerify=1&pageNo={page_no}")
    d = json.loads(http_get(q, referer="https://www.lottery.gov.cn/").decode("utf-8"))
    v = d.get("value") or {}
    return [parse_row(x) for x in (v.get("list") or [])], v.get("total", 0)


def fetch_recent(count=6):
    rows, _ = fetch_page(1, max(count, 10))
    rows.sort(key=lambda x: x["p"])
    return rows[-count:]


def fetch_recent_500(count=6):
    y = date.today().year
    url = ("https://datachart.500.com/dlt/history/newinc/history.php"
           f"?start={str(y)[2:]}001&end={str(y)[2:]}999")
    html = http_get(url, referer="https://datachart.500.com/dlt/history/history.shtml").decode("gb18030", "replace")
    out = []
    for tr in re.findall(r'<tr class="t_tr1">(.*?)</tr>', html, re.S):
        c = [re.sub(r"<[^>]+>", "", x).replace("\xa0", "").strip()
             for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(c) < 15:
            continue
        # 500.com 大乐透列: 期号 前区5 后区2 奖池 一等注数 一等金额 二等注数 二等金额 ... 日期
        try:
            f = sorted(int(v) for v in c[1:6])
            b = sorted(int(v) for v in c[6:8])
        except Exception:
            continue
        out.append({"p": c[0], "d": c[-1], "f": f, "b": b,
                    "p1": num(c[9]) if len(c) > 9 else 0,
                    "p1amt": num(c[10]) if len(c) > 10 else 0,
                    "p2": num(c[11]) if len(c) > 11 else 0,
                    "p2amt": num(c[12]) if len(c) > 12 else 0,
                    "sales": 0, "pool": num(c[8]) if len(c) > 8 else 0})
    out.sort(key=lambda x: x["p"])
    return out[-count:]


def bootstrap_history():
    hist, page, total = [], 1, None
    while True:
        rows, tot = fetch_page(page, 100)
        total = total or tot
        hist += rows
        print(f"  [bootstrap] 第 {page} 页 +{len(rows)} 累计 {len(hist)}/{total}")
        if not rows or len(hist) >= total:
            break
        page += 1
        time.sleep(0.35)
    hist.sort(key=lambda x: x["p"])
    return hist


# ---------------- 规则 ----------------
def tier_of(f_hit, b_hit):
    if f_hit == 5:
        return {2: 1, 1: 2, 0: 3}[b_hit]
    if f_hit == 4:
        return {2: 4, 1: 5, 0: 7}[b_hit]
    if f_hit == 3:
        return {2: 6, 1: 8, 0: 9}[b_hit]
    if f_hit == 2:
        return 8 if b_hit == 2 else (9 if b_hit == 1 else 0)
    if f_hit == 1:
        return 9 if b_hit == 2 else 0
    return 9 if b_hit == 2 else 0


def amount_of(t, draw):
    if t in FIXED:
        return FIXED[t]
    if t == 1:
        return draw.get("p1amt", 0)
    if t == 2:
        return draw.get("p2amt", 0)
    return 0


def expand(t):
    for fs in itertools.combinations(sorted(t["r"]), 5):
        for bs in itertools.combinations(sorted(t["b"]), 2):
            yield list(fs), list(bs)


def notes_of(t):
    return math.comb(len(t["r"]), 5) * math.comb(len(t["b"]), 2)


def review(tickets, draw):
    fw, bw = set(draw["f"]), set(draw["b"])
    per, tc, tp = [], 0, 0
    for t in tickets:
        hits = []
        for fs, bs in expand(t):
            fh = len(set(fs) & fw)
            bh = len(set(bs) & bw)
            tier = tier_of(fh, bh)
            hits.append({"f": fs, "b": bs, "fh": fh, "bh": bh,
                         "tier": tier, "prize": amount_of(tier, draw)})
        prize = sum(h["prize"] for h in hits)
        cost = len(hits) * 2
        best = min((h["tier"] for h in hits if h["tier"]), default=0)
        per.append({"n": t["n"], "type": t.get("type", ""),
                    "r": sorted(t["r"]), "b": sorted(t["b"]), "notes": len(hits),
                    "cost": cost, "prize": prize, "best": TIER[best],
                    "win_notes": sum(1 for h in hits if h["tier"]),
                    "max_f": max(h["fh"] for h in hits),
                    "max_b": max(h["bh"] for h in hits),
                    "hits": hits})
        tc += cost
        tp += prize
    order = list(TIER.values())
    best_all = min((p["best"] for p in per if p["best"] != "未中奖"),
                   key=lambda x: order.index(x), default="未中奖")
    return {"tickets": per, "cost": tc, "prize": tp, "pl": tp - tc,
            "best": best_all, "win_notes": sum(p["win_notes"] for p in per),
            "notes": sum(p["notes"] for p in per),
            "max_f": max(p["max_f"] for p in per)}


# ---------------- 选号 ----------------
def build_pools(history):
    n = len(history)
    f30, f100, fall = {}, {}, {}
    for h in history[-30:]:
        for x in h["f"]:
            f30[x] = f30.get(x, 0) + 1
    for h in history[-100:]:
        for x in h["f"]:
            f100[x] = f100.get(x, 0) + 1
    for h in history:
        for x in h["f"]:
            fall[x] = fall.get(x, 0) + 1
    om = {}
    for ball in range(1, 36):
        gap = n
        for i in range(n - 1, -1, -1):
            if ball in history[i]["f"]:
                gap = n - 1 - i
                break
        om[ball] = gap
    bom = {}
    for ball in range(1, 13):
        gap = n
        for i in range(n - 1, -1, -1):
            if ball in history[i]["b"]:
                gap = n - 1 - i
                break
        bom[ball] = gap
    bfall = {}
    for h in history:
        for x in h["b"]:
            bfall[x] = bfall.get(x, 0) + 1

    score = {}
    for x in range(1, 36):
        hot = (f100.get(x, 0) - 100 * 5 / 35) / (100 * 5 / 35)
        score[x] = hot * 100 + (om[x] - 40) * 0.5

    last = history[-1]["f"]
    zone = (sorted(range(1, 13), key=lambda x: -fall.get(x, 0))[:4]
            + sorted(range(13, 25), key=lambda x: -fall.get(x, 0))[:4]
            + sorted(range(25, 36), key=lambda x: -fall.get(x, 0))[:4])
    return {"f30": f30, "f100": f100, "fall": fall, "om": om,
            "bom": bom, "bfall": bfall, "score": score, "last": last,
            "pools": [
                ("热号延续", sorted(range(1, 36), key=lambda x: -(f100.get(x, 0) * 2 + f30.get(x, 0) * 3))[:14]),
                ("冷号回补", sorted(range(1, 36), key=lambda x: -om[x])[:14]),
                ("综合评分", sorted(range(1, 36), key=lambda x: -score[x])[:14]),
                ("上期邻号", sorted(set(last + [x + 1 for x in last if x < 35] + [x - 1 for x in last if x > 1]))),
                ("区间均衡", sorted(zone)),
            ]}


def struct_ok(nums, n_front):
    s = sum(nums)
    lo, hi = (68, 114) if n_front == 5 else (72, 130)
    if not (lo <= s <= hi):
        return False
    o = sum(x % 2 for x in nums)
    if n_front == 5:
        if o not in (2, 3):
            return False
        zmax, cons = 3, 1
    else:
        if o not in (2, 3, 4, 5):
            return False
        zmax, cons = 4, 2
    z = [sum(1 for x in nums if x <= 12), sum(1 for x in nums if 13 <= x <= 24),
         sum(1 for x in nums if x >= 25)]
    if max(z) > zmax or min(z) == 0:
        return False
    return sum(1 for i in range(len(nums) - 1) if nums[i + 1] - nums[i] == 1) <= cons


FORM_LIBRARY = {
    "A": {"desc": "1 张 6+3（前区复式 1 个 + 后区 3 个）", "budget": 36,
          "forms": [("6+3", "全复式", 6, 3, "综合评分")]},
    "B": {"desc": "2 张 6+2（放量前区，后区各 2 个）", "budget": 24,
          "forms": [("6+2", "主攻一", 6, 2, "热号延续"),
                    ("6+2", "主攻二", 6, 2, "冷号回补")]},
    "C": {"desc": "2 张 5+3（前区单式，后区覆盖 6 个）", "budget": 12,
          "forms": [("5+3", "轻量一", 5, 3, "综合评分"),
                    ("5+3", "轻量二", 5, 3, "区间均衡")]},
}


def choose_form(ledger, window=6):
    recent = ledger[-window:]
    if not recent:
        return "A", "首期无历史，采用默认组合：前区复式 1 个 + 后区 3 个"
    if len(recent) < 4:
        return "A", (f"目前仅 {len(recent)} 期复盘，样本不足以下判断，先沿用标准组合（A）")
    notes = sum(e.get("notes", 0) for e in recent) or 1
    mb = sum(e.get("max_b", 0) for e in recent) / len(recent)
    mf = sum(e.get("max_f", 0) for e in recent) / len(recent)
    if mb < 1.0:
        return "C", f"近 {len(recent)} 期后区命中偏低（平均 {mb:.1f} 个），本期加厚后区：覆盖 6 个后区号"
    if mf >= 4.0:
        return "A", f"近 {len(recent)} 期前区平均最多中 {mf:.1f} 个，前区接近度好，本期上复式"
    return "B", f"近 {len(recent)} 期前区 {mf:.1f}、后区 {mb:.1f}，前区略强，采用双票 6+2"


def make_sets(history, seed, forms):
    pools = build_pools(history)
    rng = random.Random(seed)
    bf = sorted(range(1, 13), key=lambda b: -pools["bfall"].get(b, 0))
    bo = sorted(range(1, 13), key=lambda b: -pools["bom"][b])
    border = []
    for i in range(8):
        for b in (bf[i % 12], bo[i % 12]):
            if b not in border:
                border.append(b)
    pool_map = dict(pools["pools"])
    sets, slot = [], 0
    for typ, tag, nf, nb, strategy in forms:
        pool = sorted(set(pool_map.get(strategy, [])))
        if len(pool) < nf:
            continue
        pick = None
        for _ in range(60000):
            cand = sorted(rng.sample(pool, nf))
            if not struct_ok(cand, nf):
                continue
            if any(len(set(cand) & set(s["r"])) > 3 for s in sets):
                continue
            pick = cand
            break
        if not pick:
            continue
        blues = sorted(border[slot:slot + nb])
        slot += nb
        sets.append({"n": f"{typ} {tag}", "type": typ, "r": pick,
                     "b": blues, "strategy": strategy})
    return sets


def next_period(draw):
    d = date.fromisoformat(draw["d"])
    nd = d
    for k in range(1, 8):
        nd = d + timedelta(days=k)
        if nd.weekday() in DRAW_WEEKDAYS:
            break
    code = draw["p"]
    yy = code[:2]
    if nd.year != d.year:
        return f"{str(nd.year)[2:]}001", nd.isoformat()
    return f"{yy}{int(code[2:]) + 1:03d}", nd.isoformat()


# ---------------- 推送 ----------------
def push(title, text):
    key = os.environ.get("WECOM_WEBHOOK", "")
    sck = os.environ.get("SERVERCHAN_KEY", "")
    cfg_f = ROOT / "data" / "push.json"
    if (not key or not sck) and cfg_f.exists():
        try:
            cfg = json.loads(cfg_f.read_text(encoding="utf-8"))
            key = key or cfg.get("wecom_webhook", "")
            sck = sck or cfg.get("serverchan_key", "")
        except Exception:
            pass
    sent = []
    if sck:
        data = urllib.parse.urlencode({"title": title, "desp": text, "short": title}).encode()
        req = urllib.request.Request(f"https://sctapi.ftqq.com/{sck}.send", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as r:
            sent.append("serverchan:" + r.read().decode()[:50])
    if key:
        payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{text}"}}
        req = urllib.request.Request(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            sent.append("wecom:" + r.read().decode()[:50])
    return sent


# ---------------- 主流程 ----------------
def main():
    st = load_state()
    if len(st.get("history") or []) < 500:
        print("[bootstrap] 历史不足，全量拉取…")
        st["history"] = bootstrap_history()
    print(f"[state] 历史 {len(st['history'])} 期 / 已下注 {len(st['tickets'])} 期 / 台账 {len(st['ledger'])} 条")

    try:
        recent = fetch_recent(6)
    except Exception as e:
        print(f"[warn] 体彩官网失败：{e}", file=sys.stderr)
        try:
            recent = fetch_recent_500(6)
            print("[warn] 已回退 500.com")
        except Exception as e2:
            print(f"[warn] 500.com 也失败：{e2}", file=sys.stderr)
            recent = []

    have = {h["p"] for h in st["history"]}
    added = [x for x in recent if x["p"] not in have]
    st["history"].extend(added)
    st["history"].sort(key=lambda x: x["p"])
    print(f"[抓取] 新增 {len(added)} 期，最新 {st['history'][-1]['p']}")

    latest = st["history"][-1]
    detail, reviewed_now = None, False
    if latest["p"] in st["tickets"]:
        if any(x["p"] == latest["p"] for x in st["ledger"]):
            detail = [x for x in st["ledger"] if x["p"] == latest["p"]][0]
            print(f"[复盘] {latest['p']} 已复盘")
        else:
            rv = review(st["tickets"][latest["p"]], latest)
            detail = {"p": latest["p"], "d": latest["d"], "r": latest["f"], "b": latest["b"],
                      "cost": rv["cost"], "prize": rv["prize"], "pl": rv["pl"],
                      "best": rv["best"], "notes": rv["notes"], "win_notes": rv["win_notes"],
                      "max_f": rv["max_f"],
                      "tickets": [{k: v for k, v in t.items() if k != "hits"} for t in rv["tickets"]],
                      "hits": [h for t in rv["tickets"] for h in t["hits"] if h["tier"]],
                      "ts": now_cn()}
            st["ledger"].append(detail)
            reviewed_now = True
            print(f"[复盘] {latest['p']} {rv['best']} 中 {rv['win_notes']} 注 盈亏 {rv['pl']:+}")
    else:
        print(f"[复盘] {latest['p']} 无投注记录")

    nxt, nd = next_period(latest)
    fk, why = choose_form(st["ledger"])
    form = FORM_LIBRARY[fk]
    if nxt in st["tickets"]:
        sets = st["tickets"][nxt]
        sk = st.get("form_pick", {}).get(nxt)
        if sk in FORM_LIBRARY:
            fk, form = sk, FORM_LIBRARY[sk]
            why = st.get("form_why", {}).get(nxt, why)
        else:
            st.setdefault("form_pick", {})[nxt] = fk
            st.setdefault("form_why", {})[nxt] = why
        print(f"[选号] {nxt} 沿用（票型 {fk}）")
    else:
        sets = make_sets(st["history"], int(nxt[2:]) + int(nxt[:2]) * 1000, form["forms"])
        st["tickets"][nxt] = sets
        st.setdefault("form_pick", {})[nxt] = fk
        st.setdefault("form_why", {})[nxt] = why
        print(f"[选号] 生成 {nxt}（{nd}）{len(sets)} 张 · 票型 {fk}")

    cost_all = sum(x["cost"] for x in st["ledger"])
    prize_all = sum(x["prize"] for x in st["ledger"])
    strat = {}
    for e in st["ledger"]:
        for t in e.get("tickets", []):
            a = strat.setdefault(t["n"], {"n": 0, "hit": 0, "prize": 0, "notes": 0})
            a["n"] += 1
            a["notes"] += t["notes"]
            a["hit"] += t["max_f"]
            a["prize"] += t["prize"]

    payload = {
        "updated": now_cn(), "game": "大乐透",
        "next": {"p": nxt, "d": nd, "sets": sets,
                 "form": fk, "form_desc": form["desc"], "form_why": why},
        "latest": {"p": latest["p"], "d": latest["d"], "r": latest["f"], "b": latest["b"],
                   "p1": latest.get("p1", 0), "p1amt": latest.get("p1amt", 0),
                   "sales": latest.get("sales", 0), "pool": latest.get("pool", 0)},
        "last_review": st["ledger"][-1] if st["ledger"] else None,
        "stats": {"periods": len(st["ledger"]), "cost": cost_all, "prize": prize_all,
                  "pl": prize_all - cost_all,
                  "rate": round(prize_all / cost_all * 100, 1) if cost_all else 0,
                  "strategy": [{"name": k, "n": v["n"], "notes": v["notes"],
                                "avg_hit": round(v["hit"] / v["n"], 2),
                                "prize": v["prize"]} for k, v in
                               sorted(strat.items(), key=lambda kv: -kv[1]["hit"] / kv[1]["n"])]},
        "ledger": list(reversed(st["ledger"]))[:60],
        "history": [{"p": h["p"], "d": h["d"], "r": h["f"], "b": h["b"],
                     "p1": h.get("p1", 0), "p1amt": h.get("p1amt", 0),
                     "sales": h.get("sales", 0), "pool": h.get("pool", 0)}
                    for h in st["history"]],
    }
    STATE.write_text(json.dumps(st, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[输出] {OUT}")

    # 推送
    total_notes = sum(notes_of(t) for t in sets)
    md = [f"### 大乐透 {nxt} 期", ""]
    md.append(f"{len(sets)} 张票 · {total_notes} 注 · {total_notes * 2} 元")
    md.append(f"票型 {fk}：{form['desc']}")
    md.append("")
    for t in sets:
        md.append(f"- **{t['n']}**（{notes_of(t)} 注）　**"
                  + " ".join(f"{x:02d}" for x in t["r"]) + "**　＋　**"
                  + " ".join(f"{x:02d}" for x in t["b"]) + "**")
    if detail:
        md += ["", "---", "", f"### {detail['p']} 期复盘", ""]
        md.append("开奖　**" + " ".join(f"{x:02d}" for x in detail["r"]) + "**　＋　**"
                  + " ".join(f"{x:02d}" for x in detail["b"]) + "**")
        md.append("")
        for t in detail.get("tickets", []):
            mark = f"　中奖 {t['win_notes']} 注（{t['best']}）" if t["win_notes"] else "　未中奖"
            md.append(f"- {t['n']}　{t['notes']} 注　最多中 {t['max_f']}+{t['max_b']}{mark}")
        md.append("")
        md.append(f"投入 **{detail['cost']}** 元　回收 **{detail['prize']}** 元　"
                  f"盈亏 **{detail['pl']:+}** 元")
    md += ["", "---", "", "*大乐透单注一等奖 1/21,425,712；复盘不改变中奖概率。*"]

    force = "--force-push" in sys.argv
    try:
        if not (added or reviewed_now or force):
            print("[推送] 无新开奖也无新复盘，跳过")
        else:
            title = f"大乐透 {nxt} 期" + (f" · {detail['p']} {detail['best']}" if detail else "")
            print("[推送]", push("【大乐透】" + title, "\n".join(md)) or "未配置")
    except Exception as e:
        print("[推送] 失败", e, file=sys.stderr)


if __name__ == "__main__":
    main()
