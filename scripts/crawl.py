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
import time
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


NASDAQ_HEADERS = {
    **HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _parse_nasdaq_price(price_str):
    """'23.00-27.00' 같은 밴드는 상단값 사용"""
    if not price_str:
        return None
    try:
        return float(price_str.split("-")[-1].strip().replace("$", "").replace(",", ""))
    except ValueError:
        return None


def _parse_nasdaq_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _crawl_nasdaq_ipo_month(year_month):
    url = f"https://api.nasdaq.com/api/ipo/calendar?date={year_month}"
    results = []
    resp = requests.get(url, headers=NASDAQ_HEADERS, timeout=15)
    payload = resp.json().get("data") or {}

    for section, date_field in (
        ("priced", "pricedDate"),
        ("upcoming", "expectedPriceDate"),
        ("filed", "filedDate"),
    ):
        block = payload.get(section) or {}
        table = block.get("upcomingTable", block)  # upcoming만 한 겹 더 감싸져 있음
        for row in (table.get("rows") or []):
            name = (row.get("companyName") or "").strip()
            if not name:
                continue
            results.append({
                "name": name,
                "ticker": row.get("proposedTickerSymbol") or None,
                "exchange": row.get("proposedExchange") or "TBD",
                "sector": "",
                "ipo_date": _parse_nasdaq_date(row.get(date_field)),
                # 확정가(priced)만 price로 인정 — filed/upcoming의 밴드가격을
                # price로 넣으면 확정 전인데 '프라이싱 완료'로 오탐된다.
                "price": _parse_nasdaq_price(row.get("proposedSharePrice")) if section == "priced" else None,
                "valuation": None,
                "raised": row.get("dollarValueOfSharesOffered"),
                "description": "",
                "source_url": url,
            })
    return results


def crawl_nasdaq_ipo():
    """Nasdaq 공식 IPO 캘린더 API(priced/upcoming/filed).
    StockAnalysis가 놓치는 외국기업 ADR 이중상장(예: SK하이닉스 SKHY)도 잡힌다.
    API가 월 단위 쿼리라 이번 달만 조회하면 매월 1일에 지난달 확정분이
    통째로 빠지므로(예: 7/31에 70건이던 게 8/1엔 3건) 이번 달+지난달을 합쳐서 본다."""
    today = date.today()
    prev_month = (today.replace(day=1) - timedelta(days=1))
    months = {today.strftime("%Y-%m"), prev_month.strftime("%Y-%m")}
    results = []
    try:
        for ym in months:
            results.extend(_crawl_nasdaq_ipo_month(ym))
        print(f"[nasdaq] {len(results)}건 수집 ({', '.join(sorted(months))})", file=sys.stderr)
    except Exception as e:
        print(f"[nasdaq] 오류: {e}", file=sys.stderr)

    return results


PRIVATE_SLUGS = {
    "openai": "openai",
    "anthropic": "anthropic",
    "databricks": "databricks",
    "stripe": "stripe",
    "shein": "shein",
    "plaid": "plaid",
    "monzo": "monzo",
    "consensys": "consensys",
    "kraken": "kraken",
    "canva": "canva",
}

STAT_RE = re.compile(r'label:"([^"]+)",value:"([^"]*)"')


def crawl_private_profile(slug):
    """StockAnalysis.com 비상장기업 프로필(/private/{slug}/)에서
    Valuation / IPO Status / Expected IPO Date를 직접 파싱.
    stockanalysis.com/ipos/calendar/는 근시일 확정 종목만 잡아서
    Canva·Databricks처럼 날짜 미확정인 메가캡은 여기로 갱신해야 함."""
    url = f"https://stockanalysis.com/private/{slug}/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"[private:{slug}] HTTP {resp.status_code}", file=sys.stderr)
            return None
        stats = dict(STAT_RE.findall(resp.text))
        if not stats.get("IPO Status") and not stats.get("Valuation"):
            return None
        return {
            "valuation": stats.get("Valuation"),
            "valuation_date": stats.get("Valuation Date"),
            "ipo_status": stats.get("IPO Status"),
            "expected_ipo_date": stats.get("Expected IPO Date"),
            "source_url": url,
        }
    except Exception as e:
        print(f"[private:{slug}] 오류: {e}", file=sys.stderr)
        return None


def crawl_notable_private():
    """NOTABLE 중 아직 비상장인 워치리스트 종목을 개별 프로필에서 매 실행마다 갱신.
    기존 merge_international은 ticker/price/ipo_date/exchange만 캘린더 매칭 시 갱신했는데,
    날짜 미확정 메가캡은 캘린더에 안 잡혀서 valuation/설명이 최초 입력값에 영구 고정되던 버그 수정용."""
    out = {}
    for key, slug in PRIVATE_SLUGS.items():
        info = crawl_private_profile(slug)
        if info:
            out[key] = info
        time.sleep(0.5)
    print(f"[private] {len(out)}/{len(PRIVATE_SLUGS)}건 갱신", file=sys.stderr)
    return out


def _dedup_by_name(items):
    """동명 종목 중복 시 나중 항목이 우선(nasdaq을 stockanalysis 뒤에 둬서 nasdaq 우선)"""
    out = {}
    for item in items:
        out[item["name"].lower()] = item
    return list(out.values())


MANUAL_CORRECTIONS = {
    # 이미 확정된 사실인데 소스가 오탈자를 냈거나(캘린더 페이지 표기 오류) 상장 후
    # 캘린더에서 빠져 자동갱신 대상에서 벗어난 종목. git checkout/재크롤 등으로
    # existing 데이터가 통째로 리셋돼도 유실되지 않도록 매 실행마다 강제 적용.
    "spacex": {
        "ticker": "SPCX",
        "ipo_date": "2026-06-12",
        "description": "역대 최대 IPO. 2026-06-12 상장 완료(공모가 $135, $85.7B 조달).",
        "source_url": "https://stockanalysis.com/stocks/spcx/",
    },
}


NOTABLE = {
    "openai", "anthropic", "databricks", "spacex", "stripe",
    "klarna", "reddit", "arm", "shein",
    "plaid", "monzo", "consensys", "kraken", "canva",
    "sk hynix",
}

# 봇 자동매수 감시 대상 — pricing 공시 시 alert 생성
WATCHLIST = {"plaid", "monzo", "consensys", "kraken", "canva", "sk hynix"}


def _is_valid_intl(item):
    name = item.get("name", "")
    # 날짜 형태(Jan 12, 2026 등)로 들어온 잘못된 항목 제거
    if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d", name):
        return False
    if re.match(r"^\d{4}-\d{2}-\d{2}", name):
        return False
    return bool(name and len(name) > 1)


def merge_international(existing, fresh, private_info=None):
    """주목 종목 보존 + 신규 데이터 병합. 90일 이상 지난 상장 완료 항목 제거."""
    private_info = private_info or {}
    keep = [i for i in existing if any(n in i["name"].lower() for n in NOTABLE)]
    keep_names = {i["name"].lower() for i in keep}

    # fresh 데이터(캘린더/nasdaq)에서 NOTABLE 종목의 ticker/price/ipo_date/exchange 갱신
    # — 단, 날짜가 임박해 캘린더에 실제로 잡힌 종목에만 해당
    fresh_map = {i["name"].lower(): i for i in fresh}
    for item in keep:
        name_lower = item["name"].lower()
        fresh_item = fresh_map.get(name_lower)
        if fresh_item:
            for field in ("ticker", "price", "ipo_date", "exchange"):
                if fresh_item.get(field) and not item.get(field):
                    item[field] = fresh_item[field]

        # 비상장 프로필(stockanalysis.com/private/*)에서 valuation/상태를 매 실행마다 갱신.
        # 캘린더에 안 잡히는(날짜 미확정) 메가캡이 예전 수동 입력값에 영구 고정되던 문제 수정.
        for key, info in private_info.items():
            if key not in name_lower:
                continue
            if info.get("valuation"):
                item["valuation"] = info["valuation"]
            status = info.get("ipo_status")
            expected = info.get("expected_ipo_date")
            if status or expected:
                item["description"] = (
                    f"IPO Status: {status or 'n/a'} · Expected: {expected or 'n/a'}"
                    f" (valuation as of {info.get('valuation_date') or 'n/a'})"
                )
            if info.get("source_url"):
                item["source_url"] = info["source_url"]
            # 확정 날짜(YYYY-MM-DD)가 아닌 자리표시자 ipo_date는 description과
            # 모순을 일으키므로 제거 — 이번 실행에서 캘린더가 실제 날짜를 새로
            # 채워주지 않는 한 표시하지 않는다(과거 Canva/OpenAI 사례처럼
            # "Expected 2027"인데 ipo_date만 2026-08-15로 남는 모순 방지).
            if not (fresh_item and fresh_item.get("ipo_date")):
                item["ipo_date"] = None
            break

        # 하드 오버라이드 — existing 데이터가 뭐였든 항상 이 값으로 고정
        for key, correction in MANUAL_CORRECTIONS.items():
            if key in name_lower:
                item.update(correction)
                break

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

    # 비-NOTABLE 기존 항목 중 최근(90일 이내) 것도 보존 — 이번 크롤(fresh)에 다시
    # 안 잡혀도 사라지지 않게 함. nasdaq API가 월 단위라 매월 1일에 지난달 확정분이
    # fresh에서 통째로 빠지는 경우(crawl_nasdaq_ipo 참고)의 안전망.
    new_names = {i["name"].lower() for i in new_items}
    preserved = [
        i for i in existing
        if i["name"].lower() not in keep_names
        and i["name"].lower() not in new_names
        and is_recent(i)
        and _is_valid_intl(i)
    ]

    return keep + new_items + preserved


def check_pricing_alerts(existing_intl, merged_intl):
    """WATCHLIST 종목 중 이번 크롤에서 ticker 또는 price가 새로 채워진 것 반환"""
    existing_map = {i["name"].lower(): i for i in existing_intl}
    alerts = []
    for item in merged_intl:
        name_lower = item["name"].lower()
        if not any(w in name_lower for w in WATCHLIST):
            continue
        prev = existing_map.get(name_lower, {})
        newly_priced = item.get("price") and not prev.get("price")
        newly_tickered = item.get("ticker") and not prev.get("ticker")
        if newly_priced or newly_tickered:
            alerts.append(item)
    return alerts


SPAC_NAME_HINT = re.compile(
    r"acquisition\s+corp|acquisition\s+corporation|blank\s+check|special\s+purpose",
    re.IGNORECASE,
)
LARGE_DEAL_THRESHOLD_USD = 1_000_000_000


def _parse_raised(raised_str):
    if not raised_str:
        return None
    try:
        return float(str(raised_str).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def check_large_deal_alerts(existing_intl, merged_intl):
    """WATCHLIST에 없어도 신규 프라이싱 조달금액이 $10억을 넘으면 자동 flag.
    SPAC(공모가 고정 $10, '~Acquisition Corp' 명명)은 제외.
    이미 이전 크롤에서 price가 잡혀 있던 종목은 재알림하지 않는다(SK하이닉스처럼
    WATCHLIST 미등록 상태에서 놓친 대형 ADR 이중상장 등을 다음번엔 자동으로 걸러내기 위함)."""
    existing_map = {i["name"].lower(): i for i in existing_intl}
    alerts = []
    for item in merged_intl:
        name = item["name"]
        if SPAC_NAME_HINT.search(name):
            continue
        price = item.get("price")
        if not price:
            continue
        raised = _parse_raised(item.get("raised"))
        if raised is None or raised < LARGE_DEAL_THRESHOLD_USD:
            continue
        prev = existing_map.get(name.lower(), {})
        if prev.get("price"):
            continue
        alerts.append(item)
    return alerts


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
    intl_fresh = _dedup_by_name(crawl_stockanalysis() + crawl_nasdaq_ipo())
    private_info = crawl_notable_private()
    existing_intl = existing.get("international", [])
    international = merge_international(existing_intl, intl_fresh, private_info)

    # pricing alert 감지 — ① WATCHLIST 등록 종목 ② 미등록이라도 $10억+ 대형 딜(SPAC 제외) 자동 발견
    watchlist_alerts = check_pricing_alerts(existing_intl, international)
    for a in watchlist_alerts:
        a["alert_reason"] = "watchlist"
    watchlist_names = {a["name"].lower() for a in watchlist_alerts}

    large_deal_alerts = [
        a for a in check_large_deal_alerts(existing_intl, international)
        if a["name"].lower() not in watchlist_names
    ]
    for a in large_deal_alerts:
        a["alert_reason"] = "auto_large_deal"

    alerts = watchlist_alerts + large_deal_alerts
    alerts_path = OUTPUT.parent / "pricing_alerts.json"
    if alerts:
        with open(alerts_path, "w", encoding="utf-8") as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)
        print(f"[PRICING_ALERT] {len(alerts)}건: {[a['name'] for a in alerts]}", file=sys.stderr)
    else:
        # 이전 alert 파일 제거 (중복 issue 방지)
        if alerts_path.exists():
            alerts_path.unlink()

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
