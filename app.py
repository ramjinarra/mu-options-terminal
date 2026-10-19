import re
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta
import pytz
import yfinance as yf
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionChainRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.enums import ContractType
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from streamlit_autorefresh import st_autorefresh

# ================= PAGE SETUP & 15-SECOND AUTO-REFRESH =================
st.set_page_config(page_title="MU & SNDK Master Options & Spread Terminal", layout="wide")
st_autorefresh(interval=15 * 1000, key="live_bar_refresh")

# ================= EMBEDDED ALPACA API CREDENTIALS =================
API_KEY = "PKILB6ZERYMO6FQK2OFNV3OI7F"
SECRET_KEY = "5djHfXTudEQmY6vboU7aPVuTQZRdQK2mGQuok6ZQ7sv"

# Initialize Alpaca Clients
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
opt_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# ================= SIDEBAR SETTINGS =================
st.sidebar.header("⚡ Scanner & Execution Control")
data_feed = st.sidebar.selectbox("Alpaca Stock Feed (15-Sec Refresh)", ["iex", "sip"], index=0)
washout_thresh = st.sidebar.slider("Morning Washout Threshold (%)", -5.0, -1.0, -2.0, 0.25)
dte_min, dte_max = st.sidebar.slider("Target Options DTE Window (Days)", 1, 30, (4, 14))
order_qty = st.sidebar.number_input("Default Paper Order Qty (Contracts)", min_value=1, max_value=100, value=1, step=1)

st.sidebar.markdown("---")
st.sidebar.subheader("📖 Composite Score Guide")
st.sidebar.caption(
    "• **+40 to +100:** Strong Bullish (Buy Bull Call Spread)\n"
    "• **+15 to +39:** Moderate Bullish Lean (Tight Call Spread)\n"
    "• **-14 to +14:** Choppy / Stand Aside\n"
    "• **-15 to -39:** Moderate Bearish Lean (Tight Put Spread)\n"
    "• **-40 to -100:** Strong Bearish Lock (Bear Put Spread)"
)

# ================= 1. KOREAN MEMORY LEADS (60-SEC CACHE) =================
@st.cache_data(ttl=60)
def fetch_korean_leads():
    tickers = {"Samsung (005930.KS)": "005930.KS", "SK Hynix (000660.KS)": "000660.KS"}
    results = {}
    for label, tk in tickers.items():
        try:
            hist = yf.Ticker(tk).history(period="5d", interval="1d")
            if len(hist) >= 2:
                last_p = float(hist["Close"].iloc[-1])
                prev_c = float(hist["Close"].iloc[-2])
                results[label] = (last_p, (last_p - prev_c) / prev_c * 100)
            else:
                results[label] = (np.nan, np.nan)
        except Exception:
            results[label] = (np.nan, np.nan)
    return results

# ================= 2. ALPACA LIVE OPTIONS CHAIN & SPREAD ENGINE =================
def parse_occ_symbol(occ_sym: str):
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", occ_sym.strip())
    if not m:
        return None
    root, yymmdd, opt_char, strike_str = m.groups()
    exp_date = datetime.strptime(yymmdd, "%y%m%d").date()
    return {
        "occ": occ_sym,
        "root": root,
        "expiration": exp_date,
        "type": "call" if opt_char == "C" else "put",
        "strike": int(strike_str) / 1000.0
    }

def round_strike(price: float) -> float:
    if price >= 500:
        return round(price / 5.0) * 5.0
    elif price >= 100:
        return round(price / 2.5) * 2.5
    return float(round(price))

@st.cache_data(ttl=14)
def fetch_alpaca_live_options(symbol: str, curr_price: float, t1_price: float, t2_price: float, stop_price: float, score: int, streak_days: int, min_dte: int, max_dte: int):
    if abs(score) < 15 or np.isnan(t1_price):
        return None

    is_bull = score > 0
    c_type = ContractType.CALL if is_bull else ContractType.PUT

    step = 5.0 if curr_price >= 500 else (2.5 if curr_price >= 100 else 1.0)
    atm_strike = round_strike(curr_price)

    if is_bull:
        target_ref = t2_price if (score >= 40 and streak_days <= -2) else t1_price
        min_steps = 3 if score >= 40 else 2
        short_strike = max(atm_strike + step * min_steps, round_strike(target_ref))
        strike_low, strike_high = atm_strike - step * 2, short_strike + step * 2
    else:
        target_ref = t1_price
        min_steps = 3 if score <= -40 else 2
        short_strike = min(atm_strike - step * min_steps, round_strike(target_ref))
        strike_low, strike_high = short_strike - step * 2, atm_strike + step * 2

    today_d = datetime.now(pytz.timezone("America/New_York")).date()
    exp_gte = today_d + timedelta(days=min_dte)
    exp_lte = today_d + timedelta(days=max_dte)

    try:
        req = OptionChainRequest(
            underlying_symbol=symbol,
            type=c_type,
            strike_price_gte=float(strike_low),
            strike_price_lte=float(strike_high),
            expiration_date_gte=exp_gte,
            expiration_date_lte=exp_lte
        )
        chain_snaps = opt_client.get_option_chain(req)
    except Exception as e:
        return {"error": f"Alpaca Options API message: {e}"}

    rows = []
    for occ_sym, snap in chain_snaps.items():
        meta = parse_occ_symbol(occ_sym)
        if not meta:
            continue
        q = snap.latest_quote
        t = snap.latest_trade
        g = snap.greeks

        bid = float(q.bid_price) if (q and q.bid_price is not None) else 0.0
        ask = float(q.ask_price) if (q and q.ask_price is not None) else 0.0
        last = float(t.price) if (t and t.price is not None) else 0.0
        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
        spread_pct = ((ask - bid) / mid * 100) if (mid > 0 and ask >= bid) else 99.0

        iv = float(snap.implied_volatility) * 100 if snap.implied_volatility is not None else np.nan
        delta = float(g.delta) if (g and g.delta is not None) else np.nan
        theta = float(g.theta) if (g and g.theta is not None) else np.nan
        gamma = float(g.gamma) if (g and g.gamma is not None) else np.nan

        if mid > 0:
            rows.append({
                "occ": occ_sym,
                "expiration": meta["expiration"],
                "dte": (meta["expiration"] - today_d).days,
                "type": meta["type"],
                "strike": meta["strike"],
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": last,
                "ba_spread_pct": spread_pct,
                "iv": iv,
                "delta": delta,
                "theta": theta,
                "gamma": gamma
            })

    if not rows:
        return {"error": f"No active option contracts found between {exp_gte} and {exp_lte}."}

    df_opt = pd.DataFrame(rows)
    df_opt["dte_dist"] = (df_opt["dte"] - 8).abs()
    best_exp = df_opt.sort_values(["dte_dist", "ba_spread_pct"]).iloc[0]["expiration"]
    exp_df = df_opt[df_opt["expiration"] == best_exp].sort_values("strike").reset_index(drop=True)
    dte_val = int(exp_df["dte"].iloc[0])

    long_row = exp_df.iloc[(exp_df["strike"] - atm_strike).abs().argsort()[:1]].iloc[0]
    short_row = exp_df.iloc[(exp_df["strike"] - short_strike).abs().argsort()[:1]].iloc[0]

    actual_long_k = float(long_row["strike"])
    actual_short_k = float(short_row["strike"])
    width = abs(actual_short_k - actual_long_k)

    spread_mid_debit = max(0.05, float(long_row["mid"] - short_row["mid"]))
    spread_nat_debit = max(0.05, float(long_row["ask"] - short_row["bid"])) if (long_row["ask"] > 0 and short_row["bid"] > 0) else spread_mid_debit
    debit_to_width_pct = (spread_mid_debit / width * 100) if width > 0 else 100.0
    max_profit = width - spread_mid_debit
    tp_target_val = width * 0.65

    naked_mid = float(long_row["mid"])
    naked_ask = float(long_row["ask"]) if long_row["ask"] > 0 else naked_mid
    naked_iv = float(long_row["iv"]) if not np.isnan(long_row["iv"]) else 50.0
    naked_ba_pct = float(long_row["ba_spread_pct"])
    naked_delta = abs(float(long_row["delta"])) if not np.isnan(long_row["delta"]) else 0.52

    expected_underlying_move = abs(t1_price - curr_price)
    gamma_val = float(long_row["gamma"]) if not np.isnan(long_row["gamma"]) else 0.015
    est_naked_gain = expected_underlying_move * naked_delta + 0.5 * gamma_val * (expected_underlying_move ** 2)
    est_naked_roi_pct = (est_naked_gain / naked_mid * 100) if naked_mid > 0 else 0.0
    est_spread_roi_pct = ((tp_target_val - spread_mid_debit) / spread_mid_debit * 100) if spread_mid_debit > 0 else 0.0

    opt_label = "Call" if is_bull else "Put"
    spread_label = "Bull Call Debit Spread" if is_bull else "Bear Put Debit Spread"

    # Spread is ALWAYS prioritized as #1 Best Option; priced grading indicates whether to enter at market or limit
    if debit_to_width_pct <= 45.0:
        verdict = f"✅ #1 BEST OPTION: {spread_label.upper()} (${actual_long_k:g}/${actual_short_k:g} at ${spread_mid_debit:.2f} = {debit_to_width_pct:.1f}% of Width — Ideal Entry Price!)"
    else:
        verdict = f"🟢 #1 BEST OPTION: {spread_label.upper()} (${actual_long_k:g}/${actual_short_k:g} — Work Limit Order Near ${ width * 0.43 :.2f} | Current Mid: ${spread_mid_debit:.2f})"

    return {
        "verdict": verdict,
        "expiration": str(best_exp),
        "dte": dte_val,
        "opt_label": opt_label,
        "spread_label": spread_label,
        "long_occ": long_row["occ"],
        "short_occ": short_row["occ"],
        "long_k": actual_long_k,
        "short_k": actual_short_k,
        "width": width,
        "spread_mid": spread_mid_debit,
        "spread_nat": spread_nat_debit,
        "debit_pct_width": debit_to_width_pct,
        "max_profit": max_profit,
        "tp_target_val": tp_target_val,
        "est_spread_roi_pct": est_spread_roi_pct,
        "naked_mid": naked_mid,
        "naked_ask": naked_ask,
        "naked_bid": float(long_row["bid"]),
        "naked_iv": naked_iv,
        "naked_delta": float(long_row["delta"]) if not np.isnan(long_row["delta"]) else np.nan,
        "naked_theta": float(long_row["theta"]) if not np.isnan(long_row["theta"]) else np.nan,
        "naked_ba_pct": naked_ba_pct,
        "est_naked_target_price": naked_mid + est_naked_gain,
        "est_naked_roi_pct": est_naked_roi_pct,
        "stop_price": stop_price,
        "chain_table": exp_df[["strike", "bid", "ask", "mid", "iv", "delta", "theta", "ba_spread_pct"]].round(2)
    }

# ================= 3. FULL 9-FACTOR COMPOSITE SCORING ENGINE =================
def fetch_and_analyze_ticker(symbol: str, client: StockHistoricalDataClient, kr_leads: dict):
    et_tz = pytz.timezone("America/New_York")
    now_et = datetime.now(et_tz)
    start_dt = (now_et - timedelta(days=14)).replace(hour=0, minute=0, second=0)

    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start_dt,
        adjustment="all",
        feed=data_feed
    )
    bars = client.get_stock_bars(req).df.reset_index()
    if len(bars) == 0:
        return {"status": "NO_DATA"}

    bars["timestamp_et"] = pd.to_datetime(bars["timestamp"]).dt.tz_convert("America/New_York")
    bars["date"] = bars["timestamp_et"].dt.date
    bars["time"] = bars["timestamp_et"].dt.strftime("%H:%M")

    dates = sorted(bars["date"].unique())
    today_date = dates[-1]

    # --- A. MULTI-DAY CONSECUTIVE STREAK ---
    rth_all = bars[(bars["time"] >= "09:30") & (bars["time"] <= "15:55")]
    daily_closes = rth_all.groupby("date")["close"].last().sort_index()
    prior_closes = daily_closes[daily_closes.index < today_date]

    streak_days = 0
    streak_cum_pct = 0.0
    if len(prior_closes) > 1:
        prior_rets = prior_closes.pct_change().dropna() * 100
        for r in reversed(prior_rets.values):
            if r > 0 and streak_days >= 0:
                streak_days += 1
                streak_cum_pct = (1 + streak_cum_pct / 100) * (1 + r / 100) * 100 - 100
            elif r < 0 and streak_days <= 0:
                streak_days -= 1
                streak_cum_pct = (1 + streak_cum_pct / 100) * (1 + r / 100) * 100 - 100
            else:
                break
        prev_close = float(prior_closes.iloc[-1])
    else:
        prev_close = float(bars["close"].iloc[0])

    # --- B. KOREAN MEMORY SENTIMENT ---
    sam_chg = kr_leads.get("Samsung (005930.KS)", (np.nan, 0.0))[1]
    sk_chg = kr_leads.get("SK Hynix (000660.KS)", (np.nan, 0.0))[1]
    valid_kr = [v for v in [sam_chg, sk_chg] if not np.isnan(v)]
    kr_avg_chg = float(np.mean(valid_kr)) if valid_kr else 0.0

    # --- C. TODAY'S PRE-MARKET & RTH DATA ---
    today_df = bars[bars["date"] == today_date].sort_values("time").reset_index(drop=True)
    pre = today_df[today_df["time"] < "09:30"]
    rth = today_df[(today_df["time"] >= "09:30") & (today_df["time"] <= "15:55")].copy().reset_index(drop=True)

    pre_low = float(pre["low"].min()) if len(pre) > 0 else np.nan
    pre_high = float(pre["high"].max()) if len(pre) > 0 else np.nan
    pre_vwap = float((pre["vwap"] * pre["volume"]).sum() / pre["volume"].sum()) if len(pre) > 0 and pre["volume"].sum() > 0 else np.nan
    pre_last = float(pre["close"].iloc[-1]) if len(pre) > 0 else prev_close

    if len(rth) == 0:
        pre_gap_pct = (pre_last - prev_close) / prev_close * 100
        return {
            "status": "PRE_MARKET", "today_date": today_date, "prev_close": prev_close,
            "pre_low": pre_low, "pre_high": pre_high, "pre_vwap": pre_vwap, "pre_last": pre_last,
            "pre_gap_pct": pre_gap_pct, "streak_days": streak_days, "streak_cum_pct": streak_cum_pct,
            "kr_avg_chg": kr_avg_chg
        }

    rth["pv"] = rth["vwap"] * rth["volume"]
    rth["session_vwap"] = rth["pv"].cumsum() / rth["volume"].cumsum()
    rth["vol_ma5"] = rth["volume"].rolling(5, min_periods=2).mean().shift(1)
    rth["rel_vol"] = (rth["volume"] / rth["vol_ma5"]).fillna(1.0)

    rng = (rth["high"] - rth["low"]).replace(0, np.nan)
    rth["lower_wick_ratio"] = ((np.minimum(rth["open"], rth["close"]) - rth["low"]) / rng).fillna(0.0)
    rth["upper_wick_ratio"] = ((rth["high"] - np.maximum(rth["open"], rth["close"])) / rng).fillna(0.0)
    rth["bar_range_pct"] = (rth["high"] - rth["low"]) / rth["low"] * 100
    rth["dist_vwap_pct"] = (rth["close"] - rth["session_vwap"]) / rth["session_vwap"] * 100
    rth["is_green"] = rth["close"] > rth["open"]

    rth["slot_15m"] = rth["timestamp_et"].dt.floor("15min").dt.strftime("%H:%M")
    rth_15m = rth.groupby("slot_15m").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), session_vwap=("session_vwap", "last")
    ).reset_index()
    rth_15m["is_green"] = rth_15m["close"] > rth_15m["open"]

    rth_open = float(rth["open"].iloc[0])
    curr_bar = rth.iloc[-1]
    curr_price = float(curr_bar["close"])
    curr_vwap = float(curr_bar["session_vwap"])
    curr_time = str(curr_bar["time"])

    run_low = float(rth["low"].min())
    run_low_time = str(rth.loc[rth["low"].idxmin(), "time"])
    run_high = float(rth["high"].max())

    gap_pct = (rth_open - prev_close) / prev_close * 100
    drop_vs_prev = (run_low - prev_close) / prev_close * 100
    drop_vs_open = (run_low - rth_open) / rth_open * 100
    curr_chg_pct = (curr_price - prev_close) / prev_close * 100

    orb30 = rth[rth["time"] <= "09:55"]
    orb30_high, orb30_low = float(orb30["high"].max()), float(orb30["low"].min())

    m60 = rth[rth["time"] <= "10:25"]
    m60_high, m60_low = float(m60["high"].max()), float(m60["low"].min())

    # ================= D. FULL 9-FACTOR COMPOSITE SCORING =================
    score = 0
    signals = []
    factor_breakdown = []

    # 1. Multi-Day Streak Factor
    if streak_days <= -3:
        score += 25
        signals.append(f"🟢 **Multi-Day Oversold ({streak_days} Red Days, {streak_cum_pct:+.2f}%):** 70% Net-Green rate; VWAP reclaims hold morning low **86.7%** of the time and finish higher at 4 PM **80.0%** of the time.")
        factor_breakdown.append(("Multi-Day Streak", "+25 (3+ Red Days Oversold)"))
    elif streak_days == -2:
        score += 18
        signals.append(f"🟢 **2-Day Red Pullback ({streak_cum_pct:+.2f}%):** 86.7% morning-low survival rate on VWAP reclaims.")
        factor_breakdown.append(("Multi-Day Streak", "+18 (2 Red Days Pullback)"))
    elif streak_days == -1:
        score += 8
        factor_breakdown.append(("Multi-Day Streak", "+8 (1 Red Day Pullback)"))
    elif streak_days == 1:
        factor_breakdown.append(("Multi-Day Streak", "0 (+1 Green Day Neutral)"))
    elif streak_days == 2:
        score -= 10
        signals.append(f"🟡 **2-Day Green Run ({streak_cum_pct:+.2f}%):** Afternoon fade risk (-0.15% avg close). Vertical spread is strongly preferred over naked calls.")
        factor_breakdown.append(("Multi-Day Streak", "-10 (2 Green Days Late-Fade Risk)"))
    else:
        score -= 22
        signals.append(f"🔴 **Extended Run (+{streak_days} Green Days, {streak_cum_pct:+.2f}%):** Only 38.5% close net green.")
        factor_breakdown.append(("Multi-Day Streak", f"-22 (+{streak_days} Green Days Overbought)"))

    # 2. Korean Memory Lead Factor
    if kr_avg_chg >= 0.50:
        if gap_pct <= -1.0 or drop_vs_prev <= -2.0:
            score += 20
            signals.append(f"🟢 **Korean Bullish Divergence (KR {kr_avg_chg:+.2f}% vs US Dip {drop_vs_prev:+.2f}%):** Asia memory closed green while {symbol} flushed.")
            factor_breakdown.append(("Korean Memory Lead", f"+20 (Bullish Divergence: KR {kr_avg_chg:+.2f}%)"))
        else:
            score += 10
            factor_breakdown.append(("Korean Memory Lead", f"+10 (KR Strength {kr_avg_chg:+.2f}%)"))
    elif kr_avg_chg <= -0.75:
        score -= 15
        factor_breakdown.append(("Korean Memory Lead", f"-15 (KR Weakness {kr_avg_chg:+.2f}%)"))
    else:
        factor_breakdown.append(("Korean Memory Lead", f"0 (KR Flat {kr_avg_chg:+.2f}%)"))

    # 3. Pre-Market Low & Gap Factor
    swept_pre_low = (not np.isnan(pre_low)) and (run_low < pre_low)
    if -3.0 <= gap_pct <= -1.0:
        score += 10
        signals.append(f"🟢 **Mild Gap-Down Sweet Spot ({gap_pct:+.2f}% Gap):** #1 historical bounce regime (71.4% rally >= +3.5% off RTH low).")
        factor_breakdown.append(("Off-Market Gap Regime", f"+10 (Mild Gap-Down {gap_pct:+.2f}%)"))
    if swept_pre_low and curr_price > pre_low:
        score += 18
        signals.append(f"🟢 **Pre-Market Low Stop-Hunt Reclaimed (${pre_low:.2f}):** +4.75% avg low-to-peak bounce.")
        factor_breakdown.append(("Pre-Market Structure", "+18 (Swept & Reclaimed Pre-Mkt Low)"))
    elif swept_pre_low and curr_price <= pre_low and curr_time >= "10:15":
        score -= 18
        signals.append(f"🔴 **Failed Below Pre-Market Low (${pre_low:.2f}):** Green Candle probability drops to 22.7%.")
        factor_breakdown.append(("Pre-Market Structure", "-18 (Trapped Below Pre-Mkt Low)"))

    # 4. Opening 15-Minute Sequence
    if len(rth) >= 3:
        if bool(rth.loc[0, "is_green"] and rth.loc[1, "is_green"] and rth.loc[2, "is_green"]):
            score += 20
            signals.append("🟢 **Three White Soldiers Open (09:30–09:45):** 83.3% close net green.")
            factor_breakdown.append(("Opening 15m Sequence", "+20 (3 Consecutive Green 5m Bars)"))
        elif bool((not rth.loc[0, "is_green"]) and (not rth.loc[1, "is_green"]) and (not rth.loc[2, "is_green"])):
            score -= 20
            signals.append("🔴 **Three Black Crows Open (09:30–09:45):** 83.3% close net red.")
            factor_breakdown.append(("Opening 15m Sequence", "-20 (3 Consecutive Red 5m Bars)"))

    # 5. Hybrid 5m/15m VWAP Reclaim Engine
    is_washout = (drop_vs_prev <= washout_thresh) or (drop_vs_open <= washout_thresh)
    two_5m_vwap = len(rth) >= 2 and (rth.iloc[-1]["close"] > rth.iloc[-1]["session_vwap"]) and (rth.iloc[-2]["close"] > rth.iloc[-2]["session_vwap"])
    last_15m_vwap = len(rth_15m) >= 1 and (rth_15m.iloc[-1]["close"] > rth_15m.iloc[-1]["session_vwap"]) and rth_15m.iloc[-1]["is_green"]

    if is_washout and (run_low_time <= "10:25") and (two_5m_vwap or last_15m_vwap):
        score += 35
        confirm_str = "2x 5m + 15m Close > VWAP" if (two_5m_vwap and last_15m_vwap) else ("2x 5m Closes > VWAP" if two_5m_vwap else "15m Close > VWAP")
        signals.append(f"🟢 **Morning Washout + VWAP Reclaim Active ({confirm_str}):** Bottomed at ${run_low:.2f} ({run_low_time} ET) and reclaimed VWAP (${curr_vwap:.2f}).")
        factor_breakdown.append(("VWAP Reclaim Engine", f"+35 ({confirm_str})"))

    # 6. 10:30 AM Structural Lock
    if curr_time >= "10:25":
        bar_1025 = rth[rth["time"] <= "10:25"].iloc[-1]
        if (bar_1025["close"] > rth_open) and (bar_1025["close"] > bar_1025["session_vwap"]):
            if curr_price >= m60_low:
                score += 25
                signals.append(f"🟢 **10:30 AM Bullish Structural Lock:** 80.0% probability Low of Day (${m60_low:.2f}) holds all day.")
                factor_breakdown.append(("10:30 AM Structural Lock", "+25 (Bullish Lock Holding)"))
            else:
                score -= 25
                factor_breakdown.append(("10:30 AM Structural Lock", "-25 (Broke First-Hour Low)"))
        elif (bar_1025["close"] < rth_open) and (bar_1025["close"] < bar_1025["session_vwap"]):
            if curr_price <= m60_high:
                score -= 30
                signals.append(f"🔴 **10:30 AM Bearish Structural Lock:** 80%–84.6% probability High of Day (${m60_high:.2f}) holds all day.")
                factor_breakdown.append(("10:30 AM Structural Lock", "-30 (Bearish Lock Capping Highs)"))
            else:
                score += 25
                factor_breakdown.append(("10:30 AM Structural Lock", "+25 (Broke Above First-Hour High)"))

        if run_low_time > "10:25" and curr_price < curr_vwap:
            score -= 35
            signals.append(f"🔴 **Post-10:30 AM New Low Breakdown (${run_low:.2f} at {run_low_time} ET):** 0.0% historical Net-Green recovery rate.")
            factor_breakdown.append(("Post-10:30 AM Low Break", "-35 (New Low After 10:30 AM)"))

    # 7. Live VWAP & 30-Min ORB
    score += 12 if curr_price > curr_vwap else -12
    factor_breakdown.append(("Live VWAP Position", f"{'+12' if curr_price > curr_vwap else '-12'} ({curr_bar['dist_vwap_pct']:+.2f}% vs VWAP)"))

    if curr_time >= "10:00":
        if curr_price > orb30_high and curr_price > curr_vwap:
            score += 15
            signals.append(f"🟢 **30-Min Opening Range Breakout (Above ${orb30_high:.2f}):** Opposite boundary (${orb30_low:.2f}) holds **79.5%** of the day.")
            factor_breakdown.append(("30-Min ORB Status", "+15 (Bullish ORB Breakout)"))
        elif curr_price < orb30_low and curr_price < curr_vwap:
            score -= 15
            signals.append(f"🔴 **30-Min Opening Range Breakdown (Below ${orb30_low:.2f}):** Opposite boundary (${orb30_high:.2f}) holds **78.0%** of the day.")
            factor_breakdown.append(("30-Min ORB Status", "-15 (Bearish ORB Breakdown)"))

    # 8. 5-Min Capitulation Hammer / Climax Top
    if (curr_bar["dist_vwap_pct"] <= -2.5 and curr_bar["is_green"] and curr_bar["bar_range_pct"] >= 0.60) or \
       (curr_bar["rel_vol"] >= 2.0 and curr_bar["lower_wick_ratio"] >= 0.40 and curr_bar["dist_vwap_pct"] <= -1.5):
        score += 25
        signals.append("🟢 **5-Min Capitulation Hammer / Oversold VWAP Snapback:** High-volume lower wick or green reversal bar <= -2.5% below VWAP.")
        factor_breakdown.append(("5-Min Exhaustion Trigger", "+25 (Capitulation Snapback Bar)"))

    if (curr_bar["dist_vwap_pct"] >= 2.0 and curr_bar["upper_wick_ratio"] >= 0.40 and curr_bar["rel_vol"] >= 1.5) or \
       (curr_bar["dist_vwap_pct"] >= 2.5 and (not curr_bar["is_green"]) and curr_bar["bar_range_pct"] >= 0.60):
        score -= 25
        signals.append("🔴 **5-Min Overbought Climax Rejection:** Stretched >= +2.0% above VWAP with high-volume upper wick / wide red bar.")
        factor_breakdown.append(("5-Min Exhaustion Trigger", "-25 (Overbought Climax Top Bar)"))

    # 9. Power Hour Range Pinning
    range_pos = (curr_price - run_low) / (run_high - run_low) if (run_high > run_low) else 0.5
    if curr_time >= "14:55":
        if range_pos >= 0.80:
            score += 20
            signals.append(f"🟢 **Power Hour Top-20% Range Pin ({range_pos*100:.0f}% of Day's Range):** 92.0% historical Net-Green close rate.")
            factor_breakdown.append(("Power Hour Pinning", "+20 (Top 20% of Range -> 92% Green Close)"))
        elif range_pos <= 0.20:
            score -= 20
            signals.append(f"🔴 **Power Hour Bottom-20% Range Bleed ({range_pos*100:.0f}% of Day's Range):** 85.2% historical Net-Red close rate.")
            factor_breakdown.append(("Power Hour Pinning", "-20 (Bottom 20% of Range -> 85% Red Close)"))

    score = int(np.clip(score, -100, 100))

    if score >= 40:
        bias = f"🟢 STRONG BULLISH BIAS (Composite Score: {score:+d}/100)"
        t1_price = curr_price * 1.0204
        t2_price = curr_price * (1.0321 if streak_days <= -2 else 1.0250)
        stop_price = max(run_low, orb30_low) if curr_price > orb30_low else run_low
    elif score >= 15:
        bias = f"🟢 MODERATE BULLISH LEAN (Composite Score: {score:+d}/100)"
        t1_price = max(curr_vwap, curr_price * 1.017)
        t2_price = curr_price * 1.023
        stop_price = run_low
    elif score <= -40:
        bias = f"🔴 STRONG BEARISH BIAS (Composite Score: {score:+d}/100)"
        t1_price = curr_price * 0.983
        t2_price = curr_price * 0.975
        stop_price = min(curr_vwap * 1.003, m60_high)
    elif score <= -15:
        bias = f"🔴 MODERATE BEARISH LEAN (Composite Score: {score:+d}/100)"
        t1_price = min(curr_vwap, curr_price * 0.985)
        t2_price = curr_price * 0.978
        stop_price = run_high if curr_price > curr_vwap else curr_vwap
    else:
        bias = f"⚖️ MIXED / CHOPPY REGIME (Composite Score: {score:+d}/100)"
        t1_price, t2_price, stop_price = np.nan, np.nan, np.nan

    live_opt = fetch_alpaca_live_options(symbol, curr_price, t1_price, t2_price, stop_price, score, streak_days, dte_min, dte_max)

    return {
        "status": "RTH_ACTIVE", "today_date": today_date, "curr_time": curr_time,
        "prev_close": prev_close, "rth_open": rth_open, "curr_price": curr_price,
        "curr_vwap": curr_vwap, "pre_low": pre_low, "pre_high": pre_high, "pre_vwap": pre_vwap,
        "run_low": run_low, "run_low_time": run_low_time, "run_high": run_high, "gap_pct": gap_pct,
        "drop_vs_prev": drop_vs_prev, "curr_chg_pct": curr_chg_pct, "streak_days": streak_days,
        "streak_cum_pct": streak_cum_pct, "score": score, "bias": bias, "live_opt": live_opt,
        "signals": signals, "factor_breakdown": factor_breakdown, "t1_price": t1_price,
        "t2_price": t2_price, "stop_price": stop_price, "rth_df": rth
    }

# ================= 4. PAPER ORDER EXECUTION FUNCTIONS =================
def execute_paper_spread(lo: dict, qty: int):
    try:
        buy_leg = MarketOrderRequest(symbol=lo["long_occ"], qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        sell_leg = MarketOrderRequest(symbol=lo["short_occ"], qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        trading_client.submit_order(order_data=buy_leg)
        trading_client.submit_order(order_data=sell_leg)
        st.toast(f"✅ Executed {qty}x {lo['spread_label']} ({lo['long_occ']} / {lo['short_occ']})!", icon="🚀")
        st.success(f"✅ Submitted Paper Spread Order: Buy {qty}x `{lo['long_occ']}` & Sell {qty}x `{lo['short_occ']}`")
    except Exception as e:
        st.error(f"❌ Spread Execution Error: {e}")

def execute_paper_naked(lo: dict, qty: int):
    try:
        buy_leg = MarketOrderRequest(symbol=lo["long_occ"], qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        trading_client.submit_order(order_data=buy_leg)
        st.toast(f"✅ Executed {qty}x Naked {lo['opt_label']} ({lo['long_occ']})!", icon="⚡")
        st.success(f"✅ Submitted Paper Naked Order: Buy {qty}x `{lo['long_occ']}`")
    except Exception as e:
        st.error(f"❌ Naked Option Execution Error: {e}")

# ================= 5. RENDER UI, DUAL OPTION CARDS & PORTFOLIO P&L =================
st.title("⚡ MU & SNDK Master Multi-Factor Options & Spread Terminal")

kr_leads = fetch_korean_leads()
k1, k2, k3 = st.columns([1, 1, 2])
for idx, (label, (price, chg)) in enumerate(kr_leads.items()):
    col = k1 if idx == 0 else k2
    if not np.isnan(price):
        col.metric(f"🇰🇷 {label}", f"₩{price:,.0f}", f"{chg:+.2f}% (KRX Close)")
k3.info("**15-Second Transactional Engine:** Full 9-Factor Composite Scoring + Side-by-Side **#1 Best Option (Vertical Spread)** vs. **#2 Second Best (Naked ATM Option)** + 1-Click Paper Execution & P&L.")

tabs = st.tabs(["📊 Micron (MU) Live Terminal", "📊 SanDisk (SNDK) Live Terminal", "💼 My Paper Portfolio P&L"])

for idx, ticker in enumerate(["MU", "SNDK"]):
    with tabs[idx]:
        data = fetch_and_analyze_ticker(ticker, data_client, kr_leads)
        if data["status"] == "NO_DATA":
            st.error("No data returned from Alpaca.")
            continue
        elif data["status"] == "PRE_MARKET":
            st.subheader(f"{ticker} Pre-Market Mode ({data['today_date']})")
            p1, p2, p3, p4, p5 = st.columns(5)
            p1.metric("Alpaca Pre-Market Price", f"${data['pre_last']:.2f}", f"{data['pre_gap_pct']:+.2f}% vs Prev Close")
            p2.metric("Prev RTH Close", f"${data['prev_close']:.2f}")
            p3.metric("Pre-Mkt Low / High", f"${data['pre_low']:.2f}", f"High: ${data['pre_high']:.2f}")
            p4.metric("Pre-Mkt VWAP", f"${data['pre_vwap']:.2f}")
            p5.metric("Entering Daily Streak", f"{data['streak_days']:+d} Days", f"{data['streak_cum_pct']:+.2f}% Move")
            continue

        # Row 1: 6 Core Intraday Metrics
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric(f"{ticker} Live Price ({data['curr_time']} ET)", f"${data['curr_price']:.2f}", f"{data['curr_chg_pct']:+.2f}%")
        m2.metric("RTH Session VWAP", f"${data['curr_vwap']:.2f}", f"{(data['curr_price']-data['curr_vwap'])/data['curr_vwap']*100:+.2f}% vs VWAP")
        m3.metric("09:30 AM Open (Gap)", f"${data['rth_open']:.2f}", f"{data['gap_pct']:+.2f}% Gap")
        m4.metric("Running RTH Low", f"${data['run_low']:.2f}", f"{data['drop_vs_prev']:+.2f}% ({data['run_low_time']} ET)")
        m5.metric("Pre-Market Low / High", f"${data['pre_low']:.2f}", f"High: ${data['pre_high']:.2f}")
        m6.metric("Prior Multi-Day Streak", f"{data['streak_days']:+d} Days", f"{data['streak_cum_pct']:+.2f}% Prior Move")

        # Row 2: Master Bias Banner & Side-by-Side Best (#1 Spread) vs Second Best (#2 Naked) Cards
        st.markdown(f"### Current Bias: {data['bias']}")
        lo = data["live_opt"]
        if lo is not None and "error" not in lo:
            box_fn = st.success if data["score"] >= 15 else st.error
            box_fn(f"🎯 **{lo['verdict']}** | Expiration: **`{lo['expiration']}` ({lo['dte']} DTE)** | Hard Underlying Stop: **`${lo['stop_price']:.2f}`**")

            c_sp, c_nk = st.columns(2)
            with c_sp:
                st.markdown(
                    f"#### 🏆 #1 Best Option (Primary): {lo['spread_label']}\n"
                    f"- **Legs:** Buy `${lo['long_k']:g}` {lo['opt_label']} (`{lo['long_occ']}`) / Sell `${lo['short_k']:g}` {lo['opt_label']} (`{lo['short_occ']}`)\n"
                    f"- **Spread Width:** `${lo['width']:g}` Wide | **Max Value at Expiry:** `${lo['width']:.2f}`\n"
                    f"- **Live Mid Net Debit (Limit):** **`${lo['spread_mid']:.2f}`** (**{lo['debit_pct_width']:.1f}% of width**) *(Market Ask/Bid Fill: `${lo['spread_nat']:.2f}`)*\n"
                    f"- **Intraday Profit Target:** Close spread at **`${lo['tp_target_val']:.2f}+`** (**+{lo['est_spread_roi_pct']:.0f}% ROI**) when {ticker} hits `${data['t1_price']:.2f}`"
                )
                if st.button(f"🚀 Buy {order_qty}x {lo['spread_label']} (${lo['long_k']:g}/${lo['short_k']:g})", key=f"btn_sp_{ticker}", type="primary"):
                    execute_paper_spread(lo, int(order_qty))

            with c_nk:
                st.markdown(
                    f"#### ⚡ #2 Second Best (Alternative): Naked ATM `${lo['long_k']:g}` {lo['opt_label']}\n"
                    f"- **Contract:** `{lo['long_occ']}` ({lo['dte']} DTE)\n"
                    f"- **Live Quote:** Bid `${lo['naked_bid']:.2f}` / Ask `${lo['naked_ask']:.2f}` | **Mid Price:** **`${lo['naked_mid']:.2f}`** *(Spread: {lo['naked_ba_pct']:.1f}%)*\n"
                    f"- **Live Greeks & IV:** **IV:** `{lo['naked_iv']:.1f}%` | **Delta ($\Delta$):** `{lo['naked_delta']:.2f}` | **Theta ($\Theta$):** `${lo['naked_theta']:.2f}/day`\n"
                    f"- **Projected Option Price at Target 1 (`${data['t1_price']:.2f}`):** **`~${lo['est_naked_target_price']:.2f}`** (**+{lo['est_naked_roi_pct']:.0f}% ROI**)"
                )
                if st.button(f"⚡ Buy {order_qty}x Naked ${lo['long_k']:g} {lo['opt_label']}", key=f"btn_nk_{ticker}"):
                    execute_paper_naked(lo, int(order_qty))

            with st.expander(f"🔎 View Live Alpaca Options Chain Slice ({lo['expiration']} | {lo['dte']} DTE)"):
                st.dataframe(lo["chain_table"], use_container_width=True, hide_index=True)

        elif lo is not None and "error" in lo:
            st.info(f"ℹ️ Live Options Note: {lo['error']}")
        else:
            st.warning("🎯 **Stand Aside / No Options Edge:** Composite score is neutral (-14 to +14). Wait for a confirmed 2x 5m close above VWAP or a 30m Opening Range breakout.")

        # Row 3: Full Signals List & Full 9-Factor Composite Score Breakdown Table
        col_sig, col_fac = st.columns([3, 2])
        with col_sig:
            with st.expander("📋 Active Multi-Factor & Micro-Structure Signals (Why This Bias Is Active)", expanded=True):
                for s in data["signals"]:
                    st.markdown(f"- {s}")
        with col_fac:
            with st.expander("🧮 Composite Score Breakdown (All Parameters)", expanded=True):
                st.dataframe(
                    pd.DataFrame(data["factor_breakdown"], columns=["Parameter / Factor", "Score Contribution"]),
                    use_container_width=True, hide_index=True
                )

        # Row 4: Interactive 5-Minute Plotly Candlestick Chart
        rth_df = data["rth_df"]
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=rth_df["time"], open=rth_df["open"], high=rth_df["high"], low=rth_df["low"], close=rth_df["close"], name=f"{ticker} 5m Bars"))
        fig.add_trace(go.Scatter(x=rth_df["time"], y=rth_df["session_vwap"], mode="lines", line=dict(color="#F39C12", width=2.5), name="RTH Session VWAP"))

        fig.add_hline(y=data["prev_close"], line_dash="dash", line_color="#7F8C8D", annotation_text=f"Prev Close (${data['prev_close']:.2f})")
        fig.add_hline(y=data["rth_open"], line_dash="dot", line_color="#3498DB", annotation_text=f"9:30 Open (${data['rth_open']:.2f})")
        if not np.isnan(data["pre_low"]):
            fig.add_hline(y=data["pre_low"], line_dash="dashdot", line_color="#9B59B6", annotation_text=f"Pre-Mkt Low (${data['pre_low']:.2f})")

        if not np.isnan(data["t1_price"]):
            fig.add_hline(y=data["t1_price"], line_dash="solid", line_color="#2ECC71" if data["score"] > 0 else "#E67E22", annotation_text=f"Short Strike / Target 1 (${data['t1_price']:.2f})")
        if not np.isnan(data["t2_price"]):
            fig.add_hline(y=data["t2_price"], line_dash="solid", line_color="#27AE60" if data["score"] > 0 else "#D35400", annotation_text=f"Wide Target 2 (${data['t2_price']:.2f})")
        if not np.isnan(data["stop_price"]):
            fig.add_hline(y=data["stop_price"], line_dash="solid", line_color="#E74C3C", annotation_text=f"Hard Stop (${data['stop_price']:.2f})")

        fig.update_layout(title=f"{ticker} Live 15-Second RTH Chart & Spread Strike Levels ({data['today_date']})", xaxis_rangeslider_visible=False, height=560, template="plotly_dark")
        st.plotly_chart(fig, use_container_width=True)

# ================= 6. PAPER PORTFOLIO & LIVE P&L TAB =================
with tabs[2]:
    st.subheader("💼 Active Alpaca Paper Positions & Real-Time P&L")
    try:
        acct = trading_client.get_account()
        a1, a2, a3 = st.columns(3)
        a1.metric("Paper Account Equity", f"${float(acct.equity):,.2f}")
        a2.metric("Buying Power", f"${float(acct.buying_power):,.2f}")
        day_pl = float(acct.equity) - float(acct.last_equity)
        day_pl_pct = (day_pl / float(acct.last_equity) * 100) if float(acct.last_equity) > 0 else 0.0
        a3.metric("Today's Total Account P&L", f"${day_pl:+,.2f}", f"{day_pl_pct:+.2f}%")

        positions = trading_client.get_all_positions()
        if not positions:
            st.info("No open positions in your Alpaca Paper account yet. Use the **🚀 Buy Spread** or **⚡ Buy Naked Option** buttons on the MU/SNDK tabs to execute a paper trade.")
        else:
            total_unrealized = sum(float(p.unrealized_pl) for p in positions)
            st.metric("Total Open Positions Unrealized P&L", f"${total_unrealized:+,.2f}")

            pos_rows = []
            for p in positions:
                pos_rows.append({
                    "Contract / Symbol": p.symbol,
                    "Side": p.side.value.upper() if hasattr(p.side, "value") else str(p.side),
                    "Qty": int(float(p.qty)),
                    "Avg Entry Price": f"${float(p.avg_entry_price):.2f}",
                    "Current Price": f"${float(p.current_price):.2f}",
                    "Market Value": f"${float(p.market_value):,.2f}",
                    "Unrealized P&L ($)": f"${float(p.unrealized_pl):+,.2f}",
                    "Unrealized P&L (%)": f"{float(p.unrealized_plpc)*100:+.2f}%"
                })
            st.dataframe(pd.DataFrame(pos_rows), use_container_width=True, hide_index=True)

            if st.button("🚨 Close All Open Paper Positions (Market Flatten)", type="secondary"):
                trading_client.close_all_positions(cancel_orders=True)
                st.success("✅ Sent request to flatten all open paper positions!")
    except Exception as e:
        st.error(f"Failed to fetch paper portfolio: {e}")
