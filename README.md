# 웹 크롤러 사용 가이드

텍스트 파일(`urls.txt`)에 정의된 URL을 시작점으로, 동일 도메인 내 모든 링크를 깊이 제한 없이 탐색하여 개별 마크다운 파일과 통합 `summary.md`를 생성하는 크롤러입니다.

---

## 목차

1. [파일 구성](#1-파일-구성)
2. [환경 설정](#2-환경-설정)
3. [urls.txt 작성법](#3-urlstxt-작성법)
4. [실행 방법](#4-실행-방법)
5. [출력 파일 구조](#5-출력-파일-구조)
6. [동작 원리 상세](#6-동작-원리-상세)
7. [옵션 레퍼런스](#7-옵션-레퍼런스)
8. [자주 발생하는 오류와 해결법](#8-자주-발생하는-오류와-해결법)
9. [실습 예제](#9-실습-예제)

---

## 1. 파일 구성

```
프로젝트 폴더/
├── crawler.py          # 메인 크롤러 프로그램
├── urls.txt            # 크롤링 시작 URL 목록 (직접 편집)
├── requirements.txt    # 의존성 패키지 목록
├── README.md           # 이 문서
└── output/             # 크롤링 결과 저장 위치 (자동 생성)
    ├── summary.md          ← 전체 통합 요약본
    ├── example_com/        ← 도메인별 하위 폴더
    │   ├── index_a1b2.md
    │   └── about_c3d4.md
    └── docs_python_org/
        └── tutorial_e5f6.md
```

---

## 2. 환경 설정

### Python 버전 확인

```bash
python --version
# Python 3.10 이상 권장
```

### 가상 환경 생성 및 패키지 설치

```bash
# 가상 환경 생성
python -m venv venv

# 활성화 (macOS / Linux)
source venv/bin/activate

# 활성화 (Windows)
venv\Scripts\activate

# 패키지 설치
pip install -r requirements.txt
```

설치되는 주요 패키지는 다음과 같습니다.

| 패키지 | 역할 |
| --- | --- |
| `requests` | HTTP/HTTPS 요청 및 자동 리다이렉트 처리 |
| `beautifulsoup4` | HTML 파싱 및 태그 추출 |
| `lxml` | 고성능 HTML 파서 (선택사항) |
| `urllib3` | SSL/TLS 인증서 처리 |

---

## 3. urls.txt 작성법

```text
# 크롤링 대상 URL 목록
# '#'으로 시작하는 줄은 주석으로 처리됩니다.
# 빈 줄은 무시됩니다.

https://example.com
https://docs.python.org/3/tutorial/
# https://paused-site.com  ← 주석 처리로 임시 제외 가능
```

규칙 요약:
- `#` 시작 줄: 주석으로 무시
- 빈 줄: 무시
- `http://` 또는 `https://`로 시작해야 함
- 한 줄에 URL 하나씩 입력

---

## 4. 실행 방법

### 기본 실행 (urls.txt 사용)

```bash
python crawler.py
```

### 특정 URL 파일 지정

```bash
python crawler.py --urls my_targets.txt
```

### 저장 위치 및 딜레이 조정

```bash
python crawler.py --urls urls.txt --output ./docs --delay 1.5
```

---

## 5. 출력 파일 구조

### 개별 마크다운 파일 (URL별)

```markdown
# 페이지 제목

> **출처:** https://example.com/page
> **수집 일시:** 2025-01-15 14:32:10

---

## 본문 헤딩

본문 내용...

```python
# 코드 블록은 언어 태그와 함께 보존
def example():
    return "hello"
```

| 헤더1 | 헤더2 |
| --- | --- |
| 값1 | 값2 |
```

### 통합 요약본 (summary.md)

```markdown
# 크롤링 통합 요약본 (Summary)

> **생성 일시:** 2025-01-15 14:35:00
> **총 수집 성공:** 42건
> **수집 실패:** 2건

---

## 수집 URL 색인

1. [페이지 제목 1](https://example.com)
2. [페이지 제목 2](https://example.com/about)
...

---

## 전체 페이지 내용

<!-- ===== PAGE 1: https://example.com ===== -->
(각 페이지 전체 내용)

---
```

---

## 6. 동작 원리 상세

### BFS (너비 우선 탐색) 알고리즘

```
urls.txt 로드
    ↓
[시작 URL 1]          [시작 URL 2]
  ↓                     ↓
BFS 탐색 시작          BFS 탐색 시작
  ↓
큐에서 URL 꺼내기
  ↓
HTML 수집 (fetch_page)
  ↓
마크다운 변환 (html_to_markdown)
  ↓
개별 .md 파일 저장
  ↓
링크 추출 → 동일 도메인 + 미방문 → 큐에 추가
  ↓
큐가 빌 때까지 반복
  ↓
summary.md 통합 생성
```

### 깊이 무제한 탐색 구조

```python
# crawler.py 핵심 BFS 로직
queue = deque([start_url])      # 큐 초기화
visited = set([start_url])      # 방문 집합

while queue:                    # 큐가 빌 때까지 (깊이 제한 없음)
    url = queue.popleft()
    html, links = process_url(url)

    for link in links:
        if link not in visited and is_same_domain(link):
            visited.add(link)
            queue.append(link)  # 발견된 링크를 모두 큐에 추가
```

### HTTPS 이중 처리 전략

```
요청 시도 (verify=True)
    │
    ├─ 성공 → HTML 반환
    │
    └─ SSLError 발생
          │
          ├─ verify=False 로 재시도
          │       │
          │       ├─ 성공 → HTML 반환 (경고 로그 출력)
          │       │
          │       └─ 실패 → (0, "") 반환 → stats["failed"] += 1
          │                → 다음 URL로 진행 (프로그램 중단 없음)
```

---

## 7. 옵션 레퍼런스

```bash
python crawler.py [--urls FILE] [--output DIR] [--delay SEC] [--timeout SEC]
```

| 옵션 | 단축 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `--urls` | `-u` | `urls.txt` | URL 목록 파일 경로 |
| `--output` | `-o` | `./output` | 결과 저장 디렉터리 |
| `--delay` | - | `1.0` | 요청 간 딜레이 (초) |
| `--timeout` | - | `10` | HTTP 타임아웃 (초) |

---

## 8. 자주 발생하는 오류와 해결법

### ModuleNotFoundError

```bash
# 오류
ModuleNotFoundError: No module named 'requests'

# 해결
pip install -r requirements.txt
```

### FileNotFoundError (urls.txt 없음)

```bash
# 오류
URL 목록 파일을 찾을 수 없습니다: 'urls.txt'

# 해결
# urls.txt 파일을 crawler.py 와 같은 폴더에 생성하거나
python crawler.py --urls /path/to/my_urls.txt
```

### 크롤링이 너무 느릴 때

```bash
# 딜레이를 줄여 속도 향상 (단, 서버 부하 주의)
python crawler.py --delay 0.3
```

### 타임아웃이 잦을 때

```bash
# 타임아웃 값 증가
python crawler.py --timeout 30
```

### summary.md 가 너무 클 때

대규모 사이트를 크롤링하면 summary.md가 수백MB에 달할 수 있습니다.
이 경우 개별 .md 파일만 참조하고, summary.md 생성 부분(`build_summary`)을 비활성화하는 것을 권장합니다.

---

## 9. 실습 예제

### 예제 1: 단일 문서 사이트 수집

```bash
# urls.txt 내용
# https://docs.python.org/3/tutorial/

python crawler.py --output ./python_docs --delay 1.5
```

### 예제 2: 복수 도메인 동시 수집

```bash
# urls.txt 내용
# https://site-a.com
# https://site-b.com

python crawler.py --output ./multi_sites --delay 1.0
```

### 예제 3: 결과 파일 확인

```bash
# 생성된 파일 목록 확인
ls -lh ./output/
ls -lh ./output/example_com/

# 통합 요약본 미리 보기
head -50 ./output/summary.md

# 특정 페이지 확인
cat ./output/example_com/index_a1b2c3d4.md
```

---

## 주의사항

크롤링 전 반드시 대상 사이트의 `robots.txt`와 이용약관을 확인하고,
`--delay` 옵션으로 서버에 과부하가 걸리지 않도록 충분한 요청 간격을 유지합니다.
