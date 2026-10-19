"""
从 BASE_DATE 回填 A / H / AH 指数到 data/index_history.csv
基点 1000，市值近似权重，无上限，双上市合并。
在 Actions 里手动跑一次即可。
"""
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import akshare as ak

from config import A_CODES, H_CODES, DUAL, BASE_LEVEL, DEFAULT_FX

BASE_DATE = "20260101"  # 2026-01-01
DATA = Path("data")
DATA.mkdir(exist_ok=True)
HIST = DATA / "index_history.csv"
PREV = DATA / "prev_prices.json"

# 近似权重用市值（亿元），无上限；与 main 兜底一致，可自行改
W_A = {
    "002594": 3100,
    "601633": 860,
    "000625": 600,
    "600104": 1250,
    "601238": 500,
    "601127": 810,
    "600733": 280,
    "600418": 500,
}
W_H = {
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


def fx():
    v = os.environ.get("FIXED_FX")
    return float(v) if v else DEFAULT_FX


def get_a_hist(code: str) -> pd.Series:
    """A股日收盘，index=日期 datetime64，value=收盘价"""
    for i in range(3):
        try:
            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=BASE_DATE,
                end_date=datetime.now().strftime("%Y%m%d"),
                adjust="qfq",
            )
            if df is None or df.empty:
                return pd.Series(dtype=float)
            date_col = "日期" if "日期" in df.columns else df.columns[0]
            close_col = "收盘" if "收盘" in df.columns else df.columns[4]
            s = df.set_index(pd.to_datetime(df[date_col]))[close_col].astype(float)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            return s
        except Exception as e:
            print(f"A {code} try {i+1}", e)
            time.sleep(1)
    return pd.Series(dtype=float)


def get_h_hist(code: str) -> pd.Series:
    """H股日收盘。code 5位。"""
    c = code.zfill(5)
    for i in range(3):
        try:
            # 不同版本 akshare 港股历史接口名可能不同
            df = None
            for fn_name in ["stock_hk_hist", "stock_hk_daily"]:
                fn = getattr(ak, fn_name, None)
                if fn is None:
                    continue
                try:
                    if fn_name == "stock_hk_hist":
                        df = fn(
                            symbol=c,
                            period="daily",
                            start_date=BASE_DATE,
                            end_date=datetime.now().strftime("%Y%m%d"),
                            adjust="qfq",
                        )
                    else:
                        df = fn(symbol=c, adjust="qfq")
                    break
                except Exception:
                    continue
            if df is None or df.empty:
                time.sleep(1)
                continue
            # 统一列
            cols = {str(x).lower(): x for x in df.columns}
            date_col = None
            for k in ["日期", "date"]:
                if k in df.columns:
                    date_col = k
                    break
            if date_col is None:
                date_col = df.columns[0]
            close_col = "收盘" if "收盘" in df.columns else (
                "close" if "close" in df.columns else df.columns[4]
            )
            s = df.set_index(pd.to_datetime(df[date_col]))[close_col].astype(float)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            # 若接口没有 start 过滤
            s = s[s.index >= pd.Timestamp("2026-01-01")]
            return s
        except Exception as e:
            print(f"H {c} try {i+1}", e)
            time.sleep(1)
    return pd.Series(dtype=float)


def weighted_ret(price_df: pd.DataFrame, weights: dict) -> pd.Series:
    """price_df: columns=code, rows=date；weights: code->市值权重原料"""
    cols = [c for c in price_df.columns if c in weights]
    if not cols:
        return pd.Series(dtype=float)
    w = pd.Series({c: weights[c] for c in cols}, dtype=float)
    w = w / w.sum()
    rets = price_df[cols].pct_change()
    # 某日缺某票则在当日剩余票中重归一（简单处理）
    out = []
    for dt, row in rets.iterrows():
        valid = row.dropna()
        if valid.empty:
            out.append(np.nan)
            continue
        ww = w.reindex(valid.index).fillna(0)
        if ww.sum() <= 0:
            out.append(np.nan)
            continue
        ww = ww / ww.sum()
        out.append(float((valid * ww).sum()))
    return pd.Series(out, index=rets.index)


def build_ah_prices(a_px: pd.DataFrame, h_px: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    合并双上市为合成价格序列（用 A、H 收益市值加权后再还原成价）。
    更简单：对 AH 直接用「各 leg 收益加权」而不造合成价。
    这里返回用于 AH 的「实体收益序列」在 main 里算。
    """
    pass


def main():
    print("backfill from", BASE_DATE)
    rate = fx()
    print("FX", rate)

    # ----- A 历史价 -----
    a_map = {}
    for code in A_CODES:
        s = get_a_hist(code)
        print("A", code, "days", len(s))
        if len(s) > 0:
            a_map[code] = s
        time.sleep(0.3)
    a_px = pd.DataFrame(a_map).sort_index()

    # ----- H 历史价 -----
    h_map = {}
    for code in H_CODES:
        s = get_h_hist(code)
        print("H", code, "days", len(s))
        if len(s) > 0:
            h_map[code.zfill(5)] = s
        time.sleep(0.3)
    h_px = pd.DataFrame(h_map).sort_index()

    ret_a = weighted_ret(a_px, W_A)
    # H 权重：港币市值近似，汇率常数，相对权重不变
    ret_h = weighted_ret(h_px, W_H)

    # ----- AH：双上市合并权重，其余单边 -----
    # 构造每日 AH 收益
    all_dates = sorted(set(ret_a.dropna().index) | set(ret_h.dropna().index))
    ah_rets = []
    for dt in all_dates:
        pieces = []  # (weight_raw, ret)
        used_a, used_h = set(), set()
        for firm, (ca, ch) in DUAL.items():
            ch = ch.zfill(5)
            ra = ret_a.get(dt) if ca in (ret_a.index if False else []) else None
            # 个股当日收益
            ra_i = None
            rh_i = None
            if ca in a_px.columns and dt in a_px.index:
                # 用价格算
                pass
            va = W_A.get(ca, 0)
            vh = W_H.get(ch, 0)
            # 个股收益
            if ca in a_px.columns and dt in a_px.index:
                loc = a_px.index.get_loc(dt)
                if loc > 0:
                    p0, p1 = a_px[ca].iloc[loc - 1], a_px[ca].iloc[loc]
                    if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                        ra_i = p1 / p0 - 1
            if ch in h_px.columns and dt in h_px.index:
                loc = h_px.index.get_loc(dt)
                if loc > 0:
                    p0, p1 = h_px[ch].iloc[loc - 1], h_px[ch].iloc[loc]
                    if pd.notna(p0) and pd.notna(p1) and p0 > 0:
                        rh_i = p1 / p0 - 1
            if ra_i is None and rh_i is None:
                continue
            if ra_i is not None and rh_i is not None and (va + vh) > 0:
                r = (va * ra_i + vh * rh_i) / (va + vh)
                pieces.append((va + vh, r))
            elif ra_i is not None:
                pieces.append((va or 1.0, ra_i))
            else:
                pieces.append((vh or 1.0, rh_i))
            used_a.add(ca)
            used_h.add(ch)

        for ca in A_CODES:
            if ca in used_a or ca not in a_px.columns or dt not in a_px.index:
                continue
            loc = a_px.index.get_loc(dt)
            if loc <= 0:
                continue
            p0, p1 = a_px[ca].iloc[loc - 1], a_px[ca].iloc[loc]
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                continue
            pieces.append((W_A.get(ca, 1.0), p1 / p0 - 1))

        for ch in [c.zfill(5) for c in H_CODES]:
            if ch in used_h or ch not in h_px.columns or dt not in h_px.index:
                continue
            loc = h_px.index.get_loc(dt)
            if loc <= 0:
                continue
            p0, p1 = h_px[ch].iloc[loc - 1], h_px[ch].iloc[loc]
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                continue
            pieces.append((W_H.get(ch, 1.0), p1 / p0 - 1))

        if not pieces:
            ah_rets.append((dt, np.nan))
            continue
        tw = sum(w for w, _ in pieces)
        ah_rets.append((dt, sum(w / tw * r for w, r in pieces)))

    ret_ah = pd.Series({d: r for d, r in ah_rets}).sort_index()

    # 对齐交易日：用 A 指数有效日为主，H/AH 向前对齐
    def cum_index(r: pd.Series) -> pd.Series:
        r = r.dropna()
        if r.empty:
            return r
        # 第一天收益通常 NaN，从第二个交易日起滚
        level = BASE_LEVEL
        levels = []
        dates = []
        first = True
        for dt, x in r.items():
            if first:
                # 基日：点位 1000，收益记 0
                levels.append(BASE_LEVEL)
                dates.append(dt)
                first = False
                continue
            if pd.isna(x):
                levels.append(level)
            else:
                level = level * (1 + x)
                levels.append(level)
            dates.append(dt)
        return pd.Series(levels, index=pd.DatetimeIndex(dates))

    # 对 ret 序列：第一行设为基日
    def prepare(r: pd.Series) -> pd.Series:
        r = r.copy()
        if r.empty:
            return r
        # 第一个非 nan 之前丢掉
        r = r.dropna(how="all")
        return r

    idx_a = cum_index(prepare(ret_a))
    idx_h = cum_index(prepare(ret_h))
    idx_ah = cum_index(prepare(ret_ah))

    # 合并输出
    all_idx = sorted(set(idx_a.index) | set(idx_h.index) | set(idx_ah.index))
    rows = []
    for dt in all_idx:
        ra = ret_a.get(dt, np.nan)
        rh = ret_h.get(dt, np.nan)
        rah = ret_ah.get(dt, np.nan)
        rows.append(
            {
                "date": dt.strftime("%Y-%m-%d"),
                "datetime": dt.isoformat(),
                "index_a": float(idx_a.get(dt, np.nan)) if dt in idx_a.index else np.nan,
                "index_h": float(idx_h.get(dt, np.nan)) if dt in idx_h.index else np.nan,
                "index_ah": float(idx_ah.get(dt, np.nan)) if dt in idx_ah.index else np.nan,
                "ret_a": 0.0 if pd.isna(ra) else float(ra),
                "ret_h": 0.0 if pd.isna(rh) else float(rh),
                "ret_ah": 0.0 if pd.isna(rah) else float(rah),
                "fx": rate,
            }
        )

    hist = pd.DataFrame(rows)
    # 前向填充点位空缺
    for c in ["index_a", "index_h", "index_ah"]:
        hist[c] = hist[c].ffill()
    hist.to_csv(HIST, index=False)
    print("wrote", HIST, "rows", len(hist))
    if len(hist):
        print(hist.tail(3).to_string(index=False))

    # 用最后一天价格写 prev，供 main 续算
    prev = {}
    last_a = a_px.dropna(how="all").index.max() if len(a_px) else None
    last_h = h_px.dropna(how="all").index.max() if len(h_px) else None
    if last_a is not None:
        for c in a_px.columns:
            v = a_px.loc[last_a, c]
            if pd.notna(v):
                prev[f"A:{c}"] = float(v)
    if last_h is not None:
        for c in h_px.columns:
            v = h_px.loc[last_h, c]
            if pd.notna(v):
                prev[f"H:{c}"] = float(v)
    PREV.write_text(
        __import__("json").dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("prev prices", len(prev))


if __name__ == "__main__":
    main()
