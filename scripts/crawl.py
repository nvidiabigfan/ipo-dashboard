#!/usr/bin/env python3
"""
IPO 데이터 자동 크롤러
- 국내: 38커뮤니케이션 공모주 일정
- 해외: StockAnalysis.com IPO Calendar
"""

import json
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}
OUTPUT = Path(__file__).parent.parent / "data" / "ipo_data.json"


def load_existing():
    if OUTPUT.exists():
        with open(OUTPUT, encoding="utf-8") as f:
            return json.load(f)
    return {"domestic": [], "international": []}


def crawl_38comm():
    """38커뮤니케이션 공모주 일정 (http://www.38.co.kr/html/fund/?o=k)"""
    url = "http://www.38.co.kr/html/fund/?o=k"
    results = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "html.parser")

        # 메인 테이블 탐색
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) < 8:
                    continue

                texts = [c.get_text(strip=True) for c in cols]
                name_tag = cols[0].find("a")
                if not name_tag:
                    continue

                name = name_tag.get_text(strip=True)
                if not name or len(name) < 2:
                    continue

                # 시장구분
                market = texts[1] if len(texts) > 1 else ""
                if market not in ("KOSPI", "KOSDAQ", "코스피", "코스닥", "KONEX"):
                    continue
                market = market.replace("코스피", "KOSPI").replace("코스닥", "KOSDAQ")

                # 공모가 밴드 / 확정공모가
                price_band   = texts[3] if len(texts) > 3 else ""
                offer_price  = texts[4] if len(texts) > 4 else ""

                # 청약일정: "06/10~06/11" 형태
                sub_raw = texts[5] if len(texts) > 5 else ""
                sub_start = sub_end = None
                m = re.search(r"(\d{2}/\d{2})[~\-~](\d{2}/\d{2})", sub_raw)
                if m:
                    yr = str(date.today().year)
                    sub_start = f"{yr}-{m.group(1).replace('/', '-')}"
                    sub_end   = f"{yr}-{m.group(2).replace('/', '-')}"

                # 환불일 / 납입일 / 상장일
                refund  = texts[6] if len(texts) > 6 else ""
                listing = texts[8] if len(texts) > 8 else (texts[7] if len(texts) > 7 else "")

                def parse_mmdd(s):
                    m2 = re.search(r"(\d{2})[./](\d{2})", s)
                    if m2:
                        return f"{date.today().year}-{m2.group(1).zfill(2)}-{m2.group(2).zfill(2)}"
                    return None

                item = {
                    "name": name,
                    "market": market,
                    "sector": "",
                    "subscription_start": sub_start,
                    "subscription_end": sub_end,
                    "refund_date": parse_mmdd(refund),
                    "listing_date": parse_mmdd(listing),
                    "price_band": price_band or None,
                    "offer_price": offer_price or None,
                    "source_url": url,
                }
                results.append(item)

        print(f"[38comm] {len(results)}건 수집", file=sys.stderr)
    except Exception as e:
        print(f"[38comm] 오류: {e}", file=sys.stderr)

    return results


def crawl_stockanalysis():
    """StockAnalysis.com IPO Calendar (Next.js __NEXT_DATA__ 파싱)"""
    url = "https://stockanalysis.com/ipos/calendar/"
    results = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            print("[stockanalysis] __NEXT_DATA__ 없음", file=sys.stderr)
            return results

        data = json.loads(script.string)
        # pageProps.data 또는 pageProps.ipos 위치 탐색
        page_props = data.get("props", {}).get("pageProps", {})
        raw_list = (
            page_props.get("data")
            or page_props.get("ipos")
            or page_props.get("calendar")
            or []
        )

        if isinstance(raw_list, dict):
            raw_list = raw_list.get("data", []) or raw_list.get("ipos", []) or []

        for row in raw_list[:40]:
            name     = row.get("name") or row.get("company") or ""
            ticker   = row.get("symbol") or row.get("ticker") or None
            exchange = row.get("exchange") or ""
            sector   = row.get("industry") or row.get("sector") or ""
            ipo_date = row.get("ipoDate") or row.get("date") or None
            price    = row.get("ipoPrice") or row.get("price") or None
            valuation = row.get("marketCap") or None

            if not name:
                continue
            if price:
                try: price = float(str(price).replace("$", "").replace(",", ""))
                except: price = None

            item = {
                "name": name,
                "ticker": ticker,
                "exchange": exchange.upper() if exchange else "TBD",
                "sector": sector,
                "ipo_date": ipo_date,
                "price": price,
                "valuation": f"${valuation:,.0f}M" if isinstance(valuation, (int, float)) else valuation,
                "raised": None,
                "description": "",
                "source_url": url,
            }
            results.append(item)

        print(f"[stockanalysis] {len(results)}건 수집", file=sys.stderr)
    except Exception as e:
        print(f"[stockanalysis] 오류: {e}", file=sys.stderr)

    return results


def merge_international(existing, fresh):
    """기존 주목 종목 보존, 새 데이터로 업데이트"""
    notable_names = {
        "openai", "anthropic", "databricks", "spacex", "stripe",
        "klarna", "reddit", "arm", "shein",
    }
    # 기존 주목 종목은 유지 (수동 관리)
    keep = [
        item for item in existing
        if any(n in item["name"].lower() for n in notable_names)
    ]
    keep_names = {item["name"].lower() for item in keep}

    # fresh 결과에서 중복 제거
    new_items = [item for item in fresh if item["name"].lower() not in keep_names]

    # 최근 90일 + 미래 항목만 포함
    cutoff = date.today() - timedelta(days=90)
    def keep_item(item):
        d_str = item.get("ipo_date")
        if not d_str:
            return True  # 날짜 미정 → 유지
        try:
            d = date.fromisoformat(d_str)
            return d >= cutoff
        except:
            return True

    merged = keep + [i for i in new_items if keep_item(i)]
    return merged


def main():
    existing = load_existing()

    # 국내 크롤링
    domestic_fresh = crawl_38comm()
    if domestic_fresh:
        domestic = domestic_fresh
    else:
        print("[domestic] 크롤링 실패, 기존 데이터 유지", file=sys.stderr)
        domestic = existing.get("domestic", [])

    # 해외 크롤링
    intl_fresh = crawl_stockanalysis()
    international = merge_international(
        existing.get("international", []),
        intl_fresh,
    )

    kst_now = datetime.utcnow() + timedelta(hours=9)
    output = {
        "updated_at": kst_now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "domestic": domestic,
        "international": international,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"저장 완료: {OUTPUT} (국내 {len(domestic)}건, 해외 {len(international)}건)")


if __name__ == "__main__":
    main()
