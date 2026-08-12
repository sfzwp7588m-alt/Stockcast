from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import html
import json
import math
import os
import re
import time
from urllib.parse import quote
from bs4 import BeautifulSoup
from google import genai
from google.genai import errors
from google.genai import types
from pydantic import BaseModel, Field
import requests
import streamlit as st
from streamlit_browser_storage import LocalStorage
import yfinance as yf

# 🚨 페이지 설정은 반드시 최상단에 위치해야 합니다.
st.set_page_config(
    page_title="StockCast | 주식 기상청", page_icon="🌦️", layout="wide"
)

# 사용자 개인 브라우저 로컬 스토리지 연동
storage = LocalStorage(key="stockcast_db")

# ==========================================
# 1. Pydantic 구조화 출력 스키마
# ==========================================
class StockReason(BaseModel):
    type: str = Field(description="bullish 또는 bearish")
    source_tier: str = Field(
        description="출처 등급 (예: [1순위 DART공시], [1순위 SEC공시], [2순위 1티어경제지속보], [2순위 1티어외신속보], [3순위 증권사리포트], [3순위 월가리포트])"
    )
    tag: str = Field(description="[공시], [실적], [산업], [시황], [수급] 중 하나")
    text: str = Field(description="핵심 요약 한 줄 (한국어로 번역 및 정제)")
    weight_score: int = Field(description="영향도 절대점수 (10 ~ 50 사이의 정수)")
    source_url: str = Field(description="기사 또는 공시의 실제 웹 링크")

class StockAnalysisRawResponse(BaseModel):
    stock_name: str
    stock_code: str
    confidence: str
    summary: str
    upward_prob: int = Field(description="내일 주가 상승 확률 (0~100 사이의 정수, 호재 강도 비례)")
    forecast_comment: str = Field(description="내일 주가 방향성에 대한 AI의 퀀트 예측 코멘트 (1~2문장)")
    reasons: list[StockReason]


# ==========================================
# 2. 개인 로컬 스토리지 & 서버 공통 캐시 관리
# ==========================================
CACHE_FILE = "analysis_cache.json"

# 포트폴리오 로딩 (브라우저 스토리지에서 읽어오기)
if "portfolio" not in st.session_state:
    st.session_state.portfolio = []
    st.session_state.is_loaded_from_browser = False

if not st.session_state.is_loaded_from_browser:
    saved_data = storage.get("portfolio")
    if saved_data is not None:
        try:
            st.session_state.portfolio = json.loads(saved_data)
        except Exception:
            st.session_state.portfolio = []
        st.session_state.is_loaded_from_browser = True

def save_portfolio_data(data: list):
    storage.set("portfolio", json.dumps(data, ensure_ascii=False))
    st.session_state.is_loaded_from_browser = True

# 분석 데이터는 API 절약을 위해 서버 공통 캐시 유지
def load_analysis_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_analysis_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def clear_analysis_cache():
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)


# ==========================================
# 3. API 및 공통 헤더 설정 (st.secrets 적용)
# ==========================================
try:
    DART_API_KEY = st.secrets["DART_API_KEY"]
    NAVER_CLIENT_ID = st.secrets["NAVER_CLIENT_ID"]
    NAVER_CLIENT_SECRET = st.secrets["NAVER_CLIENT_SECRET"]
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
except FileNotFoundError:
    st.error("API 키를 찾을 수 없습니다. '.streamlit/secrets.toml' 파일 설정을 확인하세요.")
    st.stop()

client = genai.Client(api_key=GEMINI_API_KEY)
TIER1_PRESS_KR = ["연합뉴스", "연합인포맥스", "한국경제", "매일경제", "서울경제", "이데일리", "머니투데이"]

SEC_HEADERS = {
    "User-Agent": "StockCast_Forecast dev_quant@sampleinvest.com",
    "Accept-Encoding": "gzip, deflate",
}

def clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", text))

def calculate_time_weight(pub_date: datetime, is_fundamental: bool = False) -> tuple[int, str]:
    now = datetime.now()
    days_ago = max(0, (now - pub_date).total_seconds() / 86400.0)
    half_life = 14.0 if is_fundamental else 2.0
    decay_lambda = math.log(2) / half_life
    decay_weight = math.exp(-decay_lambda * days_ago)
    weight_pct = int(round(decay_weight * 100))
    if days_ago < 1:
        time_str = "오늘"
    elif days_ago < 2:
        time_str = "어제"
    else:
        time_str = f"{int(days_ago)}일 전"
    return weight_pct, time_str


# ==========================================
# 4. 실시간 환율 및 마스터 종목 로딩
# ==========================================
@st.cache_data(ttl=1800)
def get_usd_krw_rate() -> float:
    try:
        usd_krw = yf.Ticker("USDKRW=X")
        rate = usd_krw.fast_info.last_price
        if rate and rate > 500:
            return float(rate)
    except Exception:
        pass
    return 1380.0

@st.cache_data(ttl=86400)
def load_all_krx_stocks() -> dict:
    stocks = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    for sosok in [0, 1]:
        for page in range(1, 20):
            url = f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
            try:
                res = requests.get(url, headers=headers, timeout=5)
                res.encoding = "euc-kr"
                soup = BeautifulSoup(res.text, "html.parser")
                rows = soup.select("table.type_2 tbody tr")
                for row in rows:
                    a_tag = row.select_one("a.tltle")
                    if a_tag:
                        name = a_tag.text.strip()
                        code = a_tag["href"].split("code=")[-1]
                        stocks[f"🇰🇷 {name} ({code})"] = {
                            "name": name,
                            "code": code,
                            "market": "KR",
                        }
            except Exception:
                continue
    return stocks

def search_us_stocks_live(query_text: str) -> list[dict]:
    if not query_text or len(query_text.strip()) < 1:
        return []
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={quote(query_text)}&quotesCount=8&newsCount=0"
    headers = {"User-Agent": "Mozilla/5.0"}
    results = []
    try:
        res = requests.get(url, headers=headers, timeout=4).json()
        for q in res.get("quotes", []):
            ticker = q.get("symbol", "")
            name = q.get("shortname") or q.get("longname") or ticker
            exch = q.get("exchDisp", "")
            if "." not in ticker and exch in ["NYSE", "NASDAQ", "AMEX", "BATS", "Cboe"]:
                results.append({
                    "name": name,
                    "code": ticker,
                    "market": "US",
                    "display": f"🇺🇸 {name} ({ticker}) - {exch}",
                })
    except Exception:
        pass
    return results


# ==========================================
# 5. [한국 주식] 데이터 수집
# ==========================================
@st.cache_data(ttl=300)
def get_kr_current_price(stock_code: str) -> int:
    if not stock_code: return 0
    url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")
        price_elem = soup.select_one("p.no_today span.blind")
        if price_elem:
            return int(price_elem.text.replace(",", ""))
    except Exception:
        pass
    return 0

def fetch_kr_sources(stock_name: str, stock_code: str) -> list:
    all_data = []

    n_url = "https://naverapihub.apigw.ntruss.com/search/v1/news"
    headers = {"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET}
    n_params = {"query": f"{stock_name} ({' | '.join(TIER1_PRESS_KR[:4])})", "display": 8, "sort": "date", "format": "json"}
    try:
        n_res = requests.get(n_url, headers=headers, params=n_params, timeout=5).json()
        seen = set()
        for item in n_res.get("items", []):
            title = clean_html(item.get("title", ""))
            key = re.sub(r"[^가-힣a-zA-Z0-9]", "", title)[:12]
            if key in seen: continue
            seen.add(key)
            
            if any(w in title for w in ["추천주", "리딩방", "급등주", "상한가", "카톡방"]):
                continue

            try:
                pub_dt = parsedate_to_datetime(item.get("pubDate", "")).replace(tzinfo=None)
            except Exception:
                pub_dt = datetime.now()

            time_w, time_str = calculate_time_weight(pub_dt, is_fundamental=False)
            link = item.get("originallink") or item.get("link")
            
            all_data.append({
                "tier": f"[2순위 1티어경제지속보 | {time_str} | 시간가중치 {time_w}%]",
                "title": title,
                "description": clean_html(item.get("description", "")),
                "link": link,
            })
            if len([d for d in all_data if "경제지속보" in d["tier"]]) >= 5: break
    except Exception:
        pass

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    url = "https://opendart.fss.or.kr/api/list.json"
    params = {"crtfc_key": DART_API_KEY, "bgn_de": start_date, "end_de": end_date, "page_count": 3}
    try:
        res = requests.get(url, params=params, timeout=5).json()
        if res.get("status") == "000":
            for item in res.get("list", []):
                if stock_name in item.get("corp_name", ""):
                    r_dt = item.get("rcept_dt", "")
                    pub_dt = datetime.strptime(r_dt, "%Y%m%d") if len(r_dt) == 8 else datetime.now()
                    time_w, time_str = calculate_time_weight(pub_dt, is_fundamental=True)
                    all_data.append({
                        "tier": f"[1순위 DART공시 | {time_str} | 시간가중치 {time_w}%]",
                        "title": item.get("report_nm"),
                        "description": f"제출인: {item.get('flr_nm')} | 접수일자: {r_dt}",
                        "link": f"http://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no')}",
                    })
                    if len([d for d in all_data if "DART공시" in d["tier"]]) >= 2: break
    except Exception:
        pass

    if stock_code:
        try:
            r_url = f"https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode={stock_code}"
            r_res = requests.get(r_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            r_res.encoding = "euc-kr"
            soup = BeautifulSoup(r_res.text, "html.parser")
            for row in soup.select("table.type_1 tr")[:3]:
                cols = row.find_all("td")
                if len(cols) >= 5 and cols[1].find("a"):
                    date_str = cols[4].text.strip()
                    pub_dt = datetime.strptime(date_str, "%y.%m.%d") if len(date_str) == 8 else datetime.now()
                    time_w, time_str = calculate_time_weight(pub_dt, is_fundamental=True)
                    all_data.append({
                        "tier": f"[3순위 증권사리포트 | {time_str} | 시간가중치 {time_w}%]",
                        "title": f"[{cols[2].text.strip()}] {cols[1].find('a').text.strip()}",
                        "description": f"발행일: {date_str} | 증권사: {cols[2].text.strip()}",
                        "link": "https://finance.naver.com/research/" + cols[1].find("a").get("href"),
                    })
                    break
        except Exception:
            pass

    return all_data


# ==========================================
# 6. [미국 주식] 실시간 수집 파이프라인
# ==========================================
def get_sec_cik(ticker: str) -> str:
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        res = requests.get(url, headers=SEC_HEADERS, timeout=5)
        if res.status_code == 200:
            for row in res.json().values():
                if row.get("ticker") == ticker.upper():
                    return str(row.get("cik_str")).zfill(10)
    except Exception:
        pass
    return ""

def fetch_finviz_us_news(ticker: str) -> list:
    news_items = []
    url = f"https://finviz.com/quote.ashx?t={ticker}&p=d"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            news_table = soup.find("table", id="news-table")
            if news_table:
                rows = news_table.find_all("tr")[:8]
                for row in rows:
                    a_tag = row.find("a")
                    td_date = row.find("td")
                    if a_tag and td_date:
                        title = a_tag.text.strip()
                        link = a_tag["href"]
                        date_text = td_date.text.strip()
                        
                        if "Today" in date_text or (":" in date_text and len(date_text) <= 8):
                            pub_dt = datetime.now()
                        else:
                            pub_dt = datetime.now() - timedelta(days=1)
                            
                        time_w, time_str = calculate_time_weight(pub_dt, is_fundamental=False)
                        
                        if link.startswith("http"):
                            news_items.append({
                                "tier": f"[2순위 1티어외신속보 | {time_str} | 시간가중치 {time_w}%]",
                                "title": title,
                                "description": f"배포일시: {date_text}",
                                "link": link,
                            })
                        if len(news_items) >= 5:
                            break
    except Exception:
        pass
    return news_items

def fetch_us_sources(ticker: str, company_name: str = "") -> tuple[list, float]:
    all_data = []
    current_price = 0.0

    finviz_news = fetch_finviz_us_news(ticker)
    all_data.extend(finviz_news)

    cik = get_sec_cik(ticker)
    if cik:
        try:
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            res = requests.get(url, headers=SEC_HEADERS, timeout=5)
            if res.status_code == 200:
                recent = res.json().get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                dates = recent.get("filingDate", [])
                primary_docs = recent.get("primaryDocument", [])
                accession_nos = recent.get("accessionNumber", [])
                for i in range(len(forms)):
                    if forms[i] in ["8-K", "10-Q", "10-K"]:
                        pub_dt = datetime.strptime(dates[i], "%Y-%m-%d")
                        time_w, time_str = calculate_time_weight(pub_dt, is_fundamental=True)
                        acc_clean = accession_nos[i].replace("-", "")
                        link = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{primary_docs[i]}"
                        all_data.append({
                            "tier": f"[1순위 SEC공시({forms[i]}) | {time_str} | 시간가중치 {time_w}%]",
                            "title": f"[{forms[i]}] SEC 공식 법적 공시 제출",
                            "description": f"접수일자: {dates[i]}",
                            "link": link,
                        })
                        if len([d for d in all_data if "SEC공시" in d["tier"]]) >= 2: break
        except Exception:
            pass

    try:
        stock = yf.Ticker(ticker)
        current_price = getattr(stock.fast_info, "last_price", 0.0) or 0.0
        try:
            target_mean = stock.info.get("targetMeanPrice")
            target_high = stock.info.get("targetHighPrice")
            rec = stock.info.get("recommendationKey", "N/A").upper()
            if target_mean:
                all_data.append({
                    "tier": "[3순위 월가리포트 | 최근 종합 | 시간가중치 70%]",
                    "title": f"[Wall Street 컨센서스] 목표주가 평균 ${target_mean:.2f} (최고 ${target_high:.2f}), 투자의견: {rec}",
                    "description": "월가 기관 애널리스트 컨센서스 종합",
                    "link": f"https://finance.yahoo.com/quote/{ticker}/analysis",
                })
        except Exception:
            pass
    except Exception:
        pass

    return all_data, current_price


# ==========================================
# 7. Gemini 분석, 예측 및 수학적 통일 엔진
# ==========================================
def analyze_stock_universal(stock_name: str, stock_code: str, market: str, force_refresh: bool = False) -> dict:
    cache = load_analysis_cache()
    today_str = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{market}_{stock_code}_{today_str}"

    if not force_refresh and cache_key in cache:
        res = cache[cache_key]
        if market == "KR":
            res["current_price"] = get_kr_current_price(stock_code)
        else:
            _, cur_p = fetch_us_sources(stock_code, stock_name)
            res["current_price_usd"] = cur_p
        return res

    if market == "KR":
        data = fetch_kr_sources(stock_name, stock_code)
        current_price = get_kr_current_price(stock_code)
        current_price_usd = 0.0
    else:
        data, current_price_usd = fetch_us_sources(stock_code, stock_name)
        current_price = 0

    formatted_text = "".join(
        [f"- {d['tier']}\n  제목: {d['title']}\n  내용: {d['description']}\n  링크: {d['link']}\n\n" for d in data]
    )

    prompt = f"""
당신은 글로벌 퀀트 금융 분석가입니다. [{stock_name} ({stock_code}, 시장: {market})]의 1·2·3순위 데이터를 종합 분석하여 추세를 확률적으로 예측하세요.

[분석 및 예측 원칙]
1. 최신 속보 뉴스(시간가중치 70~100%) 비중을 높게 반영하세요.
2. 각 요인의 'weight_score'는 중요도에 따라 10~50 정수를 부여하세요.
3. upward_prob (상승 확률): 호재 강도에 비례하여 0~100 사이의 수치로 추산하세요. (예: 강력한 매수 모멘텀시 75 이상)
4. forecast_comment: 상승/하락 확률을 뒷받침하는 짧고 전문적인 AI 코멘트를 작성하세요.
5. source_tier와 source_url을 정확히 매핑하고 한국어로 자연스럽게 정제하세요.

[데이터]
{formatted_text}
"""
    models_to_try = ["gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-3.5-flash"]
    response = None

    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=StockAnalysisRawResponse,
                ),
            )
            if response and response.text:
                break
        except errors.ClientError as e:
            if "429" in str(e):
                time.sleep(2)
                continue
            else:
                raise e

    if not response or not response.text:
        return {
            "stock_name": stock_name,
            "stock_code": stock_code,
            "market": market,
            "weather": "⛅ 구름조금",
            "bullish_pct": 50,
            "bearish_pct": 50,
            "confidence": "대기중",
            "summary": "API 호출 제한으로 잠시 후 새로고침 시 분석이 완료됩니다.",
            "upward_prob": 50,
            "forecast_comment": "데이터 수집 대기 중입니다.",
            "reasons": [],
            "current_price": current_price,
            "current_price_usd": current_price_usd,
        }

    raw_json = json.loads(response.text)
    
    bullish_sum = 0
    bearish_sum = 0
    formatted_reasons = []

    for r in raw_json.get("reasons", []):
        score = abs(r.get("weight_score", 20))
        if r["type"] == "bullish":
            bullish_sum += score
            sign = "+"
        else:
            bearish_sum += score
            sign = "-"
        
        formatted_reasons.append({
            "type": r["type"],
            "source_tier": r["source_tier"],
            "tag": r["tag"],
            "text": r["text"],
            "weight": f"{sign}{score}%",
            "source_url": r["source_url"],
        })

    total_score = bullish_sum + bearish_sum
    if total_score > 0:
        bullish_pct = int(round((bullish_sum / total_score) * 100))
    else:
        bullish_pct = 50
    bearish_pct = 100 - bullish_pct

    if bullish_pct >= 70:
        weather = "☀️ 맑음"
    elif bullish_pct >= 45:
        weather = "⛅ 구름조금"
    elif bullish_pct >= 30:
        weather = "☁️ 흐림"
    else:
        weather = "🌧️ 비"

    res_json = {
        "stock_name": raw_json["stock_name"],
        "stock_code": raw_json["stock_code"],
        "market": market,
        "weather": weather,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "confidence": raw_json["confidence"],
        "summary": raw_json["summary"],
        "upward_prob": raw_json.get("upward_prob", 50),
        "forecast_comment": raw_json.get("forecast_comment", ""),
        "reasons": formatted_reasons,
        "current_price": current_price,
        "current_price_usd": current_price_usd,
    }

    cache[cache_key] = res_json
    save_analysis_cache(cache)
    return res_json


# ==========================================
# 8. Streamlit UI 대시보드
# ==========================================
# 타이틀
st.title("🌦️ StockCast (스톡캐스트)")
st.caption("글로벌 감성 지수 분석 및 다음 날 주가 추세 예측 시스템")

usd_rate = get_usd_krw_rate()
krx_map = load_all_krx_stocks()
krx_options = list(krx_map.keys())

# --- 사이드바 ---
with st.sidebar:
    st.header("🔍 글로벌 종목 검색")
    st.caption(f"💵 실시간 적용 환율: **{usd_rate:,.1f} 원/USD**")

    search_tab_kr, search_tab_us = st.tabs(["🇰🇷 한국", "🇺🇸 미국(전종목)"])

    with search_tab_kr:
        sel_kr = st.selectbox(
            "국내 종목 검색",
            options=krx_options,
            index=None,
            placeholder="예: 삼성전자, SK하이닉스...",
        )
        if st.button("➕ 한국 종목 추가", use_container_width=True):
            if sel_kr:
                info = krx_map[sel_kr]
                if not any(item.get("code") == info["code"] for item in st.session_state.portfolio):
                    st.session_state.portfolio.append({
                        "name": info["name"],
                        "code": info["code"],
                        "market": "KR",
                        "is_holding": False,
                        "quantity": 0,
                    })
                    save_portfolio_data(st.session_state.portfolio)
                    st.rerun()

    with search_tab_us:
        us_query = st.text_input("회사명 또는 티커 (엔터)", placeholder="예: Palantir, NVDA").strip()
        if us_query:
            search_results = search_us_stocks_live(us_query)
            if search_results:
                sel_us = st.selectbox(
                    "검색 결과 선택",
                    options=[r["display"] for r in search_results],
                    index=0,
                )
                if st.button("➕ 미국 종목 추가", use_container_width=True):
                    chosen = next(r for r in search_results if r["display"] == sel_us)
                    if not any(item.get("code") == chosen["code"] for item in st.session_state.portfolio):
                        st.session_state.portfolio.append({
                            "name": chosen["name"],
                            "code": chosen["code"],
                            "market": "US",
                            "is_holding": False,
                            "quantity": 0,
                        })
                        save_portfolio_data(st.session_state.portfolio)
                        st.rerun()

    st.markdown("---")
    if st.button(
        "🔄 실시간 최신 데이터 재분석",
        use_container_width=True,
    ):
        clear_analysis_cache()
        st.cache_data.clear()
        st.toast("최신 데이터로 예측 모델을 다시 돌립니다!", icon="🔄")
        st.rerun()

    st.markdown("---")
    st.subheader("📋 등록된 종목")
    for idx, item in enumerate(st.session_state.portfolio):
        c_label, c_del = st.columns([4, 1])
        flag = "🇰🇷" if item.get("market") == "KR" else "🇺🇸"
        tag = f"보유({item.get('quantity', 0)}주)" if item.get("is_holding", False) else "관심"
        c_label.write(f"{flag} **{item.get('name', '')}** <small>({tag})</small>", unsafe_allow_html=True)
        if c_del.button("❌", key=f"del_{idx}"):
            st.session_state.portfolio.pop(idx)
            save_portfolio_data(st.session_state.portfolio)
            st.rerun()
            
    # 사이드바 하단 면책 조항
    st.markdown("---")
    with st.expander("⚠️ 투자 유의사항 (Disclaimer)"):
        st.caption(
            "본 대시보드(StockCast)에서 제공하는 감성 지수 및 내일의 주가 상승 확률은 "
            "AI가 뉴스와 공시를 퀀트 기법으로 정규화하여 산출한 **추세적 참고 정보**입니다.\n\n"
            "**절대적인 투자 지표가 아니며**, 돌발 변수에 따라 언제든 달라질 수 있습니다. "
            "투자 의사결정의 보조 도구로만 활용하시기 바라며, 최종 투자의 책임은 본인에게 있습니다."
        )


# --- 데이터 분석 파이프라인 ---
analyzed_stocks = []
total_eval_amount_krw = 0

if st.session_state.portfolio:
    with st.spinner("🌍 글로벌 데이터를 분석 중입니다... (💡 최신 정보 갱신은 좌측 '재분석' 버튼 클릭 | ※ 예측은 보조 참고용입니다)"):
        for item in st.session_state.portfolio:
            name = item.get("name", "")
            code = item.get("code", "")
            market = item.get("market", "KR")
            is_holding = item.get("is_holding", False)
            qty = item.get("quantity", 0)

            analysis = analyze_stock_universal(name, code, market)
            
            if market == "KR":
                p_krw = analysis.get("current_price", 0)
                eval_amount_krw = p_krw * qty if is_holding else 0
            else:
                p_usd = analysis.get("current_price_usd", 0.0)
                eval_amount_krw = int(p_usd * qty * usd_rate) if is_holding else 0

            analysis["is_holding"] = is_holding
            analysis["quantity"] = qty
            analysis["eval_amount_krw"] = eval_amount_krw
            analysis["market"] = market

            analyzed_stocks.append(analysis)
            if is_holding and qty > 0:
                total_eval_amount_krw += eval_amount_krw

weighted_bullish = 0.0
holding_stocks = [s for s in analyzed_stocks if s.get("is_holding") and s.get("quantity", 0) > 0]
watchlist_stocks = [s for s in analyzed_stocks if not (s.get("is_holding") and s.get("quantity", 0) > 0)]

if total_eval_amount_krw > 0:
    for stock in holding_stocks:
        weight = stock["eval_amount_krw"] / total_eval_amount_krw
        stock["weight_pct"] = round(weight * 100, 1)
        weighted_bullish += stock["bullish_pct"] * weight
else:
    weighted_bullish = 50.0

weighted_bearish = 100.0 - weighted_bullish

if weighted_bullish >= 70:
    port_weather, weather_icon = "맑음", "☀️"
elif weighted_bullish >= 45:
    port_weather, weather_icon = "구름조금", "⛅"
elif weighted_bullish >= 30:
    port_weather, weather_icon = "흐림", "☁️"
else:
    port_weather, weather_icon = "비", "🌧️"


# --- 1. 상단 글로벌 종합 브리핑 ---
st.markdown("### 🌟 StockCast 포트폴리오 요약")
if holding_stocks:
    p_col1, p_col2, p_col3 = st.columns([1.5, 2, 2.5])
    with p_col1:
        st.metric("총 보유 평가자산", f"{total_eval_amount_krw:,} 원")
    with p_col2:
        st.metric("통합 날씨", f"{weather_icon} {port_weather}", delta=f"호재 {weighted_bullish:.1f}%")
    with p_col3:
        st.write("**통합 감성 지수**")
        st.progress(int(weighted_bullish), text=f"호재 {weighted_bullish:.1f}% | 악재 {weighted_bearish:.1f}%")
else:
    st.info("💡 현재 보유 중으로 설정된 종목이 없습니다. 아래 종목 카드에서 스위치를 켜고 저장 버튼을 눌러주세요.")

st.markdown("---")

# --- 2. 종목 카드 렌더링 (st.form 내부) ---
if st.session_state.portfolio:
    with st.form("portfolio_editor_form", clear_on_submit=False):
        form_inputs = {}

        def render_form_card(stock: dict, orig_idx: int):
            market = stock.get("market", "KR")
            flag = "🇰🇷" if market == "KR" else "🇺🇸"

            if market == "KR":
                p_val = stock.get("current_price", 0)
                price_display = f"{p_val:,} 원" if p_val > 0 else "조회중"
            else:
                p_usd = stock.get("current_price_usd", 0.0)
                p_krw = int(p_usd * usd_rate)
                price_display = f"${p_usd:,.2f} ({p_krw:,}원)" if p_usd > 0 else "조회중"

            prob = stock.get("upward_prob", 50)
            if prob >= 70: wind = "📈 강한 매수풍 (상승 기류)"
            elif prob >= 55: wind = "↗️ 온화한 매수풍 (강보합)"
            elif prob >= 45: wind = "↔️ 횡보 기류 (중립/관망)"
            elif prob >= 30: wind = "↘️ 약한 매도풍 (약보합)"
            else: wind = "📉 강한 매도풍 (하락 기류)"

            with st.container(border=True):
                top_c1, top_c2, top_c3 = st.columns([3.2, 1.8, 2])
                with top_c1:
                    st.markdown(f"### {flag} {stock['stock_name']} <small style='color:gray;'>{stock['stock_code']}</small>", unsafe_allow_html=True)
                    st.caption(f"현재가: **{price_display}**")

                with top_c2:
                    is_held = st.checkbox("📦 실제 보유 중", value=stock.get("is_holding", False), key=f"chk_hold_{orig_idx}")

                with top_c3:
                    qty = st.number_input("수량 (주)", min_value=0, value=stock.get("quantity", 0), step=1, key=f"num_qty_{orig_idx}")

                form_inputs[orig_idx] = {"is_holding": is_held, "quantity": qty}

                m_c1, m_c2 = st.columns([2.5, 4.5])
                with m_c1:
                    st.markdown(f"#### {stock['weather']}")
                    if stock.get("is_holding") and stock.get("quantity", 0) > 0 and total_eval_amount_krw > 0:
                        st.caption(f"평가금: **{stock['eval_amount_krw']:,}원** (비중 **{stock.get('weight_pct', 0)}%**)")

                with m_c2:
                    st.write(f"**현재 감성 지수:** 호재 {stock['bullish_pct']}% / 악재 {stock['bearish_pct']}%")
                    st.progress(stock["bullish_pct"])

                st.info(f"💡 **한줄 요약:** {stock['summary']}")

                with st.expander("🔮 내일의 주가 방향성 예측 (StockCast Forecast)", expanded=False):
                    f_c1, f_c2 = st.columns([1, 2])
                    with f_c1:
                        st.metric("내일 상승 확률", f"{prob}%")
                        st.write(f"**예상 풍향:** {wind}")
                    with f_c2:
                        st.write("**AI 퀀트 코멘트:**")
                        st.write(f"> {stock.get('forecast_comment', '데이터 분석 중입니다.')}")

                with st.expander("🔍 세부 분석 요인 및 원문 링크"):
                    st.caption(f"신뢰도 수준: **{stock.get('confidence', '보통')}**")
                    for r in stock.get("reasons", []):
                        icon = "🟢" if r["type"] == "bullish" else "🔴"
                        tier_badge = r.get("source_tier", "")
                        st.markdown(f"{icon} `{tier_badge}` **{r['tag']}** {r['text']} `({r['weight']})`")
                        st.caption(f"🔗 [원문 기사/공시 바로가기]({r['source_url']})")

        st.subheader("📦 내 보유 종목")
        if holding_stocks:
            for stock in holding_stocks:
                orig_idx = next(i for i, item in enumerate(st.session_state.portfolio) if item["code"] == stock["stock_code"])
                render_form_card(stock, orig_idx)
        else:
            st.caption("보유 중인 종목이 없습니다.")

        st.markdown("---")
        st.subheader("⭐ 관심 종목 (즐겨찾기)")
        if watchlist_stocks:
            for stock in watchlist_stocks:
                orig_idx = next(i for i, item in enumerate(st.session_state.portfolio) if item["code"] == stock["stock_code"])
                render_form_card(stock, orig_idx)

        st.markdown("---")
        submitted = st.form_submit_button("💾 설정 저장 & 반영", use_container_width=True, type="primary")
        if submitted:
            has_changed = False
            for idx, chg in form_inputs.items():
                if (st.session_state.portfolio[idx].get("is_holding") != chg["is_holding"] or
                    st.session_state.portfolio[idx].get("quantity") != chg["quantity"]):
                    st.session_state.portfolio[idx]["is_holding"] = chg["is_holding"]
                    st.session_state.portfolio[idx]["quantity"] = chg["quantity"]
                    has_changed = True
            if has_changed:
                save_portfolio_data(st.session_state.portfolio)
                st.toast("반영 완료!", icon="✅")
                st.rerun()
            else:
                st.toast("변경된 설정이 없습니다.", icon="ℹ️")
else:
    st.write("등록된 종목이 없습니다. 좌측 사이드바에서 추가해 주세요.")
