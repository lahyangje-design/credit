"""채권 주간 전략 대시보드 (Duration / Term Sector / Credit Sector)
데이터: 한국은행 ECOS 시장금리(일별, 금융투자협회 최종호가수익률 기준)
작성일을 입력하면 그 시점까지의 데이터만으로 화면을 다시 계산합니다.
"""

import re
from datetime import date, timedelta
from statistics import NormalDist

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

try:  # 윈도우 인증서 저장소 사용 (회사망 보안 프로그램·인증서 체인 문제 대응)
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# ── ECOS 설정 ────────────────────────────────────────────────
ECOS_BASE = "{scheme}://ecos.bok.or.kr/api/"
MARKET_STAT = "817Y002"  # 시장금리(일별)
# 키: (ECOS 항목명, 예비 코드) — 항목명으로 먼저 찾고, 실패하면 예비 코드 사용
SERIES = {
    "1Y": ("국고채(1년)", "010190000"),
    "2Y": ("국고채(2년)", "010195000"),
    "3Y": ("국고채(3년)", "010200000"),
    "5Y": ("국고채(5년)", "010200001"),
    "10Y": ("국고채(10년)", "010210000"),
    "30Y": ("국고채(30년)", "010230000"),
    "KDB1Y": ("산금채(1년)", "010260000"),
    "CORP3Y": ("회사채(3년,AA-)", "010300000"),
}
FETCH_START = "20140101"

TERM_SPREADS = {  # bp
    "1y/2y": lambda d: d["2Y"] - d["1Y"],
    "2y/3y": lambda d: d["3Y"] - d["2Y"],
    "3y/5y": lambda d: d["5Y"] - d["3Y"],
    "5y/10y": lambda d: d["10Y"] - d["5Y"],
    "10y/30y": lambda d: d["30Y"] - d["10Y"],
    "3y/10y": lambda d: d["10Y"] - d["3Y"],
    "3y/5y/10y": lambda d: 2 * d["5Y"] - d["3Y"] - d["10Y"],
}
CREDIT_SPREADS = {
    "산금채 1Y": lambda d: d["KDB1Y"] - d["1Y"],
    "회사채AA- 3Y": lambda d: d["CORP3Y"] - d["3Y"],
}
FACTORS = ["경제지표", "기술적지표", "정책", "해외", "수급", "종합"]

# ── 팔레트 ───────────────────────────────────────────────────
INK, MUTED, GRID = "#14213D", "#5B6577", "#DDE2EA"
TEAL, RED, SLATE = "#0F766E", "#B42318", "#7C8BA1"

st.set_page_config(page_title="채권 주간 전략", page_icon="📊", layout="wide")
st.markdown(
    f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600&display=swap');
html, body, [class*="css"], .stMarkdown {{
  font-family: 'IBM Plex Sans KR', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif; }}
.block-container {{ padding-top: 1.6rem; max-width: 1280px; }}
.doc-title {{ font-size: 1.9rem; font-weight: 600; color: {INK}; letter-spacing: -0.02em; margin: 0; }}
.doc-meta {{ color: {MUTED}; font-size: .95rem; margin: .2rem 0 1.2rem; font-variant-numeric: tabular-nums; }}
.legs {{ display:flex; gap:2.2rem; flex-wrap:wrap; padding:.8rem 0; border-top:1px solid {GRID};
  border-bottom:1px solid {GRID}; margin-bottom:1.2rem; font-variant-numeric: tabular-nums; }}
.legs .k {{ color:{MUTED}; font-size:.85rem; }}
.legs .v {{ font-size:1.35rem; font-weight:600; color:{INK}; }}
.legs .d {{ font-size:.85rem; margin-left:.35rem; }}
.sec {{ font-size:1.05rem; font-weight:600; color:{INK}; margin: 1.4rem 0 .4rem; }}
.chart-meta {{ color:{MUTED}; font-size:.82rem; margin-top:-.4rem; font-variant-numeric: tabular-nums; }}
</style>
""",
    unsafe_allow_html=True,
)


# ── ECOS 호출 ────────────────────────────────────────────────
class EcosError(Exception):
    pass


def ecos_json(api_key: str, path: str):
    """path 예: 'StatisticSearch/{key}/json/kr/1/100/...'. 데이터 없음이면 None."""
    tpl = ECOS_BASE + path

    def _get(scheme, verify=True):
        return requests.get(tpl.format(scheme=scheme, key=api_key), timeout=30, verify=verify)

    try:
        try:
            r = _get("https")
        except requests.exceptions.SSLError:
            try:
                r = _get("http")
            except requests.exceptions.SSLError:
                import urllib3
                urllib3.disable_warnings()
                r = _get("https", verify=False)
        r.raise_for_status()
        js = r.json()
    except requests.Timeout as e:
        raise EcosError("ECOS 서버 응답이 없습니다.") from e
    except requests.RequestException as e:
        detail = str(e).replace(api_key, "***")[:300]
        raise EcosError(f"ECOS 서버에 연결하지 못했습니다 ({type(e).__name__}).\n\n상세: {detail}") from e
    except ValueError as e:
        raise EcosError("ECOS 응답을 해석하지 못했습니다. API 키를 확인하세요.") from e

    if "RESULT" in js:
        code, msg = js["RESULT"].get("CODE", ""), js["RESULT"].get("MESSAGE", "")
        if code == "INFO-200":  # 해당하는 데이터가 없습니다
            return None
        raise EcosError(f"ECOS {code}: {msg}")
    return js


def _norm(s: str) -> str:
    return re.sub(r"\s", "", s or "")


@st.cache_data(ttl=86400, show_spinner=False)
def item_codes(api_key: str) -> dict:
    try:
        js = ecos_json(api_key, f"StatisticItemList/{{key}}/json/kr/1/500/{MARKET_STAT}")
    except EcosError:
        return {}
    rows = (js or {}).get("StatisticItemList", {}).get("row", [])
    return {_norm(r.get("ITEM_NAME")): r.get("ITEM_CODE") for r in rows if r.get("CYCLE", "D") == "D"}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch(api_key: str, stat: str, item: str, start: str, end: str) -> pd.Series:
    js = ecos_json(api_key, f"StatisticSearch/{{key}}/json/kr/1/100000/{stat}/D/{start}/{end}/{item}")
    rows = (js or {}).get("StatisticSearch", {}).get("row", [])
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series(
        pd.to_numeric([r.get("DATA_VALUE") for r in rows], errors="coerce"),
        index=pd.to_datetime([r["TIME"] for r in rows], format="%Y%m%d"),
    )
    return s.dropna().sort_index()


def load_all(api_key: str):
    end = date.today().strftime("%Y%m%d")
    codes = item_codes(api_key)
    data, missing = {}, []
    for key, (name, fallback) in SERIES.items():
        code = codes.get(_norm(name), fallback)
        s = fetch(api_key, MARKET_STAT, code, FETCH_START, end)
        if s.empty:
            missing.append(name)
        else:
            data[key] = s
    df = pd.DataFrame(data)
    try:  # 기준금리 (없어도 진행)
        base = fetch(api_key, "722Y001", "0101000", FETCH_START, end)
    except EcosError:
        base = pd.Series(dtype=float)
    return df, base, missing


# ── 계산 ─────────────────────────────────────────────────────
def window_stats(s: pd.Series, n_stats: int, n_boll: int, z: float) -> dict:
    s = s.dropna()
    w = s.iloc[-n_stats:]
    mid = s.rolling(n_boll).mean()
    sd = s.rolling(n_boll).std()
    avg, std = w.mean(), w.std()
    last = w.iloc[-1]
    return dict(
        w=w, avg=avg, max=w.max(), min=w.min(), hi=avg + z * std, lo=avg - z * std,
        bh=(mid + 2 * sd).reindex(w.index), bm=mid.reindex(w.index), bl=(mid - 2 * sd).reindex(w.index),
        last=last, pct=(w <= last).mean() * 100, z=(last - avg) / std if std else float("nan"),
    )


def value_on(s: pd.Series, d) -> float:
    s = s.dropna()
    s = s[s.index <= d]
    return s.iloc[-1] if len(s) else float("nan")


# ── 차트 ─────────────────────────────────────────────────────
def base_layout(fig, height=330, unit="bp"):
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=8, b=8), hovermode="x unified",
        plot_bgcolor="white", paper_bgcolor="white",
        font=dict(family="IBM Plex Sans KR, sans-serif", color=INK, size=11),
        legend=dict(orientation="h", yanchor="top", y=-0.12, x=0, font=dict(size=10)),
    )
    fig.update_xaxes(showgrid=False, linecolor=GRID, tickformat="%y.%m")
    fig.update_yaxes(gridcolor=GRID, zeroline=False, title_text=unit, title_font=dict(size=10))
    return fig


def spread_fig(st_: dict, area=False, show_boll=True, show_band=True, ref_date=None):
    w = st_["w"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=w.index, y=w, name="스프레드", line=dict(color=INK, width=1.6),
        fill="tozeroy" if area else None, fillcolor="rgba(20,33,61,0.10)",
        hovertemplate="%{y:.1f}bp<extra></extra>"))
    if show_band:
        for key, label, color, dash in [("avg", "평균", TEAL, "solid"), ("max", "최대", SLATE, "dot"),
                                        ("min", "최소", SLATE, "dot"), ("hi", "+σ", RED, "dash"),
                                        ("lo", "−σ", RED, "dash")]:
            fig.add_trace(go.Scatter(x=[w.index[0], w.index[-1]], y=[st_[key]] * 2, name=label, mode="lines",
                                     line=dict(color=color, width=1, dash=dash), hoverinfo="skip"))
    if show_boll:
        fig.add_trace(go.Scatter(x=w.index, y=st_["bh"], name="Bol-Hi",
                                 line=dict(color="rgba(15,118,110,0.55)", width=1), hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=w.index, y=st_["bm"], name="Bol-Mid",
                                 line=dict(color="rgba(15,118,110,0.55)", width=1, dash="dot"), hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=w.index, y=st_["bl"], name="Bol-Lo",
                                 line=dict(color="rgba(15,118,110,0.55)", width=1), hoverinfo="skip"))
    if ref_date is not None:
        fig.add_vline(x=ref_date, line=dict(color=GRID, width=1, dash="dot"))
    return base_layout(fig)


def chart_meta(st_: dict) -> str:
    return (f"<div class='chart-meta'>현재 {st_['last']:.1f}bp · 평균 {st_['avg']:.1f} · "
            f"범위 {st_['min']:.1f}~{st_['max']:.1f} · 백분위 {st_['pct']:.0f}% · Z {st_['z']:+.2f}</div>")


def chg_html(x, unit="bp", digits=1):
    if pd.isna(x):
        return "<span class='d'>–</span>"
    color = TEAL if x > 0 else RED if x < 0 else MUTED
    return f"<span class='d' style='color:{color}'>{x:+.{digits}f}{unit}</span>"


# ── 사이드바 ──────────────────────────────────────────────────
with st.sidebar:
    st.subheader("작성 조건")
    report_date = st.date_input("작성일", value=date.today(), min_value=date(2016, 1, 1),
                                max_value=date.today(), format="YYYY-MM-DD")
    include_today = st.checkbox("작성일 당일 금리 포함", value=False,
                                help="끄면 작성일 직전 영업일이 기준일이 됩니다 (장 마감 전 작성 기준).")
    st.divider()
    conf = st.number_input("신뢰수준(%)", min_value=50.0, max_value=99.9, value=95.0, step=1.0)
    n_stats = st.number_input("통계 기간(영업일)", min_value=60, max_value=1000, value=250, step=10)
    n_boll = st.number_input("볼린저 기간(영업일)", min_value=5, max_value=60, value=20, step=1)
    show_boll = st.checkbox("볼린저밴드 표시", value=True)
    show_band = st.checkbox("평균·최대·최소·±σ 표시", value=True)
    show_ma = st.checkbox("이동평균(20·60·120) 표시", value=False)
    st.divider()
    try:
        api_key = st.secrets["ECOS_API_KEY"]
    except Exception:
        api_key = ""
    if not api_key:
        api_key = st.text_input("ECOS API 키", type="password")
    if st.button("데이터 새로고침"):
        fetch.clear()
        item_codes.clear()
        st.rerun()

z = NormalDist().inv_cdf((100 + conf) / 200)
n_stats, n_boll = int(n_stats), int(n_boll)

if not api_key:
    st.info("왼쪽 사이드바에 ECOS API 키를 입력하면 데이터를 불러옵니다.")
    st.stop()

try:
    with st.spinner("ECOS에서 금리를 불러오는 중"):
        raw, base_rate, missing = load_all(api_key)
except EcosError as e:
    st.error(str(e))
    st.stop()

if raw.empty or "3Y" not in raw:
    st.error("국고채 금리를 불러오지 못했습니다. API 키와 네트워크를 확인하세요.")
    st.stop()

# ── 작성일 기준으로 자르기 ──────────────────────────────────────
cut = pd.Timestamp(report_date) + (pd.Timedelta(days=1) if include_today else pd.Timedelta(0))
hist = raw[raw.index < cut]
if hist.empty:
    st.error("작성일 이전 데이터가 없습니다. 작성일을 늦춰주세요.")
    st.stop()
ref = hist["3Y"].dropna().index[-1]                      # 기준일
cmp_cands = hist.index[hist.index <= ref - pd.Timedelta(days=7)]
cmp_ = cmp_cands[-1] if len(cmp_cands) else hist.index[0]  # 비교일 (1주 전)
bp = hist * 100  # bp 단위

st.markdown("<p class='doc-title'>채권 주간 전략</p>", unsafe_allow_html=True)
st.markdown(
    f"<p class='doc-meta'>작성일 {report_date:%Y-%m-%d} · 기준일 {ref:%Y-%m-%d} · 비교일 {cmp_:%Y-%m-%d}</p>",
    unsafe_allow_html=True)
if missing:
    st.warning("ECOS에서 다음 항목을 찾지 못해 관련 화면을 건너뜁니다: " + ", ".join(missing))
if (pd.Timestamp(report_date) - ref).days > 7:
    st.warning("기준일이 작성일보다 1주 이상 앞섭니다. 해당 기간 데이터가 아직 ECOS에 없을 수 있습니다.")
if len(hist) < n_stats + n_boll:
    st.warning("통계 기간에 비해 과거 데이터가 부족해 일부 수치가 짧은 기간으로 계산됩니다.")

tab_d, tab_t, tab_c = st.tabs(["Duration Strategy", "Term Sector Strategy", "Credit Sector Strategy"])

# ── Duration Strategy ──────────────────────────────────────────
with tab_d:
    legs = []
    for key, label in [("3Y", "국고 3년"), ("10Y", "국고 10년")]:
        if key in hist:
            v, p = value_on(hist[key], ref), value_on(hist[key], cmp_)
            legs.append(f"<div><div class='k'>{label}</div><span class='v'>{v:.3f}%</span>{chg_html((v - p) * 100)}</div>")
    if {"3Y", "10Y"} <= set(hist):
        s310 = bp["10Y"] - bp["3Y"]
        v, p = value_on(s310, ref), value_on(s310, cmp_)
        legs.append(f"<div><div class='k'>3y/10y</div><span class='v'>{v:.1f}bp</span>{chg_html(v - p)}</div>")
    br = value_on(base_rate, ref) if not base_rate.empty else float("nan")
    if not pd.isna(br):
        legs.append(f"<div><div class='k'>기준금리</div><span class='v'>{br:.2f}%</span></div>")
    st.markdown(f"<div class='legs'>{''.join(legs)}</div>", unsafe_allow_html=True)
    st.caption("변동은 비교일 대비")

    st.markdown("<div class='sec'>□ Strategy Chart · 국고 3년</div>", unsafe_allow_html=True)
    s3 = hist["3Y"].dropna()
    w = s3.iloc[-n_stats:]
    mid, sd = s3.rolling(n_boll).mean(), s3.rolling(n_boll).std()
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=w.index, y=w, name="국고 3년", line=dict(color=INK, width=1.8),
                             hovertemplate="%{y:.3f}%<extra></extra>"))
    if show_boll:
        for ser, nm, dash in [(mid + 2 * sd, "Bol-Hi", "solid"), (mid, "Bol-Mid", "dot"), (mid - 2 * sd, "Bol-Lo", "solid")]:
            fig.add_trace(go.Scatter(x=w.index, y=ser.reindex(w.index), name=nm, hoverinfo="skip",
                                     line=dict(color="rgba(15,118,110,0.55)", width=1, dash=dash)))
    if show_ma:
        for n, c in [(20, "#C2410C"), (60, "#7C3AED"), (120, "#64748B")]:
            fig.add_trace(go.Scatter(x=w.index, y=s3.rolling(n).mean().reindex(w.index), name=f"{n}MA",
                                     line=dict(color=c, width=1), hoverinfo="skip"))
    if not base_rate.empty:
        b = base_rate.reindex(base_rate.index.union(w.index)).ffill().reindex(w.index)
        fig.add_trace(go.Scatter(x=w.index, y=b, name="기준금리", line=dict(color=RED, width=1.2, shape="hv"),
                                 hovertemplate="%{y:.2f}%<extra>기준금리</extra>"))
    st.plotly_chart(base_layout(fig, height=400, unit="%"), width="stretch")
    st.caption("자료: 한국은행 ECOS (금융투자협회 최종호가수익률)")

    st.markdown("<div class='sec'>□ Factor Score</div>", unsafe_allow_html=True)
    st.data_editor(pd.DataFrame({"Factor": FACTORS, "Remarks": [""] * 6, "Score": [""] * 6}),
                   hide_index=True, width="stretch", disabled=["Factor"], key=f"fs_{report_date}",
                   column_config={"Remarks": st.column_config.TextColumn(width="large")})
    st.markdown("<div class='sec'>□ Duration Strategy</div>", unsafe_allow_html=True)
    st.text_area("Duration Strategy", value="", label_visibility="collapsed", key=f"ds_{report_date}")
    st.caption("Factor Score와 전략 코멘트 칸은 비워 둔 상태이며, 입력해도 저장되지 않습니다.")

# ── Term Sector Strategy ───────────────────────────────────────
with tab_t:
    st.markdown("<div class='sec'>□ 금리 및 스프레드</div>", unsafe_allow_html=True)
    ycols = [k for k in ["1Y", "2Y", "3Y", "5Y", "10Y", "30Y"] if k in hist]
    yt = pd.DataFrame({k.lower(): [value_on(hist[k], cmp_), value_on(hist[k], ref)] for k in ycols},
                      index=[f"비교일 {cmp_:%m/%d}", f"기준일 {ref:%m/%d}"])
    ych = (yt.iloc[1] - yt.iloc[0]) * 100
    spreads = {}
    for name, f in TERM_SPREADS.items():
        try:
            spreads[name] = f(bp)
        except KeyError:
            pass
    stt = pd.DataFrame({n: [value_on(s, cmp_), value_on(s, ref)] for n, s in spreads.items()}, index=yt.index)
    sch = stt.iloc[1] - stt.iloc[0]

    c1, c2 = st.columns([6, 7])
    with c1:
        st.markdown("**Yield (%)**")
        out = yt.map(lambda x: f"{x:.3f}")
        out.loc["변동(bp)"] = ych.map(lambda x: f"{x:+.1f}")
        st.dataframe(out, width="stretch")
    with c2:
        st.markdown("**Spread (bp)**")
        out = stt.map(lambda x: f"{x:.1f}")
        out.loc["변동(bp)"] = sch.map(lambda x: f"{x:+.1f}")
        st.dataframe(out, width="stretch")

    st.markdown(f"<div class='sec'>□ Spread Chart · 최근 {n_stats}영업일, ±{z:.2f}σ</div>", unsafe_allow_html=True)
    names = list(spreads)
    for i in range(0, len(names), 2):
        cols = st.columns(2)
        for col, name in zip(cols, names[i:i + 2]):
            with col:
                stt_ = window_stats(spreads[name], n_stats, n_boll, z)
                st.markdown(f"**{name} spread**")
                st.plotly_chart(spread_fig(stt_, show_boll=show_boll, show_band=show_band, ref_date=cmp_),
                                width="stretch", key=f"ts_{name}")
                st.markdown(chart_meta(stt_), unsafe_allow_html=True)

    st.markdown("<div class='sec'>□ Comment</div>", unsafe_allow_html=True)
    st.text_area("Term comment", value="", label_visibility="collapsed", key=f"tc_{report_date}")

# ── Credit Sector Strategy ─────────────────────────────────────
with tab_c:
    cspreads = {}
    for name, f in CREDIT_SPREADS.items():
        try:
            cspreads[name] = f(bp)
        except KeyError:
            pass
    if not cspreads:
        st.info("크레딧 스프레드를 계산할 데이터가 없습니다.")
    else:
        st.markdown("<div class='sec'>□ Credit Spread (bp, 동일 만기 국고 대비)</div>", unsafe_allow_html=True)
        ct = pd.DataFrame({n: [value_on(s, cmp_), value_on(s, ref)] for n, s in cspreads.items()},
                          index=[f"비교일 {cmp_:%m/%d}", f"기준일 {ref:%m/%d}"])
        out = ct.map(lambda x: f"{x:.1f}")
        out.loc["변동(bp)"] = (ct.iloc[1] - ct.iloc[0]).map(lambda x: f"{x:+.1f}")
        st.dataframe(out, width="content")

        cols = st.columns(2)
        for col, (name, s) in zip(cols, cspreads.items()):
            with col:
                stt_ = window_stats(s, n_stats, n_boll, z)
                st.markdown(f"**□ {name} Credit Spread**")
                st.plotly_chart(spread_fig(stt_, area=True, show_boll=show_boll, show_band=show_band, ref_date=cmp_),
                                width="stretch", key=f"cs_{name}")
                st.markdown(chart_meta(stt_), unsafe_allow_html=True)
    st.caption("ECOS 일별 시장금리에는 특수채(산금채 1년 제외)·은행채·여전채가 없고 회사채는 3년 AA-만 있어 이 두 가지만 표시합니다.")

st.divider()
st.caption("데이터: 한국은행 ECOS 시장금리(일별). 금융투자협회 최종호가수익률 기준이라 4사평균 시가평가와 수 bp 차이가 날 수 있습니다. "
           "통계(평균·최대·최소·σ)는 기준일까지 최근 통계 기간의 고정값, 볼린저밴드는 이동 계산(±2σ)입니다.")
