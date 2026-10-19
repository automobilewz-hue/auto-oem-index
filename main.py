import os
import json
from pathlib import Path
from datetime import datetime

import requests
import pandas as pd
import akshare as ak

from config import A_CODES, H_CODES, DUAL, BASE_LEVEL, DEFAULT_FX

DATA = Path("data")
DATA.mkdir(exist_ok=True)
PREV = DATA / "prev_prices.json"
HIST = DATA / "index_history.csv"


def fx_rate():
    v = os.environ.get("FIXED_FX")
    if v:
        return float(v)
    try:
        df = ak.fx_spot_quote()
        for _, row in df.iterrows():
            s = " ".join(str(x) for x in row.values)
            if "港" in s or "HKD" in s.upper():
                for x in row.values:
                    try:
                        f = float(str(x).replace(",", ""))
                        if 0.5 < f < 2:
                            return f
                    except Exception:
                        pass
    except Exception as e:
        print("fx error", e)
    return DEFAULT_FX


def load_prev():
    if PREV.exists():
        return json.loads(PREV.read_text())
    return {}


def save_prev(d):
    PREV.write_text(json.dumps(d, ensure_ascii=False, indent=2))


def fnum(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def fetch_a():
    out = {}
    df = ak.stock_zh_a_spot_em()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    for code in A_CODES:
        r = df[df["代码"] == code]
        if r.empty:
            continue
        row = r.iloc[0]
        price = fnum(row.get("最新价"))
        mkt = fnum(row.get("流通市值")) or fnum(row.get("总市值"))
        if not price or not mkt:
            continue
        if mkt < 1e8:
            mkt *= 1e8
        out[code] = {"price": price, "mkt_cny": mkt, "name": str(row.get("名称", code))}
    return out


def fetch_h(fx):
    out = {}
    df = ak.stock_hk_spot_em()
    df["_c"] = df["代码"].astype(str).str.replace(".HK", "").str.zfill(5)
    mkt_col = "市值" if "市值" in df.columns else ("总市值" if "总市值" in df.columns else None)
    for code in H_CODES:
        c = code.zfill(5)
        r = df[df["_c"] == c]
        if r.empty:
            continue
        row = r.iloc[0]
        price = fnum(row.get("最新价"))
        mkt_hkd = fnum(row[mkt_col]) if mkt_col else None
        if not price or not mkt_hkd:
            continue
        if mkt_hkd < 1e6:
            mkt_hkd *= 1e8
        out[c] = {"price": price, "mkt_cny": mkt_hkd * fx, "name": str(row.get("名称", c))}
    return out


def ret(key, price, prev):
    if key not in prev or prev[key] <= 0:
        return None
    return price / prev[key] - 1.0


def build_ah(a, h, prev):
    used_a, used_h = set(), set()
    rows = []
    for firm, (ca, ch) in DUAL.items():
        ch = ch.zfill(5)
        sa, sh = a.get(ca), h.get(ch)
        if not sa and not sh:
            continue
        va = sa["mkt_cny"] if sa else 0.0
        vh = sh["mkt_cny"] if sh else 0.0
        v = va + vh
        if v <= 0:
            continue
        ra = ret(f"A:{ca}", sa["price"], prev) if sa else None
        rh = ret(f"H:{ch}", sh["price"], prev) if sh else None
        if ra is not None and rh is not None:
            r = (va * ra + vh * rh) / v
        else:
            r = ra if ra is not None else rh
        rows.append((firm, v, r))
        if sa:
            used_a.add(ca)
        if sh:
            used_h.add(ch)
    for code, s in a.items():
        if code in used_a:
            continue
        rows.append((f"A:{code}", s["mkt_cny"], ret(f"A:{code}", s["price"], prev)))
    for code, s in h.items():
        if code in used_h:
            continue
        rows.append((f"H:{code}", s["mkt_cny"], ret(f"H:{code}", s["price"], prev)))
    return rows


def index_return(rows):
    total = sum(m for _, m, _ in rows)
    if total <= 0:
        return None
    s, ok = 0.0, False
    for _, m, r in rows:
        if r is None:
            continue
        s += (m / total) * r
        ok = True
    return s if ok else None


def simple_side(snap, prev, prefix):
    rows = []
    for code, s in snap.items():
        rows.append((code, s["mkt_cny"], ret(f"{prefix}:{code}", s["price"], prev)))
    return index_return(rows)


def update_hist(ra, rh, rah, fx):
    if HIST.exists():
        hist = pd.read_csv(HIST)
        last = hist.iloc[-1]
        la, lh, lah = float(last["index_a"]), float(last["index_h"]), float(last["index_ah"])
    else:
        hist = pd.DataFrame()
        la = lh = lah = BASE_LEVEL

    def step(level, r):
        return level * (1 + (r if r is not None else 0.0))

    la, lh, lah = step(la, ra), step(lh, rh), step(lah, rah)
    row = {
        "datetime": datetime.utcnow().isoformat() + "Z",
        "index_a": la,
        "index_h": lh,
        "index_ah": lah,
        "ret_a": ra if ra is not None else 0.0,
        "ret_h": rh if rh is not None else 0.0,
        "ret_ah": rah if rah is not None else 0.0,
        "fx": fx,
    }
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    hist.to_csv(HIST, index=False)
    return la, lh, lah


def notify(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text},
                timeout=30,
            )
            print("telegram ok")
        except Exception as e:
            print("telegram fail", e)
    else:
        print("telegram not configured")

    wh = os.environ.get("WECOM_WEBHOOK_URL")
    if wh:
        try:
            requests.post(wh, json={"msgtype": "text", "text": {"content": text}}, timeout=30)
            print("wecom ok")
        except Exception as e:
            print("wecom fail", e)
    else:
        print("wecom not configured")


def main():
    fx = fx_rate()
    print("FX", fx)
    a = fetch_a()
    h = fetch_h(fx)
    print("A", len(a), "H", len(h))
    if len(a) + len(h) < 3:
        notify(f"取数失败 A={len(a)} H={len(h)}")
        return

    prev = load_prev()
    ra = simple_side(a, prev, "A")
    rh = simple_side(h, prev, "H")
    rah = index_return(build_ah(a, h, prev))

    la, lh, lah = update_hist(ra, rh, rah, fx)

    prices = {f"A:{k}": v["price"] for k, v in a.items()}
    prices.update({f"H:{k}": v["price"] for k, v in h.items()})
    save_prev(prices)

    def pct(x):
        return f"{x*100:+.2f}%" if x is not None else "首日"

    msg = (
        f"【整车指数CNY 无上限 双上市合并】\n"
        f"FX={fx:.4f}\n"
        f"A : {la:.2f} ({pct(ra)}) n={len(a)}\n"
        f"H : {lh:.2f} ({pct(rh)}) n={len(h)}\n"
        f"AH: {lah:.2f} ({pct(rah)})\n"
    )
    print(msg)
    notify(msg)


if __name__ == "__main__":
    main()
