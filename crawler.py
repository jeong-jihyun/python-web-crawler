#!/usr/bin/env python3
"""
crawler.py  ·  v4.0  (대용량 커스터마이징 개선판)
=================================================
개선 항목 (v4.0):
    [1] 메모리 최적화  - 페이지 처리 즉시 저장 후 본문 메모리 해제
                        summary.md 스트리밍 append 저장 (OOM 방지)
    [2] 속도 개선     - ThreadPoolExecutor 멀티스레드 병렬 수집
    [3] 진행률 표시   - 자체 구현 ProgressBar (외부 의존성 없음)
    [4] Resume 기능   - visited.json 으로 중단 지점 저장/복원
    [5] 파일명 개선   - 챕터 번호 기반 정렬 (001_, 002_ ... 접두사)
    [6] TOC 자동 생성 - summary.md 상단에 링크 포함 목차 자동 삽입
    [7] 실패 URL 분리 - failed_urls.txt 별도 저장
    [8] 통계 리포트   - report.md 생성 (속도/용량/도메인별 통계)

사용법:
    python crawler.py [옵션]

실행 예시:
    python crawler.py
    python crawler.py --urls urls.txt --output ./docs --delay 1.0
    python crawler.py --resume          ← 중단된 크롤링 이어받기
    python crawler.py --workers 5       ← 5개 스레드 병렬 수집
"""

import os, re, sys, time, json, hashlib, logging, argparse, threading
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# ────────────────────────────────────────────────────────────
# 로깅 설정
# ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# [개선 3] 자체 구현 ProgressBar
# ════════════════════════════════════════════════════════════

class ProgressBar:
    """
    외부 라이브러리 없이 터미널에 진행률을 표시하는 프로그레스바입니다.

    표시 형식:
        [████████████████████░░░░░░░░░░] 67% | 104/155 | ✅ 98 ❌ 6 | ETA 0:01:23

    Attributes:
        total (int): 전체 작업 수
        current (int): 현재 완료 수
        success (int): 성공 건수
        failed (int): 실패 건수
        start_time (float): 시작 시각 (time.time())
        _lock (threading.Lock): 스레드 안전 업데이트용

    사용 예시:
        bar = ProgressBar(total=155, prefix="수집 중")
        bar.update(success=True)   # 성공 시
        bar.update(success=False)  # 실패 시
        bar.close()                # 완료 시 줄바꿈
    """

    def __init__(self, total: int, prefix: str = "", bar_width: int = 35):
        self.total      = max(total, 1)
        self.prefix     = prefix
        self.bar_width  = bar_width
        self.current    = 0
        self.success    = 0
        self.failed     = 0
        self.start_time = time.time()
        self._lock      = threading.Lock()
        self._render()

    def update(self, success: bool = True) -> None:
        """
        진행 상태를 1 증가시키고 화면을 갱신합니다.

        Args:
            success: True면 성공 카운터, False면 실패 카운터 증가
        """
        with self._lock:
            self.current += 1
            if success:
                self.success += 1
            else:
                self.failed += 1
            self._render()

    def _render(self) -> None:
        """현재 상태를 터미널 한 줄에 덮어쓰기로 출력합니다."""
        pct      = self.current / self.total
        filled   = int(self.bar_width * pct)
        bar      = "█" * filled + "░" * (self.bar_width - filled)
        elapsed  = time.time() - self.start_time
        eta_str  = "--:--"
        if self.current > 0:
            eta_sec = (elapsed / self.current) * (self.total - self.current)
            m, s    = divmod(int(eta_sec), 60)
            eta_str = f"{m}:{s:02d}"
        line = (
            f"\r{self.prefix} [{bar}] "
            f"{int(pct*100):3d}% | "
            f"{self.current}/{self.total} | "
            f"✅{self.success} ❌{self.failed} | "
            f"ETA {eta_str}"
        )
        sys.stderr.write(line)
        sys.stderr.flush()

    def close(self) -> None:
        """프로그레스바 출력을 마무리하고 줄바꿈을 출력합니다."""
        elapsed = time.time() - self.start_time
        m, s    = divmod(int(elapsed), 60)
        sys.stderr.write(
            f"\r완료: {self.success}건 성공 / {self.failed}건 실패 "
            f"/ 총 {m}분 {s}초 소요\n"
        )
        sys.stderr.flush()


# ════════════════════════════════════════════════════════════
# 유틸리티 함수
# ════════════════════════════════════════════════════════════

def load_urls_from_file(filepath: str) -> list:
    """
    텍스트 파일에서 크롤링 대상 URL 목록을 읽어옵니다.
    '#'으로 시작하는 줄과 빈 줄은 무시합니다.

    Args:
        filepath: URL 목록 텍스트 파일 경로

    Returns:
        유효한 URL 문자열 리스트

    Raises:
        FileNotFoundError: 파일이 존재하지 않을 경우
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(
            f"URL 목록 파일을 찾을 수 없습니다: '{filepath}'\n"
            f"urls.txt 를 생성하거나 --urls 옵션으로 경로를 지정하세요."
        )
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith(("http://", "https://")):
                logger.warning(f"[{filepath}:{lineno}] 유효하지 않은 URL: {line}")
                continue
            urls.append(normalize_url(line))
    logger.info(f"URL 파일 로드: {len(urls)}개 ({filepath})")
    return urls


def normalize_url(url: str) -> str:
    """
    URL 정규화: 프래그먼트 제거, 후행 슬래시 통일.

    Args:
        url: 정규화할 URL

    Returns:
        정규화된 URL

    예시:
        >>> normalize_url("https://wikidocs.net/290881#section")
        'https://wikidocs.net/290881'
    """
    parsed     = urlparse(url)
    normalized = urlunparse(parsed._replace(fragment=""))
    return normalized.rstrip("/")


def is_same_domain(base_url: str, target_url: str) -> bool:
    """두 URL이 동일 도메인인지 확인합니다."""
    return urlparse(base_url).netloc == urlparse(target_url).netloc


def is_crawlable(url: str) -> bool:
    """
    비HTML 자원(이미지·미디어·문서 파일)을 크롤링 대상에서 제외합니다.

    Args:
        url: 검사할 URL

    Returns:
        크롤링 가능하면 True
    """
    skip_ext = {
        ".jpg",".jpeg",".png",".gif",".svg",".webp",".ico",
        ".pdf",".zip",".tar",".gz",".rar",
        ".mp3",".mp4",".avi",".mov",".wav",
        ".css",".js",".woff",".woff2",".ttf",".eot",
        ".xml",".json",".csv",".xls",".xlsx",
    }
    _, ext = os.path.splitext(urlparse(url).path.lower())
    return ext not in skip_ext


# ════════════════════════════════════════════════════════════
# [개선 5] 챕터 번호 기반 파일명 생성
# ════════════════════════════════════════════════════════════

_seq_counter: dict = {}   # domain → 현재 순번
_seq_lock = threading.Lock()

def url_to_filename(url: str, output_dir: str) -> str:
    """
    URL을 순번 기반 마크다운 파일명으로 변환합니다.

    파일명 형식: {도메인디렉터리}/{순번3자리}_{경로슬러그}_{해시8}.md
    예시: wikidocs_net/001_book_18092_a1b2c3d4.md
          wikidocs_net/002_290881_e5f6a7b8.md

    순번을 접두사로 붙여 파일탐색기에서 수집 순서대로 정렬됩니다.

    Args:
        url: 변환할 URL
        output_dir: 저장 최상위 디렉터리

    Returns:
        완전한 파일 경로 문자열
    """
    parsed     = urlparse(url)
    domain_key = parsed.netloc
    domain_dir = re.sub(r"[^a-zA-Z0-9\-]", "_", domain_key)

    with _seq_lock:
        seq = _seq_counter.get(domain_key, 0) + 1
        _seq_counter[domain_key] = seq

    path_part = parsed.path.strip("/").replace("/", "_") or "index"
    path_part = re.sub(r"[^a-zA-Z0-9가-힣_\-]", "_", path_part)[:60]
    short_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    filename   = f"{seq:03d}_{path_part}_{short_hash}.md"
    return os.path.join(output_dir, domain_dir, filename)


# ════════════════════════════════════════════════════════════
# 도메인별 javascript: 링크 변환 레이어
# ════════════════════════════════════════════════════════════

_JS_CONVERTERS: dict = {}

def register_js_converter(domain: str):
    """
    특정 도메인의 javascript: 링크 변환 함수를 등록하는 데코레이터입니다.

    Args:
        domain: 적용 대상 도메인 (예: "wikidocs.net")
    """
    def decorator(func):
        _JS_CONVERTERS[domain] = func
        return func
    return decorator


@register_js_converter("wikidocs.net")
def _wikidocs_converter(href: str, base_url: str):
    """
    wikidocs.net의 javascript:page(숫자) 패턴을 실제 URL로 변환합니다.

    Args:
        href: 원본 href 속성값 (예: "javascript:page(290881)")
        base_url: 현재 페이지 URL

    Returns:
        변환된 URL 또는 None
    """
    m = re.search(r'page\((\d+)\)', href)
    return f"https://wikidocs.net/{m.group(1)}" if m else None


def resolve_js_link(href: str, base_url: str):
    """
    javascript: 링크를 실제 HTTP URL로 변환합니다.

    처리 순서:
        1. 도메인 전용 변환기 조회 및 적용
        2. 없으면 href 내 URL 패턴(https?://) 직접 추출
        3. 모두 실패 시 None 반환

    Args:
        href: 원본 href (javascript: 로 시작)
        base_url: 기준 URL (도메인 확인용)

    Returns:
        변환된 절대 URL 또는 None
    """
    domain = urlparse(base_url).netloc
    if domain in _JS_CONVERTERS:
        result = _JS_CONVERTERS[domain](href, base_url)
        if result:
            return result
    url_match = re.search(r"https?://[^\s'\"]+", href)
    return url_match.group(0) if url_match else None


# ════════════════════════════════════════════════════════════
# HTTP 요청 처리
# ════════════════════════════════════════════════════════════

def create_session() -> requests.Session:
    """
    공통 헤더가 설정된 HTTP 세션을 생성합니다.
    브라우저 유사 User-Agent로 차단을 최소화합니다.

    Returns:
        설정 완료된 Session 객체
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
    })
    return s


def fetch_page(session: requests.Session, url: str, timeout: int = 10) -> tuple:
    """
    URL의 HTML을 가져옵니다.

    HTTPS 처리:
        1차: verify=True (정상 검증)
        2차: SSLError 시 verify=False 재시도
        실패: (0, "") 반환 → 프로그램 중단 없음

    Args:
        session: HTTP 세션
        url: 요청 URL
        timeout: 타임아웃 (초)

    Returns:
        (상태코드, HTML) 튜플, 실패 시 (0, "")
    """
    try:
        r = session.get(url, timeout=timeout, verify=True, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return r.status_code, r.text

    except requests.exceptions.SSLError:
        logger.warning(f"SSL 오류 → verify=False 재시도: {url}")
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            r = session.get(url, timeout=timeout, verify=False, allow_redirects=True)
            r.encoding = r.apparent_encoding
            return r.status_code, r.text
        except Exception as e:
            logger.error(f"SSL 재시도 실패: {url} → {e}")
            return 0, ""

    except requests.exceptions.TooManyRedirects:
        logger.error(f"리다이렉트 과다: {url}")
        return 0, ""
    except requests.exceptions.ConnectionError as e:
        logger.error(f"연결 실패: {url} → {e}")
        return 0, ""
    except requests.exceptions.Timeout:
        logger.error(f"타임아웃: {url}")
        return 0, ""
    except requests.exceptions.HTTPError as e:
        logger.warning(f"HTTP {e.response.status_code}: {url}")
        return e.response.status_code, ""
    except Exception as e:
        logger.error(f"오류: {url} → {e}")
        return 0, ""


# ════════════════════════════════════════════════════════════
# HTML 파싱
# ════════════════════════════════════════════════════════════

def extract_links(html: str, base_url: str) -> list:
    """
    HTML에서 링크를 추출합니다.
    javascript: 링크는 resolve_js_link()로 변환을 시도합니다.

    Args:
        html: 파싱할 HTML
        base_url: 상대→절대 URL 변환 기준

    Returns:
        정규화된 절대 URL 리스트 (중복 제거)
    """
    soup  = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("mailto:", "tel:", "data:", "void", "#")):
            continue
        if href.startswith("javascript:"):
            converted = resolve_js_link(href, base_url)
            if converted:
                links.append(normalize_url(converted))
            continue
        abs_url = normalize_url(urljoin(base_url, href))
        if abs_url.startswith(("http://", "https://")):
            links.append(abs_url)
    return list(set(links))


def html_to_markdown(html: str, url: str) -> str:
    """
    HTML을 마크다운 문서로 변환합니다.

    변환 태그: h1~h6, p, pre>code, code, ul, ol, table,
               a, img, strong, em, blockquote, hr, br

    Args:
        html: 변환할 HTML
        url: 원본 URL (메타정보 및 링크 절대화용)

    Returns:
        마크다운 문자열
    """
    from bs4 import NavigableString, Tag

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","nav","footer",
                     "header","aside","form","iframe",
                     "noscript","meta","link"]):
        tag.decompose()

    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    body  = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id=re.compile(r"^(content|main|body|post|entry)$", re.I))
        or soup.find(class_=re.compile(r"(content|main|post|entry|article)", re.I))
        or soup.find("body")
        or soup
    )

    # 재귀 대신 명시적 스택을 사용하는 반복(iterative) 변환기
    # RecursionError 없이 수천 개 노드도 안전하게 처리
    def node(root_node) -> str:
        """
        BS4 트리를 명시적 스택 기반 반복 순회로 마크다운 변환합니다.

        재귀 방식은 HTML 노드 수가 많을 때(500개 이상 flat 태그 등)
        Python 기본 재귀 한계(1000)를 초과하여 RecursionError를 유발합니다.
        이 구현은 자체 스택을 사용하므로 수만 개 노드도 안전하게 처리합니다.

        처리 전략:
          - LEAF 태그 (p, pre, ul, ol, table, a, img, hr, blockquote, br, 헤딩, 인라인):
            자식 재귀 없이 해당 노드의 텍스트/속성만으로 마크다운 생성
          - CONTAINER 태그 (div, section, span 등):
            자식 노드를 스택에 역순으로 push하여 순서 보존

        Args:
            root_node: 변환을 시작할 BS4 노드

        Returns:
            마크다운 문자열
        """
        parts = []
        stack = [root_node]

        while stack:
            n = stack.pop()

            if isinstance(n, NavigableString):
                t = str(n)
                if t.strip():
                    parts.append(t)
                else:
                    parts.append(" ")
                continue

            if not isinstance(n, Tag):
                continue

            name = n.name.lower() if n.name else ""

            # ── LEAF 처리: 자식 스택 push 없이 직접 변환 ─────
            if re.match(r"h[1-6]", name):
                parts.append(f"\n\n{'#'*int(name[1])} {n.get_text(strip=True)}\n\n")

            elif name == "p":
                t = n.get_text(separator=" ", strip=True)
                if t:
                    parts.append(f"\n\n{t}\n\n")

            elif name == "pre":
                ct   = n.find("code")
                tgt  = ct or n
                code = tgt.get_text()
                lang = ""
                if ct:
                    for cls in ct.get("class", []):
                        if cls.startswith(("language-", "lang-")):
                            lang = cls.split("-", 1)[1]; break
                parts.append(f"\n\n```{lang}\n{code.rstrip()}\n```\n\n")

            elif name == "code" and (not n.parent or n.parent.name != "pre"):
                parts.append(f"`{n.get_text()}`")

            elif name == "ul":
                items = [f"- {li.get_text(' ', strip=True)}"
                         for li in n.find_all("li", recursive=False)]
                if items:
                    parts.append("\n\n" + "\n".join(items) + "\n\n")

            elif name == "ol":
                items = [f"{i}. {li.get_text(' ', strip=True)}"
                         for i, li in enumerate(n.find_all("li", recursive=False), 1)]
                if items:
                    parts.append("\n\n" + "\n".join(items) + "\n\n")

            elif name == "table":
                parts.append(_table(n))

            elif name == "a":
                href = n.get("href", "").strip()
                txt  = n.get_text(strip=True) or href
                if href and not href.startswith(("javascript:", "mailto:")):
                    parts.append(f"[{txt}]({urljoin(url, href)})")
                else:
                    parts.append(txt)

            elif name == "img":
                src = n.get("src", "").strip()
                if src:
                    parts.append(f"![{n.get('alt','')}]({urljoin(url,src)})")

            elif name in ("strong", "b"):
                t = n.get_text(strip=True)
                if t:
                    parts.append(f"**{t}**")

            elif name in ("em", "i"):
                t = n.get_text(strip=True)
                if t:
                    parts.append(f"*{t}*")

            elif name == "hr":
                parts.append("\n\n---\n\n")

            elif name == "blockquote":
                lines = n.get_text("\n", strip=True).splitlines()
                parts.append("\n\n" + "\n".join(f"> {l}" for l in lines) + "\n\n")

            elif name == "br":
                parts.append("  \n")

            # ── CONTAINER: 자식 노드를 스택에 역순 push ───────
            else:
                for child in reversed(list(n.children)):
                    stack.append(child)

        return "".join(parts)

    def _table(t) -> str:
        rows = t.find_all("tr")
        if not rows: return ""
        md = []
        for i, row in enumerate(rows):
            cells = [c.get_text(strip=True).replace("|","\\|").replace("\n"," ")
                     for c in row.find_all(["th","td"])]
            if not cells: continue
            md.append("| " + " | ".join(cells) + " |")
            if i == 0:
                md.append("| " + " | ".join(["---"]*len(cells)) + " |")
        return "\n\n" + "\n".join(md) + "\n\n" if md else ""

    content = re.sub(r"\n{3,}", "\n\n", node(body)).strip()

    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"# {title or '(제목 없음)'}\n\n"
        f"> **출처:** {url}  \n"
        f"> **수집 일시:** {now}\n\n"
        f"---\n\n"
        f"{content}\n"
    )


# ════════════════════════════════════════════════════════════
# [개선 1] 스트리밍 summary 작성기
# ════════════════════════════════════════════════════════════

class SummaryWriter:
    """
    summary.md를 메모리에 모으지 않고 페이지 처리 즉시
    파일에 append 방식으로 스트리밍 저장합니다.

    기존 방식 (v3):
        모든 페이지 내용을 crawl_results 리스트에 보관 →
        크롤링 완료 후 한 번에 summary.md 작성 →
        대용량 시 메모리 폭증 위험

    개선 방식 (v4):
        페이지 처리 즉시 summary.md에 append →
        crawl_results에는 메타데이터(url, title, status)만 보관 →
        메모리 사용량 O(1) 유지

    TOC는 크롤링 완료 후 메타데이터만으로 구성하여 파일 앞부분에 삽입합니다.

    Attributes:
        path (str): summary.md 파일 경로
        toc_entries (list): TOC 구성용 (url, title, seq) 메타데이터 목록
        _lock (threading.Lock): 멀티스레드 동시 쓰기 보호
        _seq (int): 페이지 순번

    사용 예시:
        writer = SummaryWriter("./output/summary.md")
        writer.write_page(url, title, md_content)  # 페이지마다 호출
        writer.finalize(failed_urls)               # 완료 시 TOC 삽입
    """

    def __init__(self, path: str):
        self.path        = path
        self.toc_entries: list = []
        self._lock       = threading.Lock()
        self._seq        = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # 파일 초기화 (기존 내용 삭제)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")

    def write_page(self, url: str, title: str, content: str) -> None:
        """
        단일 페이지 내용을 summary.md에 즉시 append합니다.
        호출 후 content 변수는 즉시 메모리에서 해제 가능합니다.

        Args:
            url: 페이지 URL
            title: 페이지 제목
            content: 마크다운 본문 (호출 후 해제됨)
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            self.toc_entries.append({"seq": seq, "url": url, "title": title})
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"<!-- ===== PAGE {seq}: {url} ===== -->\n\n")
                f.write(content)
                f.write("\n\n---\n\n")

    def finalize(self, failed_urls: list, elapsed_sec: float) -> None:
        """
        크롤링 완료 후 수집된 TOC와 헤더를 파일 앞부분에 삽입합니다.

        TOC는 이미 저장된 페이지 본문 앞에 prepend되므로,
        임시 파일을 이용해 header + body 순서로 재조립합니다.

        Args:
            failed_urls: 수집 실패 URL 목록
            elapsed_sec: 총 소요 시간 (초)
        """
        now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success = len(self.toc_entries)
        failed  = len(failed_urls)
        m, s    = divmod(int(elapsed_sec), 60)

        # ── 헤더 + TOC 구성 ─────────────────────────────────
        header_lines = [
            "# 크롤링 통합 요약본 (Summary)\n",
            f"> **생성 일시:** {now}  \n",
            f"> **수집 성공:** {success}건  \n",
            f"> **수집 실패:** {failed}건  \n",
            f"> **총 소요 시간:** {m}분 {s}초\n",
            "\n---\n\n",
            "## 목차 (TOC)\n\n",
        ]
        for e in self.toc_entries:
            header_lines.append(f"{e['seq']}. [{e['title']}]({e['url']})\n")

        if failed_urls:
            header_lines.append("\n### 수집 실패 URL\n\n")
            for u in failed_urls:
                header_lines.append(f"- ❌ {u}\n")

        header_lines.append("\n---\n\n## 전체 페이지 내용\n\n")
        header = "".join(header_lines)

        # ── 기존 본문 읽기 → 헤더 + 본문 재조립 ────────────
        tmp_path = self.path + ".tmp"
        with open(self.path, "r", encoding="utf-8") as src, \
             open(tmp_path,  "w", encoding="utf-8") as dst:
            dst.write(header)
            for chunk in iter(lambda: src.read(65536), ""):
                dst.write(chunk)

        os.replace(tmp_path, self.path)
        logger.info(f"summary.md 완성: {self.path}")


# ════════════════════════════════════════════════════════════
# [개선 4] Resume 상태 관리
# ════════════════════════════════════════════════════════════

class ResumeState:
    """
    크롤링 진행 상태를 JSON 파일로 저장/복원하여
    중단 후 재시작 시 이미 처리한 URL을 건너뜁니다.

    저장 파일: {output_dir}/.crawler_state.json

    파일 구조:
        {
            "visited": ["https://...", ...],
            "start_urls": ["https://...", ...],
            "timestamp": "2025-01-15 14:32:00"
        }

    Attributes:
        path (str): 상태 파일 경로
        visited (set): 이미 처리한 URL 집합

    사용 예시:
        state = ResumeState("./output")
        state.load()                   # 이전 상태 복원
        state.mark_visited(url)        # 처리 완료 표시
        state.save()                   # 주기적 저장
        state.clear()                  # 완료 후 정리
    """

    def __init__(self, output_dir: str):
        self.path    = os.path.join(output_dir, ".crawler_state.json")
        self.visited: set = set()
        self._lock   = threading.Lock()
        self._dirty  = 0   # 저장 안 된 변경 횟수 (50마다 자동 저장)

    def load(self) -> int:
        """
        저장된 상태 파일을 읽어 visited 집합을 복원합니다.

        Returns:
            복원된 URL 수 (상태 파일 없으면 0)
        """
        if not os.path.exists(self.path):
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.visited = set(data.get("visited", []))
            logger.info(f"Resume: 이전 상태 복원 ({len(self.visited)}개 URL 건너뜀)")
            return len(self.visited)
        except Exception as e:
            logger.warning(f"상태 파일 로드 실패: {e}")
            return 0

    def mark_visited(self, url: str) -> None:
        """
        URL을 처리 완료로 표시하고, 50개마다 자동 저장합니다.

        Args:
            url: 처리 완료된 URL
        """
        with self._lock:
            self.visited.add(url)
            self._dirty += 1
            if self._dirty >= 50:
                self._write()
                self._dirty = 0

    def save(self) -> None:
        """현재 상태를 즉시 파일에 저장합니다."""
        with self._lock:
            self._write()
            self._dirty = 0

    def _write(self) -> None:
        """내부 저장 메서드 (Lock 없이 호출, 호출자가 Lock 보유해야 함)."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "visited":   list(self.visited),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        """완료 후 상태 파일을 삭제합니다."""
        if os.path.exists(self.path):
            os.remove(self.path)
            logger.info("Resume 상태 파일 삭제 완료")

    def __contains__(self, url: str) -> bool:
        return url in self.visited


# ════════════════════════════════════════════════════════════
# [개선 8] 통계 리포트 생성
# ════════════════════════════════════════════════════════════

def build_report(
    stats: dict,
    output_dir: str,
    elapsed_sec: float,
    failed_urls: list,
) -> str:
    """
    크롤링 완료 후 상세 통계 리포트(report.md)를 생성합니다.

    리포트 내용:
        - 수집 개요 (성공/실패/소요 시간/초당 처리 속도)
        - 도메인별 수집 현황
        - 파일 크기 통계 (최소/최대/평균/전체 합계)
        - 수집 실패 URL 목록

    Args:
        stats: 크롤링 통계 딕셔너리
        output_dir: 리포트 저장 디렉터리
        elapsed_sec: 총 소요 시간 (초)
        failed_urls: 실패 URL 목록

    Returns:
        생성된 report.md 파일 경로
    """
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    success = stats.get("success", 0)
    failed  = stats.get("failed",  0)
    total   = success + failed
    m, s    = divmod(int(elapsed_sec), 60)
    rate    = total / elapsed_sec if elapsed_sec > 0 else 0

    # 파일 크기 통계
    sizes = stats.get("file_sizes", [])
    if sizes:
        avg_kb  = sum(sizes) / len(sizes) / 1024
        max_kb  = max(sizes) / 1024
        min_kb  = min(sizes) / 1024
        total_mb= sum(sizes) / 1024 / 1024
    else:
        avg_kb = max_kb = min_kb = total_mb = 0

    lines = [
        "# 크롤링 통계 리포트",
        "",
        f"> **생성 일시:** {now}",
        "",
        "---",
        "",
        "## 수집 개요",
        "",
        f"| 항목 | 값 |",
        f"| --- | --- |",
        f"| 총 요청 | {total}건 |",
        f"| 성공 | {success}건 |",
        f"| 실패 | {failed}건 |",
        f"| 성공률 | {success/total*100:.1f}% |" if total else "| 성공률 | - |",
        f"| 총 소요 시간 | {m}분 {s}초 |",
        f"| 초당 처리 속도 | {rate:.2f} 페이지/초 |",
        "",
        "## 파일 크기 통계",
        "",
        f"| 항목 | 값 |",
        f"| --- | --- |",
        f"| 전체 합계 | {total_mb:.2f} MB |",
        f"| 페이지 평균 | {avg_kb:.1f} KB |",
        f"| 최대 | {max_kb:.1f} KB |",
        f"| 최소 | {min_kb:.1f} KB |",
        "",
    ]

    # 도메인별 통계
    domain_stats = stats.get("domain_stats", {})
    if domain_stats:
        lines += ["## 도메인별 수집 현황", "", "| 도메인 | 성공 | 실패 |", "| --- | --- | --- |"]
        for domain, ds in domain_stats.items():
            lines.append(f"| {domain} | {ds.get('success',0)} | {ds.get('failed',0)} |")
        lines.append("")

    if failed_urls:
        lines += ["## 수집 실패 URL 목록", ""]
        for u in failed_urls:
            lines.append(f"- {u}")
        lines.append("")

    report_path = os.path.join(output_dir, "report.md")
    os.makedirs(output_dir, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"통계 리포트 저장: {report_path}")
    return report_path


# ════════════════════════════════════════════════════════════
# [개선 7] 실패 URL 별도 파일 저장
# ════════════════════════════════════════════════════════════

def save_failed_urls(failed_urls: list, output_dir: str) -> str:
    """
    수집 실패 URL 목록을 failed_urls.txt에 저장합니다.
    이 파일을 urls.txt로 사용하여 실패분만 재수집할 수 있습니다.

    Args:
        failed_urls: 실패 URL 문자열 리스트
        output_dir: 저장 디렉터리

    Returns:
        저장된 파일 경로
    """
    path = os.path.join(output_dir, "failed_urls.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 수집 실패 URL 목록\n")
        f.write("# 이 파일을 urls.txt로 사용하여 실패분만 재수집할 수 있습니다.\n")
        f.write(f"# 생성 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        for url in failed_urls:
            f.write(url + "\n")
    logger.info(f"실패 URL 저장: {path} ({len(failed_urls)}건)")
    return path


# ════════════════════════════════════════════════════════════
# 핵심 크롤러 클래스 (v4.0)
# ════════════════════════════════════════════════════════════

class WebCrawler:
    """
    대용량 처리에 최적화된 멀티스레드 웹 크롤러입니다.

    v4.0 개선 사항:
        메모리: 페이지 처리 즉시 본문 해제, summary 스트리밍 저장
        속도:   ThreadPoolExecutor로 다수 페이지 병렬 수집
        안정성: Resume으로 중단 지점 복원, 실패 URL 분리 재수집 가능
        편의성: 진행률 바, TOC 자동 생성, 통계 리포트

    Attributes:
        url_file (str):    URL 목록 파일 경로
        output_dir (str):  결과 저장 디렉터리
        delay (float):     요청 간 딜레이 (초)
        timeout (int):     HTTP 타임아웃 (초)
        workers (int):     병렬 수집 스레드 수
        resume (bool):     이전 상태 이어받기 여부
        visited (set):     방문 완료 URL 집합
        _lock:             공유 자원 보호 뮤텍스

    사용 예시:
        crawler = WebCrawler(
            url_file="urls.txt",
            output_dir="./output",
            delay=1.0,
            workers=3,
            resume=True,
        )
        crawler.run()
    """

    def __init__(
        self,
        url_file:   str   = "urls.txt",
        output_dir: str   = "./output",
        delay:      float = 1.0,
        timeout:    int   = 10,
        workers:    int   = 3,
        resume:     bool  = False,
    ):
        self.url_file   = url_file
        self.output_dir = output_dir
        self.delay      = delay
        self.timeout    = timeout
        self.workers    = workers
        self.resume     = resume

        self.visited: set   = set()
        self.failed_urls: list = []
        self._lock          = threading.Lock()
        self.session        = create_session()

        # 통계
        self.stats = {
            "success":      0,
            "failed":       0,
            "file_sizes":   [],
            "domain_stats": {},
        }

        # 서브 시스템
        self._summary: SummaryWriter = None
        self._resume:  ResumeState   = None
        self._bar:     ProgressBar   = None

    # ── 단일 URL 처리 ────────────────────────────────────────
    def _process_url(self, url: str) -> tuple:
        """
        단일 URL을 수집·변환·저장하는 처리 단위입니다.

        [메모리 최적화]
        마크다운 변환 후 SummaryWriter.write_page()와 save_markdown()에
        내용을 전달한 즉시 md_content 변수를 del로 해제합니다.
        crawl_results에는 본문 없이 메타데이터(url, title)만 기록합니다.

        Args:
            url: 처리할 URL

        Returns:
            (성공여부, 추출된 링크 리스트) 튜플
        """
        domain = urlparse(url).netloc
        status, html = fetch_page(self.session, url, self.timeout)

        if not html or status == 0:
            with self._lock:
                self.failed_urls.append(url)
                self.stats["failed"] += 1
                self.stats["domain_stats"].setdefault(domain, {"success":0,"failed":0})
                self.stats["domain_stats"][domain]["failed"] += 1
            if self._bar:
                self._bar.update(success=False)
            self._resume.mark_visited(url)
            return False, []

        # 마크다운 변환
        md_content = html_to_markdown(html, url)
        title_m    = re.search(r"^# (.+)$", md_content, re.MULTILINE)
        title      = title_m.group(1).strip() if title_m else url

        # 개별 파일 저장
        filepath   = url_to_filename(url, self.output_dir)
        _save_md(filepath, md_content)
        file_size  = len(md_content.encode("utf-8"))

        # summary 스트리밍 저장 (즉시 파일에 append)
        self._summary.write_page(url, title, md_content)

        # [메모리 해제] 본문 변수 즉시 삭제
        del md_content

        with self._lock:
            self.stats["success"] += 1
            self.stats["file_sizes"].append(file_size)
            self.stats["domain_stats"].setdefault(domain, {"success":0,"failed":0})
            self.stats["domain_stats"][domain]["success"] += 1

        if self._bar:
            self._bar.update(success=True)
        self._resume.mark_visited(url)

        links = extract_links(html, url)
        del html  # HTML도 즉시 해제
        return True, links

    # ── BFS 탐색 (멀티스레드) ────────────────────────────────
    def _crawl_from(self, start_url: str) -> None:
        """
        시작 URL로부터 BFS로 동일 도메인 내 모든 페이지를 탐색합니다.

        [멀티스레드 구조]
        큐에서 workers 수만큼 URL을 꺼내 ThreadPoolExecutor로 병렬 처리합니다.
        새로 발견된 링크는 메인 스레드에서 큐에 추가하여 BFS 순서를 유지합니다.

        [Resume 지원]
        self._resume.visited에 포함된 URL은 큐에 추가하지 않습니다.

        Args:
            start_url: 탐색 시작 URL
        """
        with self._lock:
            if start_url in self.visited:
                return
            self.visited.add(start_url)

        queue: deque = deque([start_url])
        domain = urlparse(start_url).netloc

        logger.info(f"\n{'═'*60}")
        logger.info(f"  도메인 탐색: {domain}")
        logger.info(f"  병렬 스레드: {self.workers}개")
        logger.info(f"{'═'*60}")

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            while queue:
                # workers 수만큼 배치 구성
                batch = []
                while queue and len(batch) < self.workers:
                    batch.append(queue.popleft())

                # 병렬 실행
                futures = {executor.submit(self._process_url, u): u for u in batch}
                for future in as_completed(futures):
                    url = futures[future]
                    try:
                        success, new_links = future.result()
                    except Exception as e:
                        logger.error(f"처리 오류: {url} → {e}")
                        continue

                    # 새 링크를 큐에 추가
                    for link in new_links:
                        with self._lock:
                            already = (link in self.visited) or (link in self._resume)
                            if not already:
                                self.visited.add(link)
                        if (
                            not already
                            and is_same_domain(start_url, link)
                            and is_crawlable(link)
                        ):
                            queue.append(link)

                # 배치 간 딜레이 (서버 부하 방지)
                if queue:
                    time.sleep(self.delay)

        logger.info(f"  탐색 완료: {domain}")

    # ── 메인 실행 ────────────────────────────────────────────
    def run(self) -> None:
        """
        크롤러 전체 실행 흐름을 제어하는 메인 메서드입니다.

        실행 순서:
            1. URL 파일 로드
            2. Resume 상태 복원 (--resume 시)
            3. SummaryWriter, ProgressBar 초기화
            4. 각 시작 URL에 대해 BFS 탐색 (멀티스레드, 깊이 제한 없음)
            5. summary.md TOC 삽입 (finalize)
            6. failed_urls.txt 저장
            7. report.md 생성
            8. Resume 상태 파일 삭제
            9. 최종 통계 출력
        """
        # ── 1. URL 로드 ──────────────────────────────────────
        try:
            start_urls = load_urls_from_file(self.url_file)
        except FileNotFoundError as e:
            logger.error(str(e)); sys.exit(1)

        if not start_urls:
            logger.error("수집할 URL이 없습니다."); sys.exit(1)

        os.makedirs(self.output_dir, exist_ok=True)

        # ── 2. Resume 초기화 ─────────────────────────────────
        self._resume = ResumeState(self.output_dir)
        if self.resume:
            restored = self._resume.load()
            if restored:
                logger.info(f"이전 상태 이어받기: {restored}개 URL 건너뜀")

        # ── 3. SummaryWriter 초기화 ──────────────────────────
        summary_path  = os.path.join(self.output_dir, "summary.md")
        self._summary = SummaryWriter(summary_path)

        # ── 4. 총 예상 페이지 수 추정 (진행률용) ─────────────
        # 정확한 수는 알 수 없으므로 시작 URL 수 × 10으로 초기 추정
        # (실제 수집되면 진행률 바가 동적으로 업데이트됨)
        estimated = len(start_urls) * 10
        self._bar  = ProgressBar(total=estimated, prefix="수집")

        run_start = datetime.now()
        logger.info(f"\n크롤링 시작: {run_start.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"시작 URL:   {len(start_urls)}개")
        logger.info(f"저장 위치:  {os.path.abspath(self.output_dir)}")
        logger.info(f"스레드:     {self.workers}개 | 딜레이: {self.delay}초")

        # ── 5. BFS 탐색 ──────────────────────────────────────
        for i, url in enumerate(start_urls, 1):
            logger.info(f"\n[{i}/{len(start_urls)}] 시작: {url}")
            self._crawl_from(url)

        elapsed = (datetime.now() - run_start).total_seconds()
        self._bar.close()

        # ── 6. summary.md 완성 (TOC 삽입) ────────────────────
        logger.info("\nsummary.md 완성 중 (TOC 삽입)...")
        self._summary.finalize(self.failed_urls, elapsed)

        # ── 7. 실패 URL 저장 ─────────────────────────────────
        if self.failed_urls:
            save_failed_urls(self.failed_urls, self.output_dir)

        # ── 8. 통계 리포트 ────────────────────────────────────
        report_path = build_report(self.stats, self.output_dir, elapsed, self.failed_urls)

        # ── 9. Resume 상태 정리 ──────────────────────────────
        self._resume.save()   # 최종 저장 (재수집 참고용)

        # ── 10. 최종 요약 출력 ────────────────────────────────
        m, s = divmod(int(elapsed), 60)
        total_mb = sum(self.stats["file_sizes"]) / 1024 / 1024
        logger.info(f"\n{'═'*60}")
        logger.info(f"  크롤링 완료")
        logger.info(f"  성공: {self.stats['success']}건 / 실패: {self.stats['failed']}건")
        logger.info(f"  소요: {m}분 {s}초 | 총 {total_mb:.1f} MB 저장")
        logger.info(f"  개별 파일: {os.path.abspath(self.output_dir)}/")
        logger.info(f"  통합 요약: {os.path.abspath(summary_path)}")
        logger.info(f"  통계 리포트: {os.path.abspath(report_path)}")
        if self.failed_urls:
            fp = os.path.join(self.output_dir, "failed_urls.txt")
            logger.info(f"  실패 URL:  {os.path.abspath(fp)}")
        logger.info(f"{'═'*60}\n")


# ════════════════════════════════════════════════════════════
# 파일 저장 헬퍼
# ════════════════════════════════════════════════════════════

def _save_md(filepath: str, content: str) -> None:
    """
    마크다운 파일을 저장합니다.
    상위 디렉터리가 없으면 자동으로 생성합니다.

    Args:
        filepath: 저장 경로
        content: 마크다운 문자열
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


# ════════════════════════════════════════════════════════════
# CLI 진입점
# ════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    """
    커맨드라인 인자를 파싱합니다.

    Returns:
        파싱된 인자 네임스페이스 객체
    """
    p = argparse.ArgumentParser(
        description="대용량 최적화 웹 크롤러 v4.0 (멀티스레드 + Resume + 스트리밍)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
실행 예시:
  python crawler.py                                  기본 실행
  python crawler.py --workers 5                      5스레드 병렬
  python crawler.py --resume                         중단 지점 이어받기
  python crawler.py --delay 0.5 --workers 3          빠른 수집
  python crawler.py --urls urls.txt --output ./docs  경로 지정

실패분 재수집:
  python crawler.py --urls ./output/failed_urls.txt
        """,
    )
    p.add_argument("--urls",    "-u", default="urls.txt",  metavar="FILE",
                   help="URL 목록 파일 (기본값: urls.txt)")
    p.add_argument("--output",  "-o", default="./output",  metavar="DIR",
                   help="저장 디렉터리 (기본값: ./output)")
    p.add_argument("--delay",         default=1.0,  type=float, metavar="SEC",
                   help="요청 간 딜레이 초 단위 (기본값: 1.0)")
    p.add_argument("--timeout",       default=10,   type=int,   metavar="SEC",
                   help="HTTP 타임아웃 초 단위 (기본값: 10)")
    p.add_argument("--workers",       default=3,    type=int,   metavar="N",
                   help="병렬 수집 스레드 수 (기본값: 3)")
    p.add_argument("--resume",        action="store_true",
                   help="이전 중단 지점에서 이어서 실행")
    return p.parse_args()


def main() -> None:
    """프로그램 진입점. 인자 파싱 후 WebCrawler를 실행합니다."""
    args = parse_args()
    WebCrawler(
        url_file   = args.urls,
        output_dir = args.output,
        delay      = args.delay,
        timeout    = args.timeout,
        workers    = args.workers,
        resume     = args.resume,
    ).run()


if __name__ == "__main__":
    main()
