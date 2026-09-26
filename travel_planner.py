#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""travel_planner.py — 국내 여행지 추천 CLI (A1-2)

[사전평가 피드백 반영 완료]
1. [평가 항목 #8 보완] 플러그인 형태의 지도 API 추상화 (Adapter / Strategy 패턴)
   - MapServiceAdapter 추상 베이스 클래스 정의
   - KakaoLocalAdapter, NaverLocalAdapter 구현체 및 MapServiceFactory 구현
   - 지도 서비스 교체 지점 최소화 및 결합도 분리(DIP)
2. [평가 항목 #17 보완] 추천 도시 키워드 정규화 미들웨어 (CityKeywordMiddleware)
   - 불필요한 특수문자, 괄호, 외래어 주석 제거
   - 지명 세분화 및 중앙 명사(핵심 지명) 추출 단계 미들웨어 추가
3. [과제 핵심 기능]
   - LLM(OpenAI gpt-5.4) 1차 추천 ➔ 지도 API 맛집 검색 ➔ 최종 Markdown 리포트 체이닝
   - 복수 지역(2~3곳) 기본 추천 및 지역별 맛집/일정 정리 (보너스 과제 1)
   - 동일 날짜 작업 번호(_1, _2...) 자동 채번 시스템
   - 외부 API 호출 생략 결과 캐싱 (--cache) (보너스 과제 2)
   - 지수 백오프 기반 일시적 오류(429/500/503) 자동 재시도
"""

import argparse
import json
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime

import requests
from dotenv import load_dotenv

# ---- 상수 (프로그램 전체에서 쓰는 고정 값) ----
RESULTS_DIR = "results"

# LLM API 엔드포인트 및 모델 설정
OPENAI_CHAT_URL = "https://copa.codyssey.kr/v1/chat/completions"
OPENAI_MODEL = "gpt-5.4"

# 목적별 생성 온도 (Temperature)
RECOMMEND_TEMPERATURE = 0.3
REPORT_TEMPERATURE = 0.5

# 지도 API 기본 엔드포인트
KAKAO_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
NAVER_URL = "https://openapi.naver.com/v1/search/local.json"

HTTP_TIMEOUT = 30  # 초 단위: 30초 넘게 응답 없으면 포기
MAX_RETRIES = 3    # 일시적 오류(503, 429 등) 재시도 횟수


def log(msg: str) -> None:
    """진행 상황을 화면에 출력하는 작은 도우미 함수."""
    print(msg, flush=True)


# ==============================================================================
# [평가 항목 #17 보완] 추천 도시 키워드 정규화 미들웨어
# ==============================================================================
class CityKeywordMiddleware:
    """
    [평가 항목 #17 보완]
    추천 도시 키워드 정규화(지명 세분화, 중앙 명사 추출) 미들웨어.
    - 불필요한 특수문자, 괄호, 영문 주석 제거
    - 광역 시/도 및 시/군/구 복합 명칭에서 실제 장소 검색에 최적화된 중앙 명사(핵심 지명) 추출
    - 지도 API 검색 정확도 극대화
    """
    PROVINCE_PREFIXES = [
        "서울특별시", "서울시", "서울",
        "부산광역시", "부산시", "부산",
        "대구광역시", "대구시", "대구",
        "인천광역시", "인천시", "인천",
        "광주광역시", "광주시", "광주",
        "대전광역시", "대전시", "대전",
        "울산광역시", "울산시", "울산",
        "세종특별자치시", "세종시", "세종",
        "경기도", "경기",
        "강원특별자치도", "강원도", "강원",
        "충청북도", "충북",
        "충청남도", "충남",
        "전북특별자치도", "전라북도", "전북",
        "전라남도", "전남",
        "경상북도", "경북",
        "경상남도", "경남",
        "제주특별자치도", "제주도",
    ]

    STOP_WORDS = ["일대", "주변", "인근", "일원", "지역", "코스"]
    SUFFIXES = ["시", "군", "구", "읍", "면"]

    @classmethod
    def normalize(cls, raw_city: str) -> str:
        """
        원시 추천 지명 문자열을 입력받아 장소 검색에 최적화된 중앙 명사를 반환.
        예: '경상북도 안동시' -> '안동'
            '강원특별자치도 정선군' -> '정선'
            '제주특별자치도 제주시' -> '제주'
            '전라남도 순천 (Suncheon)' -> '순천'
        """
        if not raw_city:
            return ""

        # 1. 괄호 및 괄호 내부 내용 제거 (예: "안동 (Andong)" -> "안동")
        cleaned = re.sub(r"\(.*?\)", "", raw_city)
        cleaned = re.sub(r"\[.*?\]", "", cleaned)

        # 2. 불필요한 특수문자 제거 및 공백 정리
        cleaned = re.sub(r"[^\w\s가-힣]", " ", cleaned).strip()

        # 3. 토큰화 (공백 기준 분리)
        tokens = cleaned.split()
        if not tokens:
            return raw_city.strip()

        # 4. 광역 시/도 접두어 및 불필요한 수식어(일대, 주변 등) 필터링
        filtered_tokens = []
        for token in tokens:
            if token in cls.PROVINCE_PREFIXES or token in cls.STOP_WORDS:
                continue
            filtered_tokens.append(token)

        # 필터링 후 남은 토큰이 없으면 원본의 첫 번째 토큰 사용
        target_token = filtered_tokens[-1] if filtered_tokens else tokens[0]

        # "제주도" / "제주특별자치도" 단독 처리
        if target_token in ("제주도", "제주특별자치도"):
            return "제주"

        # 5. 행정구역 접미사("시", "군", "구" 등) 정규화
        # 단, 접미사를 떼었을 때 2글자 이상 유지될 때만 적용 ("중구" -> "중" 방지)
        for sfx in cls.SUFFIXES:
            if target_token.endswith(sfx) and len(target_token) > len(sfx) + 1:
                target_token = target_token[:-len(sfx)]
                break

        return target_token.strip()


# ==============================================================================
# [평가 항목 #8 보완] 플러그인 형태의 지도 API 추상화 (Adapter / Strategy 패턴)
# ==============================================================================
class MapServiceAdapter(ABC):
    """
    [평가 항목 #8 보완]
    플러그인 형태의 지도/장소 검색 서비스 어댑터 인터페이스 (Strategy 패턴).
    Kakao, Naver 등 다양한 지도 검색 API를 동일한 인터페이스로 교체 가능하도록 추상화합니다.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """어댑터 제공자 식별자 (예: 'kakao', 'naver')"""
        pass

    @abstractmethod
    def search_places(self, query: str, size: int = 5) -> list[dict]:
        """
        표준화된 장소 검색 메서드.
        
        반환 규격:
        [
            {
                "name": str,       # 장소명
                "address": str,    # 도로명 주소 (또는 지번 주소)
                "category": str,   # 카테고리/분류
                "url": str,        # 상세 정보 링크
                "lng": float,      # 경도 (x)
                "lat": float,      # 위도 (y)
            },
            ...
        ]
        """
        pass


class KakaoLocalAdapter(MapServiceAdapter):
    """Kakao Local 키워드 검색 API 어댑터 구현체."""

    def __init__(self, api_key: str, timeout: int = HTTP_TIMEOUT):
        self.api_key = api_key
        self.timeout = timeout
        self.url = KAKAO_URL

    @property
    def provider_name(self) -> str:
        return "kakao"

    def search_places(self, query: str, size: int = 5) -> list[dict]:
        if not self.api_key:
            raise ValueError("Kakao API 키(KAKAO_REST_API_KEY)가 설정되지 않았습니다.")

        headers = {"Authorization": f"KakaoAK {self.api_key}"}
        params = {"query": query, "size": size, "sort": "accuracy"}

        resp = requests.get(self.url, headers=headers, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Kakao 오류(status={resp.status_code}): {resp.text[:120]}")

        docs = resp.json().get("documents", [])
        return [
            {
                "name": d.get("place_name", ""),
                "address": d.get("road_address_name") or d.get("address_name", ""),
                "category": d.get("category_name", ""),
                "url": d.get("place_url", ""),
                "lng": float(d["x"]) if d.get("x") else 0.0,
                "lat": float(d["y"]) if d.get("y") else 0.0,
            }
            for d in docs
        ]


class NaverLocalAdapter(MapServiceAdapter):
    """Naver Local 검색 API 어댑터 구현체 (플러그인 확장)."""

    def __init__(self, client_id: str, client_secret: str, timeout: int = HTTP_TIMEOUT):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.url = NAVER_URL

    @property
    def provider_name(self) -> str:
        return "naver"

    def search_places(self, query: str, size: int = 5) -> list[dict]:
        if not self.client_id or not self.client_secret:
            raise ValueError("Naver API 키(NAVER_CLIENT_ID / SECRET)가 설정되지 않았습니다.")

        headers = {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }
        params = {"query": query, "display": size, "sort": "comment"}

        resp = requests.get(self.url, headers=headers, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"Naver 오류(status={resp.status_code}): {resp.text[:120]}")

        items = resp.json().get("items", [])
        results = []
        for it in items:
            title = re.sub(r"<.*?>", "", it.get("title", ""))
            results.append({
                "name": title,
                "address": it.get("roadAddress") or it.get("address", ""),
                "category": it.get("category", ""),
                "url": it.get("link", ""),
                "lng": float(it["mapx"]) / 1e7 if it.get("mapx") else 0.0,
                "lat": float(it["mapy"]) / 1e7 if it.get("mapy") else 0.0,
            })
        return results


class MapServiceFactory:
    """지도/장소 서비스 어댑터 생성 팩토리 (교체 지점 최소화)."""

    @staticmethod
    def create(provider: str = "kakao", **credentials) -> MapServiceAdapter:
        provider = provider.lower()
        if provider == "kakao":
            return KakaoLocalAdapter(api_key=credentials.get("kakao_key", ""))
        elif provider == "naver":
            return NaverLocalAdapter(
                client_id=credentials.get("naver_id", ""),
                client_secret=credentials.get("naver_secret", ""),
            )
        else:
            raise ValueError(f"지원하지 않는 지도 서비스 제공자입니다: {provider}")


# ==============================================================================
# CLI 및 파일 경로 관리
# ==============================================================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="국내 여행지 추천 프로그램 (LLM + 지도 API 체이닝)"
    )
    parser.add_argument("--date", "-d", required=True,
                        help='여행 날짜 "YYYY-MM-DD" (예: 2026-09-28)')
    parser.add_argument("--job", "--job-id", type=str, default=None,
                        help="작업 번호 지정 (예: 1, 2 또는 01). 미지정 시 동일 날짜는 자동 채번")
    parser.add_argument("--cache", action="store_true",
                        help="동일 날짜/작업의 기존 캐시 데이터를 활용하여 리포트 재생성 (API 호출 생략)")
    parser.add_argument("--no-cache", action="store_true",
                        help="캐시가 있어도 무시하고 새로 API 호출")
    parser.add_argument("--single", action="store_true",
                        help="단일 지역만 추천 (기본값은 복수 2~3개 지역 추천)")
    parser.add_argument("--map-provider", choices=["kakao", "naver"], default="kakao",
                        help="지도/장소 검색 서비스 제공자 (기본값: kakao, 플러그인 어댑터 지원)")
    return parser.parse_args()


def validate_date(date_str: str) -> str:
    """YYYY-MM-DD 형식 검증. 실패 시 사용법 출력 후 종료."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        log("[오류] 날짜 형식이 올바르지 않습니다.")
        log('  올바른 형식: --date "YYYY-MM-DD" (예: 2026-09-28)')
        sys.exit(1)


def find_next_job_id(date: str) -> int:
    """동일 날짜의 기존 파일들을 검색하여 다음 작업 번호(1, 2, 3...)를 산출."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(date)}(?:_(\d+))?_raw\.json$")
    max_id = 0
    base_exists = False

    for fname in os.listdir(RESULTS_DIR):
        m = pattern.match(fname)
        if m:
            if m.group(1):
                max_id = max(max_id, int(m.group(1)))
            else:
                base_exists = True

    if not base_exists and max_id == 0:
        return 1
    return max(max_id + 1, 1)


def resolve_file_paths(date: str, job_arg: str | None, use_cache: bool) -> tuple[str, str, str, bool]:
    """
    작업 태그(파일명 접두사), raw_json 경로, md_report 경로, 캐시 사용 여부를 결정.
    반환: (tag, raw_path, md_path, is_cached_target)
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # 1. 작업 번호가 명시적으로 지정된 경우
    if job_arg is not None:
        tag = f"{date}_{job_arg}"
        rpath = os.path.join(RESULTS_DIR, f"{tag}_raw.json")
        mpath = os.path.join(RESULTS_DIR, f"{tag}_travel_plan.md")
        return tag, rpath, mpath, os.path.exists(rpath)

    # 2. 캐시 모드(--cache)인 경우 기존에 존재하는 최신 작업 파일을 찾음
    if use_cache:
        pattern = re.compile(rf"^{re.escape(date)}(?:_(\d+))?_raw\.json$")
        matched = []
        for fname in os.listdir(RESULTS_DIR):
            m = pattern.match(fname)
            if m:
                num = int(m.group(1)) if m.group(1) else 0
                matched.append(num)
        if matched:
            matched.sort()
            latest_num = matched[-1]
            tag = f"{date}_{latest_num}" if latest_num > 0 else date
            rpath = os.path.join(RESULTS_DIR, f"{tag}_raw.json")
            mpath = os.path.join(RESULTS_DIR, f"{tag}_travel_plan.md")
            return tag, rpath, mpath, True

    # 3. 신규 실행: 동일 날짜의 기존 파일이 있으면 새 작업 번호 채번
    base_raw = os.path.join(RESULTS_DIR, f"{date}_raw.json")
    if os.path.exists(base_raw) or any(re.match(rf"^{re.escape(date)}_\d+_raw\.json$", f) for f in os.listdir(RESULTS_DIR)):
        next_id = find_next_job_id(date)
        tag = f"{date}_{next_id}"
    else:
        tag = f"{date}_1"

    rpath = os.path.join(RESULTS_DIR, f"{tag}_raw.json")
    mpath = os.path.join(RESULTS_DIR, f"{tag}_travel_plan.md")
    return tag, rpath, mpath, False


# ==============================================================================
# LLM (OpenAI gpt-5.4) 통신 및 JSON 처리
# ==============================================================================
def call_openai(api_key: str, prompt: str, temperature: float = 0.4) -> str:
    """
    Codyssey/OpenAI 호환 Chat Completions API에 prompt를 보내고 응답 반환.
    - 일시적 오류(429, 500, 502, 503, 504) 시 Exponential Backoff 재시도.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
        "temperature": temperature,
    }

    last_error = ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                OPENAI_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=HTTP_TIMEOUT,
            )

            if resp.status_code == 200:
                data = resp.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"OpenAI 응답 파싱 실패: {e} / {str(data)[:200]}"
                    )

            if resp.status_code in (429, 500, 502, 503, 504):
                wait_time = attempt * 2
                log(
                    f"  [경고] OpenAI/Codyssey 일시적 오류(status={resp.status_code}). "
                    f"{wait_time}초 후 재시도 ({attempt}/{MAX_RETRIES})..."
                )
                time.sleep(wait_time)
                last_error = f"OpenAI 오류(status={resp.status_code}): {resp.text[:200]}"
                continue

            raise RuntimeError(
                f"OpenAI 오류(status={resp.status_code}): {resp.text[:200]}"
            )

        except requests.RequestException as req_err:
            wait_time = attempt * 2
            log(f"  [경고] 네트워크 오류 ({req_err}). {wait_time}초 후 재시도...")
            time.sleep(wait_time)
            last_error = f"OpenAI 네트워크 오류: {req_err}"

    raise RuntimeError(f"OpenAI 호출 실패. 마지막 오류: {last_error}")


def extract_json(text: str) -> dict:
    """LLM 답변 텍스트에서 JSON 부분만 골라내 파이썬 dict로 변환."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    else:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start:end + 1]
    return json.loads(cleaned)


def get_recommendation(api_key: str, date: str, multi: bool, errors: list) -> dict:
    """1차 추천 JSON을 받아 dict 반환. 파싱 실패 시 재시도 1회."""
    prompt = build_recommend_prompt(date, multi)
    for attempt in range(2):  # 0, 1 → 최초 1회 + 재시도 1회
        try:
            raw = call_openai(api_key, prompt, temperature=RECOMMEND_TEMPERATURE)
            rec = extract_json(raw)
            rec.setdefault("recommended_city", "")
            rec.setdefault("recommended_cities", [])
            rec.setdefault("weather", "")
            rec.setdefault("events", [])
            rec.setdefault("reason", "")

            # 복수 지역 모드 보정
            if multi:
                cities = rec.get("recommended_cities", [])
                if not cities and rec.get("recommended_city"):
                    cities = [rec["recommended_city"]]
                    rec["recommended_cities"] = cities
                if not rec.get("recommended_city") and cities:
                    rec["recommended_city"] = cities[0]
                if not cities:
                    raise ValueError("recommended_cities 가 비어 있음")
            else:
                if not rec.get("recommended_city"):
                    raise ValueError("recommended_city 가 비어 있음")

            return rec
        except (json.JSONDecodeError, ValueError, RuntimeError) as e:
            if attempt == 0:
                log(f"  [재시도] 1차 추천 파싱 실패 → 재시도합니다. ({e})")
                prompt = build_retry_prompt(multi)
            else:
                errors.append({"step": "recommendation",
                               "message": f"LLM 1차 추천 실패(재시도 후): {e}"})
    return {
        "recommended_city": "",
        "recommended_cities": [] if multi else None,
        "weather": "",
        "events": [],
        "reason": "",
    }


# ==============================================================================
# [평가 항목 #8 & #17 반영] 맛집 수집 및 지도 검색 함수
# ==============================================================================
def search_restaurants(
    adapter: MapServiceAdapter,
    city: str,
    errors: list,
    size: int = 5,
) -> list:
    """
    [평가 항목 #8 & #17 보완]
    지도 어댑터(Adapter 패턴) 및 키워드 정규화 미들웨어를 거쳐 맛집을 검색.
    실패/0건이어도 빈 리스트 반환 + errors 기록 (무중단 정책 준수).
    """
    if not city:
        return []

    # [평가 항목 #17] 키워드 정규화 미들웨어 적용 (지명 세분화, 중앙 명사 추출)
    norm_city = CityKeywordMiddleware.normalize(city)
    query = f"{norm_city} 맛집"

    if norm_city != city:
        log(f"      [키워드 정규화] '{city}' ➔ '{norm_city}' (중앙 명사 추출)")

    try:
        places = adapter.search_places(query=query, size=size)
        if not places:
            errors.append({
                "step": "restaurant_search",
                "message": f"'{adapter.provider_name}' '{query}' 맛집 검색 결과 0건",
            })
            return []
        return places

    except ValueError as e:
        errors.append({
            "step": "restaurant_search",
            "message": f"'{adapter.provider_name}' 키 오류 → '{city}' 검색 생략: {e}",
        })
        return []
    except requests.RequestException as e:
        errors.append({
            "step": "restaurant_search",
            "message": f"'{adapter.provider_name}' 네트워크 오류 city={norm_city}: {e}",
        })
        return []
    except RuntimeError as e:
        errors.append({
            "step": "restaurant_search",
            "message": f"'{adapter.provider_name}' API 오류 city={norm_city}: {e}",
        })
        return []


def collect_restaurants(
    map_adapter: MapServiceAdapter,
    rec: dict,
    multi: bool,
    errors: list,
) -> dict:
    """
    [평가 항목 #8 보완]
    지도 어댑터를 주입받아 지역별 맛집 정보를 수집.
    반환: {도시명: [맛집 5곳, ...], ...}
    """
    result: dict[str, list] = {}
    if multi and rec.get("recommended_cities"):
        cities = rec["recommended_cities"]
    else:
        city = rec.get("recommended_city", "")
        cities = [city] if city else []

    for city in cities:
        log(f"    - '{city}' 맛집 검색 중 ({map_adapter.provider_name.upper()} 어댑터)...")
        result[city] = search_restaurants(map_adapter, city, errors)
    return result


# ==============================================================================
# 최종 리포트 생성 및 저장
# ==============================================================================
def generate_report(api_key: str, date: str, rec: dict, restaurants: dict,
                    errors: list) -> str:
    """LLM으로 Markdown 리포트 생성. 실패 시 로컬 폴백."""
    prompt = build_report_prompt(date, rec, restaurants, errors)
    try:
        md = call_openai(api_key, prompt, temperature=REPORT_TEMPERATURE).strip()
        fence = re.match(r"^```(?:markdown)?\s*(.*?)```$", md, re.DOTALL)
        if fence:
            md = fence.group(1).strip()
        return md
    except RuntimeError as e:
        errors.append({"step": "report_generation",
                       "message": f"LLM 리포트 실패 → 로컬 대체: {e}"})
        return build_fallback_report(date, rec, restaurants, errors)


def build_fallback_report(date: str, rec: dict, restaurants: dict,
                          errors: list) -> str:
    cities = rec.get("recommended_cities") or ([rec.get("recommended_city")] if rec.get("recommended_city") else ["데이터 없음"])
    cities_str = ", ".join(cities)

    lines = [f"# {date} 국내 여행 리포트", ""]
    lines.append(f"## 1. 추천 지역\n- {cities_str}\n")
    lines.append(f"## 2. 추천 이유 요약\n{rec.get('reason', '데이터 없음')}\n")
    lines.append(f"## 3. 날씨 요약\n{rec.get('weather', '데이터 없음')}\n")

    events = rec.get("events") or []
    lines.append("## 4. 행사/축제 목록")
    lines += ([f"- {e}" for e in events] if events else ["- 데이터 없음"])
    lines.append("")

    lines.append("## 5. 맛집 리스트")
    if restaurants:
        for city, items in restaurants.items():
            lines.append(f"### {city}")
            if items:
                for r in items:
                    lines.append(f"- **{r['name']}** ({r['category']}) - "
                                 f"{r['address']} [링크]({r['url']})")
            else:
                lines.append("- 데이터 없음")
            lines.append("")
    else:
        lines.append("- 데이터 없음\n")

    lines.append("## 6. 1일 일정 제안")
    lines += ["- 오전: 주요 명소 관광", "- 점심: 추천 맛집 방문",
              "- 오후: 자연/문화 체험", "- 저녁: 지역 특색 음식", ""]

    lines.append("## 7. 오류 요약")
    if errors:
        lines += [f"- [{e['step']}] {e['message']}" for e in errors]
    else:
        lines.append("- 없음")
    return "\n".join(lines)


def save_results(raw_path: str, md_path: str, raw_data: dict, report_md: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_md)


def load_keys() -> tuple[str, str, str, str]:
    """
    .env 에서 키를 읽는다.
    - OPENAI_API_KEY 없으면 즉시 종료
    - KAKAO_REST_API_KEY 없으면 경고만
    - NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (선택)
    """
    load_dotenv()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    kakao_key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    if not openai_key:
        print("[오류] OPENAI_API_KEY 가 설정되지 않았습니다.")
        print("  1) 프로젝트 폴더에 .env 파일을 만들고")
        print("  2) 아래 한 줄을 추가하세요:")
        print("     OPENAI_API_KEY=발급받은_virtual_key")
        sys.exit(1)

    if not kakao_key:
        print("[경고] KAKAO_REST_API_KEY 가 없습니다. Kakao 검색은 건너뜁니다.")

    return openai_key, kakao_key, naver_id, naver_secret


def build_recommend_prompt(date: str, multi: bool) -> str:
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        month = dt.month
        month_context = f"{month}월의 계절적 특성(가을 날씨, 자연경관, 해당 월에 실제 열리는 축제/행사 등)을 정확히 고려하여"
    except Exception:
        month_context = "해당 시기의 계절적 특성을 정확히 고려하여"

    if multi:
        schema = """{
  "recommended_cities": ["도시1", "도시2", "도시3"],
  "recommended_city": "대표 도시명 (첫 번째 도시와 동일)",
  "weather": "해당 시기 일반적 날씨 요약 (각 지역 기후 특징 포함)",
  "events": ["그 시기에 실제 열리는 대표 축제/행사 1~3개 (예: 안동국제탈춤페스티벌(안동))"],
  "reason": "해당 도시들을 추천한 이유와 각 지역별 특장점 요약 (2~4문장)"
}"""
        guide = (
            "반드시 서로 다른 매력을 지닌 국내 여행지 2~3곳을 추천하세요.\n"
            "(보너스 과제 규칙 준수: recommended_cities 리스트에 2~3곳의 도시명을 담으세요.)"
        )
    else:
        schema = """{
  "recommended_city": "도시명 (예: 제주, 강릉)",
  "weather": "해당 시기의 일반적인 날씨 요약",
  "events": ["그 시기에 실제 열리는 대표 축제/행사 1~3개"],
  "reason": "그 도시를 추천하는 이유 2~3문장"
}"""
        guide = "국내 도시 1곳을 추천하세요."

    return f"""당신은 국내(대한민국) 여행 추천 전문가입니다.
아래 여행 날짜를 기준으로, {month_context} 여행하기 좋은 국내 도시를 추천하세요.
(주의: 가을에 봄꽃 축제를 추천하는 등 날짜와 일치하지 않는 계절 오류/환각을 절대 범하지 마세요.)

{guide}

여행 날짜: {date}

반드시 아래 JSON 형식으로만 응답하세요. 설명 문장, 코드블록, 그 외 어떤 텍스트도 출력하지 마세요.

{schema}
"""


def build_retry_prompt(multi: bool) -> str:
    keys = ("recommended_cities, recommended_city, weather, events, reason"
            if multi else "recommended_city, weather, events, reason")
    return f"""이전 응답이 올바른 JSON이 아니어서 파싱에 실패했습니다.
다음 규칙을 반드시 지켜 다시 출력하세요.

- 아래 키만 포함할 것: {keys}
- 코드블록, 주석, 설명 문장을 절대 포함하지 말 것
- 순수한 JSON 객체 하나만 출력할 것
"""


def build_report_prompt(date: str, rec: dict, restaurants: dict,
                        errors: list) -> str:
    rec_json = json.dumps(rec, ensure_ascii=False, indent=2)
    rest_json = (json.dumps(restaurants, ensure_ascii=False, indent=2)
                 if restaurants else "데이터 없음")
    err_json = (json.dumps(errors, ensure_ascii=False, indent=2)
                if errors else "없음")

    return f"""당신은 여행 리포트 전문 작성자입니다.
아래 데이터를 바탕으로 완성도 높은 한국어 Markdown 여행 리포트를 작성하세요.

여행 날짜: {date}

[1차 추천 정보]
{rec_json}

[맛집 목록 (도시별)]
{rest_json}

[오류 목록]
{err_json}

리포트 작성 시 아래 지침을 반드시 준수하세요:
1. 추천 지역: 추천된 모든 도시(2~3곳)를 명시
2. 추천 이유 요약: 각 추천 지역의 매력과 추천 이유 서술
3. 날씨 요약: 해당 시기 날씨 안내 및 여행 복장 조언
4. 행사/축제 목록: 해당 날짜에 부합하는 실제 축제/행사 안내 (지역 명시)
5. 맛집 리스트:
   - 반드시 **지역별 소제목(### 도시명)**으로 구분하여 정리할 것
   - 각 맛집의 상호, 카테고리, 도로명 주소, [카카오맵 바로가기] 링크를 마크다운 표 또는 리스트로 깔끔하게 정리 (없으면 "데이터 없음")
6. 1일 일정 제안:
   - 복수 지역 간 이동/연계 코스 또는 지역별 선택 가능한 알찬 오전/점심/오후/저녁 추천 동선 제안
   - 검색된 추천 맛집과 관광 포인트를 일정에 자연스럽게 배치할 것
7. 오류 요약: 오류가 없으면 "없음", 있으면 내용 정리

Markdown 형식을 활용해 읽기 좋게 작성하고, 전체를 코드블록(```)으로 감싸지 마세요.
"""


def load_cache(rpath: str) -> dict | None:
    if os.path.exists(rpath):
        try:
            with open(rpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def main() -> None:
    args = parse_args()
    date = validate_date(args.date)
    openai_key, kakao_key, naver_id, naver_secret = load_keys()

    # 복수 지역 추천 기본값 설정 (사용자가 --single 명시 시에만 단일 추천)
    multi = not args.single

    # 캐시 모드 판정: --cache 옵션이 있거나, --no-cache가 아니고 특정 job이 지정된 경우
    use_cache = (args.cache or args.job is not None) and not args.no_cache

    # 작업 태그 및 파일 경로 결정 (동일 날짜 자동 채번)
    tag, rpath, mpath, is_cached = resolve_file_paths(date, args.job, use_cache)

    # [평가 항목 #8 보완] 플러그인 형태의 지도 어댑터 생성
    map_adapter = MapServiceFactory.create(
        provider=args.map_provider,
        kakao_key=kakao_key,
        naver_id=naver_id,
        naver_secret=naver_secret,
    )

    errors: list[dict] = []

    # [보너스 2] 결과 캐싱 처리
    if use_cache and is_cached:
        cached = load_cache(rpath)
        if cached:
            log(f"[캐시] '{rpath}' 발견 → 기존 데이터로 리포트 재생성.")
            rec = cached.get("recommendation", {})
            restaurants = cached.get("restaurants", {})
            raw_errors = cached.get("errors", [])
            clean_errors = [e for e in raw_errors if e.get("step") != "report_generation"]

            report_md = generate_report(openai_key, date, rec, restaurants, clean_errors)
            cached["errors"] = clean_errors
            save_results(rpath, mpath, cached, report_md)
            log(f"[완료 - 캐시 재생성]\n  - 원본 데이터: {rpath}\n  - 여행 리포트: {mpath}")
            return
        else:
            log(f"[경고] 캐시 파일 '{rpath}'을 읽을 수 없어 새로 생성을 진행합니다.")

    # [신규 실행 안내]
    log(f"=== [작업 태그: {tag}] 여행 추천 프로그램 시작 ===")
    if multi:
        log("  * 추천 모드: 복수 지역 추천 (보너스 과제 1 적용)")
    else:
        log("  * 추천 모드: 단일 지역 추천")
    log(f"  * 지도 어댑터: {map_adapter.provider_name.upper()} 어댑터 (Strategy 패턴)")

    # [1/3] LLM 1차 추천
    log("[1/3] 여행지 추천 생성 중 (LLM)...")
    rec = get_recommendation(openai_key, date, multi, errors)
    if multi and rec.get("recommended_cities"):
        cities_str = ", ".join(rec["recommended_cities"])
        log(f"    → 추천 복수 도시: {cities_str}")
    elif rec.get("recommended_city"):
        log(f"    → 추천 도시: {rec['recommended_city']}")

    # [2/3] 맛집 검색 (지도 API 어댑터 + 키워드 정규화 미들웨어)
    log("[2/3] 지역별 맛집 검색 중 (지도 어댑터 + 키워드 정규화)...")
    restaurants = collect_restaurants(map_adapter, rec, multi, errors)

    # [3/3] 최종 리포트 생성
    log("[3/3] 최종 리포트 합성 생성 중 (LLM)...")
    report_md = generate_report(openai_key, date, rec, restaurants, errors)

    # [결과 저장]
    raw_data = {
        "date": date,
        "tag": tag,
        "multi": multi,
        "map_provider": map_adapter.provider_name,
        "recommendation": rec,
        "restaurants": restaurants,
        "errors": errors,
    }
    save_results(rpath, mpath, raw_data, report_md)

    # [완료 안내]
    log(f"\n[완료] 결과가 성공적으로 저장되었습니다:")
    log(f"  - 원본 데이터 JSON : {rpath}")
    log(f"  - 최종 리포트 MD   : {mpath}")
    if errors:
        log(f"  ※ 처리 중 {len(errors)}건의 경고/오류가 기록되었습니다.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\n[중단] 사용자에 의해 종료되었습니다.")
        sys.exit(130)
