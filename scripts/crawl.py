#!/usr/bin/env python3
"""
IPO 데이터 자동 크롤러
- 국내: 38커뮤니케이션 (lxml + curl로 EUC-KR 안정 처리)
- 해외: StockAnalysis.com / 기존 주목 종목 유지
"""

import json
import os
import re
import subprocess
import sys
import tempfile
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
SOURCE_38 = "http://www.38.co.kr/html/fund/?o=k"


def load_existing():
    if OUTPUT.exists():
        with open(OUTPUT, encoding="utf-8") as f:
            return json.load(f)
    return {"domestic": [], "international": []}


def _fetch_euckr(url):
    """curl로 EUC-KR 페이지를 안정적으로 가져옴"""
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as tmp:
        tmppath = tmp.name
    try:
        subprocess.run(
            [
                "curl", "-s", "--max-time", "20",
                "-H", "Accept-Encoding: identity",
                "-H", f'User-Agent: {HEADERS["User-Agent"]}',
                url, "-o", tmppath,
            ],
            check=True,
        )
        with open(tmppath, "rb") as f:
            raw = f.read()
        return raw.decode("euc-kr", errors="replace")
    finally:
        os.unlink(tmppath)


def _infer_market(name):
    if "(유가)" in name:
        return "KOSPI"
    lname = name.lower()
    if "스팩" in name or "spac" in lname:
        return "KOSDAQ"
    return "KOSDAQ"


def _clean_name(name):
    return name.replace("(유가)", "").strip()


def crawl_38comm():
    """38커뮤니케이션 공모주 청약일정 파싱"""
    results = []
    try:
        text = _fetch_euckr(SOURCE_38)
        soup = BeautifulSoup(text, "lxml")

        # 7-컬럼(종목명|청약일정|확정가|희망가|경쟁률|주간사|분석) 구조의 데이터 테이블 탐색
        date_pat = re.compile(r"^\d{4}\.\d{2}\.\d{2}~\d{2}\.\d{2}$")

        for table in soup.find_all("table"):
            data_rows = []
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all("td")]
                if len(cells) == 7 and date_pat.match(cells[1]):
                    data_rows.append(cells)

            if len(data_rows) < 5:
                continue

            for cells in data_rows:
                raw_name   = cells[0]
                date_str   = cells[1]
                offer_str  = cells[2]
                band_str   = cells[3]
                comp_ratio = cells[4] or None

                m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})~(\d{2})\.(\d{2})", date_str)
                if not m:
                    continue
                yr, ms, ds, me, de = m.groups()
                sub_start = f"{yr}-{ms}-{ds}"
                sub_end   = f"{yr}-{me}-{de}"

                results.append({
                    "name": _clean_name(raw_name),
                    "market": _infer_market(raw_name),
                    "sector": "",
                    "subscription_start": sub_start,
                    "subscription_end": sub_end,
                    "refund_date": None,
                    "listing_date": None,
                    "price_band": band_str if band_str and band_str != "-" else None,
                    "offer_price": offer_str if offer_str and offer_str != "-" else None,
                    "competition_ratio": comp_ratio,
                    "source_url": SOURCE_38,
                })
            break  # 첫 번째 데이터 테이블만

        print(f"[38comm] {len(results)}건 수집", file=sys.stderr)
    except Exception as e:
        print(f"[38comm] 오류: {e}", file=sys.stderr)

    return results


def crawl_stockanalysis():
    """StockAnalysis.com IPO 캘린더 — __NEXT_DATA__ 또는 API 시도"""
    url = "https://stockanalysis.com/ipos/calendar/"
    results = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Next.js 데이터 시도
        script = soup.find("script", id="__NEXT_DATA__")
        if script:
            data = json.loads(script.string)
            page_props = data.get("props", {}).get("pageProps", {})
            raw_list = (
                page_props.get("data")
                or page_props.get("ipos")
                or page_props.get("calendar")
                or []
            )
            if isinstance(raw_list, dict):
                raw_list = raw_list.get("data", []) or raw_list.get("ipos", []) or []

            for row in raw_list[:60]:
                name     = row.get("name") or row.get("company") or ""
                ticker   = row.get("symbol") or row.get("ticker") or None
                exchange = (row.get("exchange") or "").upper() or "TBD"
                sector   = row.get("industry") or row.get("sector") or ""
                ipo_date = row.get("ipoDate") or row.get("date") or None
                price    = row.get("ipoPrice") or row.get("price") or None
                if price:
                    try:
                        price = float(str(price).replace("$", "").replace(",", ""))
                    except:
                        price = None
                if not name:
                    continue
                results.append({
                    "name": name,
                    "ticker": ticker,
                    "exchange": exchange,
                    "sector": sector,
                    "ipo_date": ipo_date,
                    "price": price,
                    "valuation": None,
                    "raised": None,
                    "description": "",
                    "source_url": url,
                })

        # HTML 테이블 fallback
        if not results:
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                if len(rows) < 3:
                    continue
                for row in rows[1:]:
                    cells = [td.get_text(strip=True) for td in row.find_all("td")]
                    if len(cells) >= 3 and cells[0]:
                        results.append({
                            "name": cells[0],
                            "ticker": cells[1] if len(cells) > 1 else None,
                            "exchange": cells[2] if len(cells) > 2 else "TBD",
                            "sector": cells[3] if len(cells) > 3 else "",
                            "ipo_date": cells[4] if len(cells) > 4 else None,
                            "price": None,
                            "valuation": None,
                            "raised": None,
                            "description": "",
                            "source_url": url,
                        })
                if results:
                    break

        print(f"[stockanalysis] {len(results)}건 수집", file=sys.stderr)
    except Exception as e:
        print(f"[stockanalysis] 오류: {e}", file=sys.stderr)

    return results


NOTABLE = {
    "openai", "anthropic", "databricks", "spacex", "stripe",
    "klarna", "reddit", "arm", "shein",
}


def _is_valid_intl(item):
    name = item.get("name", "")
    # 날짜 형태(Jan 12, 2026 등)로 들어온 잘못된 항목 제거
    if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d", name):
        return False
    if re.match(r"^\d{4}-\d{2}-\d{2}", name):
        return False
    return bool(name and len(name) > 1)


def merge_international(existing, fresh):
    """주목 종목 보존 + 신규 데이터 병합. 90일 이상 지난 상장 완료 항목 제거."""
    keep = [i for i in existing if any(n in i["name"].lower() for n in NOTABLE)]
    keep_names = {i["name"].lower() for i in keep}

    cutoff = date.today() - timedelta(days=90)

    def is_recent(item):
        d_str = item.get("ipo_date")
        if not d_str:
            return True
        try:
            return date.fromisoformat(str(d_str)[:10]) >= cutoff
        except:
            return True

    new_items = [
        i for i in fresh
        if i["name"].lower() not in keep_names and is_recent(i) and _is_valid_intl(i)
    ]
    return keep + new_items


def main():
    existing = load_existing()

    # 국내
    domestic_fresh = crawl_38comm()
    if domestic_fresh:
        # 기존 sector/listing_date 정보 보존 (수동 입력값)
        name_map = {i["name"]: i for i in existing.get("domestic", [])}
        for item in domestic_fresh:
            prev = name_map.get(item["name"], {})
            item["sector"]       = item["sector"] or prev.get("sector", "")
            item["listing_date"] = item["listing_date"] or prev.get("listing_date")
            item["refund_date"]  = item["refund_date"]  or prev.get("refund_date")
        domestic = domestic_fresh
    else:
        print("[domestic] 크롤링 실패 — 기존 데이터 유지", file=sys.stderr)
        domestic = existing.get("domestic", [])

    # 해외
    intl_fresh = crawl_stockanalysis()
    international = merge_international(existing.get("international", []), intl_fresh)

    from datetime import timezone
    kst = timezone(timedelta(hours=9))
    kst_now = datetime.now(tz=timezone.utc).astimezone(kst)

    output = {
        "updated_at": kst_now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "domestic": domestic,
        "international": international,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"저장 완료: 국내 {len(domestic)}건, 해외 {len(international)}건")


if __name__ == "__main__":
    main()
