"""NTIS OpenAPI 클라이언트 (https://www.ntis.go.kr/rndopen/openApi).

국가R&D 과제·성과(논문, 특허)·연구보고서·용어사전은 같은 검색 문법을 쓴다.

  GET <BASE>/<endpoint>?apprvKey=…&collection=…&query=…&searchField=…&addQuery=…
      &sortby=…&startPosition=…&displayCount=…&naviCount=…

- query: 띄어 쓰면 AND, `|` 는 OR, `!` 는 NOT, 큰따옴표는 구문 일치.
- searchField: 검색어를 대조할 필드(BI=전체, TI=제목 …). 서비스마다 다르다.
- addQuery: 상세 조건. `필드=값` 을 `&` 로 잇는다(`CN=11110000&DBT=PAP`). 연도는 `PY=2021/MORE,2024/UNDER`.
- NAVIGATION: 결과 전체를 기준별로 센 값(연도·부처·수행기관 …). 집계에 쓴다.

응답은 XML 이다. 검색어 강조 태그(`<span class="search_word">`)는 걷어 낸다. 분류코드 조회(targetSearch)만
문법이 달라 따로 다룬다. 승인키는 설정 파일의 `[ntis] api_key` 에서 읽고, 파일이 바뀌면 다시 읽는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import threading
import time
from typing import Any, Callable
from urllib import error, parse, request
import xml.etree.ElementTree as ET

import appconfig

BASE = 'https://www.ntis.go.kr/rndopen/openApi'
USER_AGENT = 'rnd-finder-mcp/1.0'
MAX_DISPLAY = 100          # 한 번에 받을 수 있는 건수
MAX_BYTES = 8 * 1024 * 1024
CONDITION_JOIN = '&'       # addQuery 안에서 조건을 잇는 문자
_HIGHLIGHT = re.compile(r'</?span[^>]*>')
_RAW_HIGHLIGHT = re.compile(rb'</?span[^>]*>')
_BARE_AMPERSAND = re.compile(rb'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)')
_CONTROL = re.compile(rb'[\x00-\x08\x0b\x0c\x0e-\x1f]')
_SPACES = re.compile(r'[ \t\r\f\v]+')
SORTS = {'relevance': 'RANK/DESC', 'recent': 'DATE/DESC', 'oldest': 'DATE/ASC'}


class NtisError(Exception):
    """kind: no_key(키 미등록) | auth(키 거절) | http | parse | api(서비스가 돌려준 오류) | input(잘못된 조건)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Settings:
    api_key: str = ''
    external_use: bool = True
    delay: float = 0.3
    timeout: float = 20.0
    cache_ttl: float = 600.0
    key_source: str = ''       # 키를 어디서 읽었는지(상태 표시용)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> 'Settings':
        env = os.environ if env is None else env
        values = appconfig.section('ntis', path)
        key, source = str(values.get('api_key') or '').strip(), ''
        if key:
            source = str(appconfig.config_path(path))
        if (env.get('NTIS_API_KEY') or '').strip():
            key, source = env['NTIS_API_KEY'].strip(), '환경변수 NTIS_API_KEY'

        def number(name: str, default: float, low: float, high: float) -> float:
            try:
                return min(max(float(values.get(name, default)), low), high)
            except (TypeError, ValueError):
                raise ValueError(f'설정 [ntis] {name} 은 숫자여야 합니다') from None

        external = values.get('external_use', True)
        if not isinstance(external, bool):
            raise ValueError('설정 [ntis] external_use 는 true 또는 false 여야 합니다')
        return cls(api_key=key, external_use=external, delay=number('delay', 0.3, 0.0, 10.0),
                   timeout=number('timeout', 20.0, 1.0, 120.0), cache_ttl=number('cache_ttl', 600.0, 0.0, 86400.0),
                   key_source=source)


@dataclass(frozen=True)
class Service:
    """검색 서비스 하나. fields: 검색 필드 별칭→코드, filters: 상세 조건 별칭→코드, facets: 집계 기준 별칭→NAVIGATION 이름."""
    name: str
    label: str
    endpoint: str
    collection: str
    fields: dict[str, str]
    filters: dict[str, str]
    facets: dict[str, str] = field(default_factory=dict)
    fixed: tuple[str, ...] = ()        # 항상 붙이는 상세 조건(성과 검색의 DBT)


SERVICES = {service.name: service for service in (
    Service('project', '국가R&D 과제', 'public_project', 'project',
            fields={'all': 'BI', 'title': 'TI', 'manager': 'AU', 'funding_agency': 'OG', 'organization': 'PB',
                    'keyword': 'KW', 'abstract': 'AB'},
            filters={'project_number': 'CN', 'title': 'TI01', 'manager': 'AU01', 'funding_agency': 'OG01',
                     'organization': 'PB01', 'ministry': 'PB02', 'program': 'PB03', 'subprogram': 'PB05',
                     'keyword': 'KW01'},
            facets={'year': 'projectyear', 'ministry': 'ministryname', 'organization': 'leadagency',
                    'funding_agency': 'manageagency', 'program': 'budgetprojectname', 'field': 'scienceclass1',
                    'performer': 'performagent', 'phase': 'developmentphases', 'six_technology': 'sixtechnology',
                    'strategic_technology': 'strategictechnique'}),     # 매뉴얼에는 strategictechique 로 잘못 적혀 있다
    Service('paper', '국가R&D 성과 - 논문', 'public_result', 'rpaper',
            fields={'all': 'BI', 'title': 'TI', 'author': 'AU', 'journal': 'PB', 'keyword': 'KW', 'abstract': 'AB'},
            filters={'result_id': 'CN', 'title': 'TI01', 'journal': 'PB01', 'project_number': 'TI03', 'author': 'AU01',
                     'keyword': 'KW01'},
            facets={'year': 'year', 'ministry': 'ministryname', 'organization': 'performagency', 'journal': 'journalname',
                    'field': 'scienceclass1', 'performer': 'performagent', 'sci': 'scitype', 'nation': 'nationtype',
                    'author': 'authorname'},
            fixed=('DBT=PAP',)),
    Service('patent', '국가R&D 성과 - 특허', 'public_result', 'rpatent',
            fields={'all': 'BI', 'title': 'TI', 'applicant': 'AU', 'number': 'OG', 'country': 'PB'},
            filters={'result_id': 'CN', 'title': 'TI01', 'project_number': 'TI03', 'applicant': 'AU01',
                     'number': 'OG01', 'country': 'PB02'},
            facets={'year': 'year', 'ministry': 'ministryname', 'organization': 'performagency', 'country': 'country',
                    'field': 'scienceclass1', 'performer': 'performagent', 'regist_type': 'registtype',
                    'applicant': 'applicant'},
            fixed=('DBT=PAT',)),
    Service('report', '국가R&D 연구보고서', 'rresearchpdf', 'rresearchpdf',
            fields={'all': 'BI', 'title': 'TI', 'publisher': 'PB', 'keyword': 'KW', 'abstract': 'AB'},
            filters={'result_id': 'CN', 'title': 'TI01', 'publisher': 'PB01', 'keyword': 'KW01'}),
    Service('term', '국가R&D 용어사전', 'ntisDic', '',
            fields={'all': 'BI', 'title': 'TI', 'abbreviation': 'AB'},
            filters={'term_id': 'CN', 'title': 'TI01', 'abbreviation': 'AB01'},
            facets={'year': 'year', 'field': 'scienceclass', 'type': 'termtypename'}),
)}
CODE_KINDS = {'standard': 'NTIS001', 'strategic': 'NTIS002'}   # 과학기술표준분류 / 국가중점기술


@dataclass
class Page:
    total: int
    start: int
    hits: list[dict[str, Any]]
    facets: dict[str, list[dict[str, Any]]]


def clean(value: str | None) -> str:
    """강조 태그와 불필요한 공백을 걷어 낸 글."""
    text = _HIGHLIGHT.sub('', value or '')
    return '\n'.join(line for line in (_SPACES.sub(' ', part).strip() for part in text.split('\n')) if line)


def element_value(node: ET.Element) -> Any:
    """XML 요소를 dict/글로 바꾼다. 잎에 속성이 있으면 {'text': …, 속성…}, 같은 이름이 여럿이면 목록."""
    children = list(node)
    if not children:
        text = clean(node.text)
        return {'text': text, **node.attrib} if node.attrib else text
    result: dict[str, Any] = dict(node.attrib)
    for child in children:
        value = element_value(child)
        if child.tag in result:
            previous = result[child.tag]
            result[child.tag] = previous + [value] if isinstance(previous, list) else [previous, value]
        else:
            result[child.tag] = value
    return result


def year_condition(year_from: int | None, year_to: int | None) -> str:
    """기준년도 조건. 둘 다 없으면 ''."""
    for value in (year_from, year_to):
        if value is not None and not 1900 <= int(value) <= 2100:
            raise NtisError('input', f'연도는 1900~2100 사이여야 합니다: {value}')
    if year_from is not None and year_to is not None:
        if int(year_from) > int(year_to):
            raise NtisError('input', f'year_from({year_from})이 year_to({year_to})보다 큽니다')
        return f'PY={int(year_from)}/SAME' if int(year_from) == int(year_to) else f'PY={int(year_from)}/MORE,{int(year_to)}/UNDER'
    if year_from is not None:
        return f'PY={int(year_from)}/MORE'
    return f'PY={int(year_to)}/UNDER' if year_to is not None else ''


def parse_search(body: bytes) -> Page:
    root = _root(body)
    if root.tag != 'RESULT':
        raise NtisError('parse', f'알 수 없는 응답 형식입니다: <{root.tag}>')
    if root.find('resMsg') is not None or (root.findtext('verifyFlag') or '').upper() == 'FALSE':
        raise _api_error(root.findtext('resMsg') or '요청이 거절되었습니다', root.findtext('resCd'))
    facets: dict[str, list[dict[str, Any]]] = {}
    for entry in root.iterfind('NAVIGATION/NAVIGATIONENTRY'):
        rows = []
        for item in entry.iterfind('NAVIGATIONELEMENTS/NAVIGATIONELEMENT'):
            name, _, code = (item.get('NAME') or '').partition('▦')
            rows.append({'name': clean(name), 'code': code.strip(), 'count': _int(item.get('COUNT'))})
        facets[entry.get('NAME') or ''] = rows
    hits = [element_value(hit) for hit in root.iterfind('RESULTSET/HIT')]
    return Page(total=_int(root.findtext('TOTALHITS')), start=_int(root.findtext('STARTPOSITION')) or 1, hits=hits, facets=facets)


def parse_codes(body: bytes) -> list[dict[str, str]]:
    root = _root(body)
    if root.tag != 'ntis':
        raise NtisError('parse', f'알 수 없는 응답 형식입니다: <{root.tag}>')
    code, message = (root.findtext('status/recode') or '').strip(), (root.findtext('status/remsg') or '').strip()
    if code == '101':
        return []
    if code != '100':
        raise _api_error(message or '분류코드 조회에 실패했습니다', code)
    # 값이 없는 항목은 'null' 이라는 글자로 온다
    return [{child.tag: '' if clean(child.text) == 'null' else clean(child.text) for child in dataset}
            for dataset in root.iterfind('contents/dataset')]


def _root(body: bytes) -> ET.Element:
    """NTIS 응답은 XML 규칙을 어길 때가 있다(2026-11 실측): 연구보고서의 원문 주소에 `&` 가 그대로 들어 있고,
    강조 태그가 이스케이프되지 않은 채 값 안에 끼어 있다. 강조 태그는 미리 걷고, 그래도 안 읽히면 `&` 를 고쳐 다시 읽는다."""
    body = _RAW_HIGHLIGHT.sub(b'', body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        try:
            root = ET.fromstring(_CONTROL.sub(b'', _BARE_AMPERSAND.sub(b'&amp;', body)))
        except ET.ParseError:
            raise NtisError('parse', 'NTIS 응답을 해석하지 못했습니다(XML 아님): ' + body[:120].decode('utf-8', 'replace')) from None
    if root.tag == 'error':
        raise _api_error((root.text or '').strip() or 'NTIS 가 오류를 돌려주었습니다', None)
    return root


def _api_error(message: str, code: str | None) -> NtisError:
    kind = 'auth' if any(word in message for word in ('인증키', '승인키', '권한')) or code == '200' else 'api'
    return NtisError(kind, f'NTIS 오류: {message}' + (f' (코드 {code})' if code else ''))


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


class Client:
    """요청 간격·짧은 캐시·설정 다시 읽기를 맡는다. fetch(url, data, timeout) → bytes 를 바꿔 끼워 시험한다."""

    def __init__(self, config: str | os.PathLike | None = None, fetch: Callable[[str, bytes | None, float], bytes] | None = None,
                 settings: Settings | None = None):
        self._config = config
        self._fixed = settings
        self._settings = settings or Settings.load(config)
        self._stamp = appconfig.modified_at(config)
        self._fetch = fetch or _http_fetch
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache: dict[str, tuple[float, bytes]] = {}
        self.last_error = ''
        self.last_ok = 0.0

    @property
    def settings(self) -> Settings:
        """설정 파일이 바뀌었으면 다시 읽는다 — 서버를 띄워 둔 채 키를 등록해도 바로 반영된다."""
        if self._fixed is None:
            stamp = appconfig.modified_at(self._config)
            if stamp != self._stamp:
                self._settings, self._stamp = Settings.load(self._config), stamp
                self._cache.clear()
        return self._settings

    def search(self, service: str, query: str, *, search_field: str = '', conditions: dict[str, str] | None = None,
               year_from: int | None = None, year_to: int | None = None, sort: str = '', start: int = 1,
               count: int = 10, navi: int = 0) -> Page:
        spec = SERVICES[service]
        query = (query or '').strip()
        parts = list(spec.fixed)
        year = year_condition(year_from, year_to)
        if year:
            parts.append(year)
        for alias, value in (conditions or {}).items():
            value = str(value or '').strip()
            if not value:
                continue
            if alias not in spec.filters:
                raise NtisError('input', f'{spec.label} 검색에 없는 조건입니다: {alias}')
            if any(mark in value for mark in '&=,'):
                raise NtisError('input', f'조건 값에 & = , 는 쓸 수 없습니다: {value}')
            parts.append(f'{spec.filters[alias]}={value}')
        if not query and len(parts) == len(spec.fixed):
            raise NtisError('input', '검색어나 조건(연도·부처 등)을 하나 이상 주어야 합니다')
        if search_field and search_field not in spec.fields:
            raise NtisError('input', f'{spec.label} 검색에 없는 검색 범위입니다: {search_field} (가능: {", ".join(spec.fields)})')
        if sort and sort not in SORTS:
            raise NtisError('input', f'sort 는 {", ".join(SORTS)} 중 하나여야 합니다')
        params = {'collection': spec.collection, 'query': query, 'searchField': spec.fields.get(search_field, ''),
                  'addQuery': CONDITION_JOIN.join(parts), 'sortby': SORTS.get(sort, ''),
                  'startPosition': max(1, int(start)), 'displayCount': min(max(1, int(count)), MAX_DISPLAY),
                  'naviCount': max(0, int(navi))}
        return self._call(spec.endpoint, {key: value for key, value in params.items() if value not in ('', 0)}, parse_search)

    def codes(self, kind: str, code: str = '') -> list[dict[str, str]]:
        if kind not in CODE_KINDS:
            raise NtisError('input', f'kind 는 {", ".join(CODE_KINDS)} 중 하나여야 합니다')
        params = {'rqstSlctCd': CODE_KINDS[kind]}
        if code.strip():
            params['rqstSearchCd'] = code.strip()
        return self._call('targetSearch', params, parse_codes, post=True)

    def _call(self, endpoint: str, params: dict[str, Any], parser: Callable[[bytes], Any], post: bool = False) -> Any:
        settings = self.settings
        if not settings.api_key:
            raise NtisError('no_key', 'NTIS 승인키가 등록되지 않았습니다. 설정 파일의 [ntis] api_key 에 키를 넣으세요 '
                                      f'({appconfig.config_path(self._config)}).')
        public = parse.urlencode(sorted(params.items()))
        cache_key = f'{endpoint}?{public}'
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and now - cached[0] < settings.cache_ttl:
            return parser(cached[1])
        secret = parse.urlencode({'apprvKey': settings.api_key})
        url, data = (f'{BASE}/{endpoint}', f'{secret}&{public}'.encode()) if post else (f'{BASE}/{endpoint}?{secret}&{public}', None)
        with self._lock:
            wait = settings.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                body = self._fetch(url, data, settings.timeout)
                parsed = parser(body)
            except NtisError as exc:
                # NTIS 는 거절한 키를 오류 문구에 그대로 실어 보낸다 — 밖으로 내보내기 전에 가린다
                self.last_error = str(exc).replace(settings.api_key, '***')
                raise NtisError(exc.kind, self.last_error) from None
            except Exception as exc:     # 주소에 키가 들어 있으므로 원래 예외 글을 그대로 내보내지 않는다
                self.last_error = f'NTIS 요청 실패: {type(exc).__name__}'
                raise NtisError('http', self.last_error) from None
            finally:
                self._last = time.monotonic()
        self.last_error, self.last_ok = '', time.time()
        if settings.cache_ttl > 0:               # 해석까지 된 응답만 캐시에 넣는다
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (now, body)
        return parsed


def _http_fetch(url: str, data: bytes | None, timeout: float) -> bytes:
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}     # Accept 를 application/xml 로 좁히면 NTIS 가 404 를 낸다(2026-11 실측)
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
    try:
        with request.urlopen(request.Request(url, data=data, headers=headers), timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
    except error.HTTPError as exc:
        raise NtisError('http', f'NTIS 요청 실패: HTTP {exc.code}') from None
    except (error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise NtisError('http', f'NTIS 에 연결하지 못했습니다: {type(reason).__name__}') from None
    if len(body) > MAX_BYTES:
        raise NtisError('http', 'NTIS 응답이 너무 큽니다')
    return body
