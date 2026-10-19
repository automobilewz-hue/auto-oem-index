import os
import json
import time
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

# 兜底用近似流通/市值（亿元）。只影响权重，价格仍用实时行情。
APPROX_A_CIRC_YI = {
    "002594": 3100,
    "601633": 860,
    "000625": 600,
    "600104": 1250,
    "601238": 500,
    "601127": 810,
    "600733": 280,
    "600418": 500,
}

APPROX_H_MKT_YI = {
    "01211": 3500,
    "00175": 1600,
    "09973": 600,
    "02333": 350,
    "02015": 1400,
    "09866": 800,
    "09868": 1400,
    "09863": 500,
    "09927": 700,
}


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


def _sina_a_list():
    out = []
    for c in A_CODES:
        c = c.zfill(6)
        prefix = "sh" if c.startswith("6") else "sz"
        out.append(prefix + c)
    return out


def fetch_a():
    """东财优先（重试3次），失败则新浪价格 + 近似市值。"""
    out = {}

    for i in range(3):
        try:
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
                out[code] = {
                    "price": price,
                    "mkt_cny": mkt,
                    "name": str(row.get("名称", code)),
                }
            if out:
                print("fetch_a: eastmoney ok", len(out))
                return out
        except Exception as e:
            print(f"fetch_a eastmoney try {i + 1} fail:", e)
            time.sleep(2)

    try:
        url = "https://hq.sinajs.cn/list=" + ",".join(_sina_a_list())
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        }
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "gbk"
        for line in resp.text.splitlines():
            if "hq_str_" not in line or '=""' in line or '=""' in line:
                continue
            try:
                left, right = line.split("=", 1)
                raw = left.split("_")[-1]
                code = raw.replace("sh", "").replace("sz", "").zfill(6)
                parts = right.strip().strip(";").strip('"').split(",")
                if len(parts) < 4:
                    continue
                price = fnum(parts[3])
                if not price or price <= 0:
                    continue
                yi = APPROX_A_CIRC_YI.get(code, 500)
                out[code] = {
                    "price": price,
                    "mkt_cny": yi * 1e8,
                    "name": parts[0] or code,
                }
            except Exception:
                continue
        if out:
            print("fetch_a: sina ok", len(out))
            return out
    except Exception as e:
        print("fetch_a sina fail:", e)

    print("fetch_a: all failed")
    return out


def fetch_h(fx):
    """东财优先（重试3次），失败则腾讯价格 + 近似市值。"""
    out = {}

    for i in range(3):
        try:
            df = ak.stock_hk_spot_em()
            df["_c"] = (
                df["代码"].astype(str).str.replace(".HK", "", regex=False).str.zfill(5)
            )
            mkt_col = None
            for c in ["市值", "总市值", "流通市值"]:
                if c in df.columns:
                    mkt_col = c
                    break
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
                out[c] = {
                    "price": price,
                    "mkt_cny": mkt_hkd * fx,
                    "name": str(row.get("名称", c)),
                }
            if out:
                print("fetch_h: eastmoney ok", len(out))
                return out
        except Exception as e:
            print(f"fetch_h eastmoney try {i + 1} fail:", e)
            time.sleep(2)

    try:
        want = {c.zfill(5) for c in H_CODES}
        symbols = ",".join("r" + c.zfill(5) for c in H_CODES)
        url = f"https://qt.gtimg.cn/q={symbols}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.encoding = "gbk"
        for seg in resp.text.split(";"):
            if "~" not in seg:
                continue
            parts = seg.split("~")
            if len(parts) < 6:
                continue
            try:
                # 腾讯港股: 名称~代码~现价 等，位置可能微调
                code = None
                price = None
                name = parts[1] if len(parts) > 1 else ""
                for p in parts[1:6]:
                    p2 = str(p).zfill(5)
                    if p2 in want:
                        code = p2
                        break
                # 现价常见在 parts[3]
                price = fnum(parts[3])
                if code is None or not price or price <= 0:
                    continue
                yi = APPROX_H_MKT_YI.get(code, 500)
                out[code] = {
                    "price": price,
                    "mkt_cny": yi * 1e8 * fx,
                    "name": name or code,
                }
            except Exception:
                continue
        if out:
            print("fetch_h: tencent ok", len(out))
            return out
    except Exception as e:
        print("fetch_h tencent fail:", e)

    print("fetch_h: all failed")
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
        la = float(last["index_a"])
        lh = float(last["index_h"])
        lah = float(last["index_ah"])
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
            requests.post(
                wh, json={"msgtype": "text", "text": {"content": text}}, timeout=30
            )
            print("wecom ok")
        except Exception as e:
            print("wecom fail", e)
    else:
        print("wecom not configured")


def main():
    try:
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
            return f"{x * 100:+.2f}%" if x is not None else "首日"

        msg = (
            f"【整车指数CNY 无上限 双上市合并】\n"
            f"FX={fx:.4f}\n"
            f"A : {la:.2f} ({pct(ra)}) n={len(a)}\n"
            f"H : {lh:.2f} ({pct(rh)}) n={len(h)}\n"
            f"AH: {lah:.2f} ({pct(rah)})\n"
        )
        print(msg)
        notify(msg)
    except Exception as e:
        import traceback

        print(traceback.format_exc())
        notify(f"脚本异常: {e}")
        raise


if __name__ == "__main__":
    main()
