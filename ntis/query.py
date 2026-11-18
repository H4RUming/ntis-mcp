"""NTIS 검색 결과를 도구가 돌려줄 모양으로 다듬는다: 과제·성과·보고서 검색, 기준별 집계, 용어·분류코드 조회.

과제 검색은 NTIS 가 대외 서비스(웹·모바일)에 허용한 항목만 내보낼 수 있다(과제검색 매뉴얼 1.2):
과제명, 과제고유번호, 기준년도, 예산사업명, 연구책임자, 과제수행기관, 부처명, 총연구기간, 당해연도 연구기간,
정부투자연구비, 총연구비, 연구내용(250자 내외), 한글·영문 키워드. 설정의 external_use 가 참이면 이 항목만
돌려주고 나머지는 과제 상세 페이지 주소로 대신한다.
"""
from __future__ import annotations

import time
from typing import Any

import appconfig

from .client import CODE_KINDS, SERVICES, Client, NtisError, Page

PROJECT_URL = 'https://www.ntis.go.kr/project/pjtInfo.do?pjtId={}'
MAX_LIMIT = 50
SUMMARY_CHARS = 250
_EXTERNAL_NOTE = ('NTIS 가 대외 서비스에 허용한 항목만 담았습니다. 연구목표·기대효과·참여연구원·분류 등은 url 의 '
                  'NTIS 과제 상세 페이지(로그인 필요)에서 확인합니다.')


def _text(value: Any, *path: str) -> str:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, dict):
        value = value.get('text') or value.get('Korean') or value.get('Name') or ''
    if isinstance(value, list):
        value = ', '.join(_text(item) for item in value)
    return str(value or '').strip()


def _number(value: Any) -> int | None:
    text = _text(value).replace(',', '')
    try:
        return int(float(text))
    except ValueError:
        return None


def _date(value: str) -> str:
    digits = ''.join(ch for ch in value if ch.isdigit())[:8]
    return f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}' if len(digits) == 8 else value


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def _drop_empty(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if value not in ('', None, [], {})}


def project_record(hit: dict[str, Any], external_use: bool) -> dict[str, Any]:
    number = _text(hit, 'ProjectNumber')
    abstract = _text(hit, 'Abstract', 'Teaser') or _text(hit, 'Abstract', 'Full')
    record = {
        'project_number': number,
        'title': _text(hit, 'ProjectTitle', 'Korean'),
        'title_en': _text(hit, 'ProjectTitle', 'English'),
        'year': _number(hit.get('ProjectYear')),
        'ministry': _text(hit, 'Ministry', 'Name'),
        'program': _text(hit, 'BudgetProject', 'Name'),
        'organization': _text(hit, 'ResearchAgency', 'Name'),
        'manager': _text(hit, 'Manager', 'Name'),
        'total_period': _period(hit, 'TotalStart', 'TotalEnd'),
        'year_period': _period(hit, 'Start', 'End'),
        'government_funds': _number(hit.get('GovernmentFunds')),
        'total_funds': _number(hit.get('TotalFunds')),
        'summary': _short(abstract, SUMMARY_CHARS),
        'keywords': _text(hit, 'Keyword', 'Korean'),
        'keywords_en': _text(hit, 'Keyword', 'English'),
        'url': PROJECT_URL.format(number) if number else '',
    }
    if not external_use:
        record.update({
            'goal': _text(hit, 'Goal', 'Full') or _text(hit, 'Goal', 'Teaser'),
            'abstract': _text(hit, 'Abstract', 'Full') or abstract,
            'effect': _text(hit, 'Effect', 'Full') or _text(hit, 'Effect', 'Teaser'),
            'funding_agency': _text(hit, 'OrderAgency', 'Name'),
            'subprogram': _text(hit, 'BigprojectTitle'),
            'organization_project_number': _text(hit, 'OrganizationPNumber'),
            'researchers': _text(hit, 'Researchers', 'Name'),
            'performer': _text(hit, 'PerformAgent'),
            'phase': _text(hit, 'DevelopmentPhases'),
            'region': _text(hit, 'Region'),
            'six_technology': _text(hit, 'SixTechnology'),
            'science_class': _classes(hit.get('ScienceClass')),
            'previous_project_number': _text(hit, 'SeriesProject', 'PrePjtId'),
        })
    return _drop_empty(record)


def _period(hit: dict[str, Any], start: str, end: str) -> str:
    first, last = _date(_text(hit, 'ProjectPeriod', start)), _date(_text(hit, 'ProjectPeriod', end))
    return f'{first} ~ {last}' if first or last else ''


def _classes(value: Any) -> list[str]:
    rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
    names = []
    for row in rows:
        name = ' > '.join(part for part in (_text(row, 'Large'), _text(row, 'Medium'), _text(row, 'Small')) if part)
        if name and name not in names:
            names.append(name)
    return names


def paper_record(hit: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty({
        'result_id': _text(hit, 'ResultID'), 'title': _text(hit, 'ResultTitle'), 'journal': _text(hit, 'JournalName'),
        'authors': _text(hit, 'Author'), 'year': _number(hit.get('Year')) or _number(hit.get('PubYear')),
        'paper_type': _text(hit, 'PaperType'), 'sci': {'01': 'SCI', '02': '비SCI'}.get(_text(hit, 'SciType'), _text(hit, 'SciType')),
        'nation': _text(hit, 'NationType'), 'summary': _short(_text(hit, 'Abstract', 'Teaser') or _text(hit, 'Abstract', 'Full'), 400),
        **_linked_project(hit)})


def patent_record(hit: dict[str, Any]) -> dict[str, Any]:
    return _drop_empty({
        'result_id': _text(hit, 'ResultID'), 'title': _text(hit, 'ResultTitle'), 'year': _number(hit.get('Year')),
        'country': _text(hit, 'RegistCountry'), 'number': _text(hit, 'RegistNumber'), 'applicant': _text(hit, 'Registrant'),
        'regist_type': _text(hit, 'RegistType'), 'ipr_type': _text(hit, 'IprType'), **_linked_project(hit)})


def _linked_project(hit: dict[str, Any]) -> dict[str, Any]:
    number = _text(hit, 'ProjectID')
    return {'project_number': number, 'project_title': _text(hit, 'ProjectTitle'), 'project_year': _number(hit.get('ProjectYear')),
            'ministry': _text(hit, 'MinistryName'), 'program': _text(hit, 'BudgetProject'),
            'organization': _text(hit, 'PerformAgency'), 'project_url': PROJECT_URL.format(number) if number else ''}


def report_record(hit: dict[str, Any]) -> dict[str, Any]:
    number = _text(hit, 'ProjectNumber')
    return _drop_empty({
        'report_id': _text(hit, 'ResearchPublicNo') or _text(hit, 'TermSn'), 'title': _text(hit, 'ResultTitle', 'Korean') or _text(hit, 'ResultTitle'),
        'title_en': _text(hit, 'ResultTitle', 'English'), 'year': _number(hit.get('PublicationYear')),
        'publisher': _text(hit, 'PublicationAgency'), 'summary': _short(_text(hit, 'Abstract', 'Korean') or _text(hit, 'Abstract'), 600),
        'keywords': _text(hit, 'Keyword', 'Korean') or _text(hit, 'Keyword'), 'contents': _short(_text(hit, 'Contents'), 600),
        'url': _text(hit, 'DocUrl'), 'project_number': number, 'project_title': _text(hit, 'ProjectTitle'),
        'organization': _text(hit, 'LeadAgency'), 'manager': _text(hit, 'ManagerName'),
        'funding_agency': _text(hit, 'SpecializedAgency'), 'project_url': PROJECT_URL.format(number) if number else ''})


def term_record(hit: dict[str, Any]) -> dict[str, Any]:
    known = {'TermSn': 'term_id', 'KorWord': 'term', 'EngWord': 'term_en', 'MainAbrv': 'abbreviation', 'TermCls': 'type',
             'SntStdCls': 'field', 'RelWord': 'related', 'TermDctn': 'definition'}
    record = {name: _text(hit, tag) for tag, name in known.items()}
    record['definition'] = _short(record['definition'], 1500)
    for tag, value in hit.items():      # 그 밖에 매뉴얼에 없는 항목이 오면 그대로 싣는다
        if tag not in known and tag not in ('NO', 'SITID') and _text(value):
            record[tag] = _short(_text(value), 1000)
    return _drop_empty(record)


_RECORDS = {'paper': paper_record, 'patent': patent_record, 'report': report_record, 'term': term_record}


def _page(client: Client, service: str, query: str, search_in: str, conditions: dict[str, str], year_from: int | None,
          year_to: int | None, sort: str, limit: int, offset: int) -> tuple[Page, int]:
    limit = min(max(1, int(limit)), MAX_LIMIT)
    offset = max(0, int(offset))
    page = client.search(service, query, search_field=search_in, conditions=conditions, year_from=year_from or None,
                         year_to=year_to or None, sort=sort or ('relevance' if query.strip() else 'recent'),
                         start=offset + 1, count=limit)
    return page, offset


def _listing(page: Page, offset: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    following = offset + len(items)
    return {'total': page.total, 'offset': offset, 'count': len(items),
            'next_offset': following if items and following < page.total else None, 'items': items}


def search_projects(client: Client, query: str = '', search_in: str = '', year_from: int = 0, year_to: int = 0,
                    ministry: str = '', organization: str = '', program: str = '', manager: str = '', sort: str = '',
                    limit: int = 10, offset: int = 0) -> dict[str, Any]:
    conditions = {'ministry': ministry, 'organization': organization, 'program': program, 'manager': manager}
    page, offset = _page(client, 'project', query, search_in, conditions, year_from, year_to, sort, limit, offset)
    external = client.settings.external_use
    result = _listing(page, offset, [project_record(hit, external) for hit in page.hits])
    result['note'] = ('과제는 연도마다 한 건씩 등록됩니다(다년도 과제는 기준년도별로 따로 나옵니다). 연구비는 그 기준년도 한 해의 금액이며 NTIS 가 준 숫자 그대로입니다.'
                      + (' ' + _EXTERNAL_NOTE if external else ''))
    return result


def get_project(client: Client, project_number: str) -> dict[str, Any]:
    number = ''.join(project_number.split())
    if not number.isalnum():
        raise NtisError('input', f'과제고유번호 형식이 아닙니다: {project_number}')
    page = client.search('project', number, conditions={'project_number': number}, count=5)
    matches = [hit for hit in page.hits if _text(hit, 'ProjectNumber') == number] or page.hits[:1]
    if not matches:
        raise NtisError('input', f'과제고유번호 {number} 를 찾지 못했습니다')
    external = client.settings.external_use
    record = project_record(matches[0], external)
    if external:
        record['note'] = _EXTERNAL_NOTE
    return record


def search_results(client: Client, kind: str, query: str = '', search_in: str = '', year_from: int = 0, year_to: int = 0,
                   project_number: str = '', sort: str = '', limit: int = 10, offset: int = 0) -> dict[str, Any]:
    if kind not in ('paper', 'patent'):
        raise NtisError('input', 'kind 는 paper(논문) 또는 patent(특허)여야 합니다')
    page, offset = _page(client, kind, query, search_in, {'project_number': project_number}, year_from, year_to, sort, limit, offset)
    return _listing(page, offset, [_RECORDS[kind](hit) for hit in page.hits])


def search_reports(client: Client, query: str = '', search_in: str = '', year_from: int = 0, year_to: int = 0,
                   publisher: str = '', sort: str = '', limit: int = 10, offset: int = 0) -> dict[str, Any]:
    page, offset = _page(client, 'report', query, search_in, {'publisher': publisher}, year_from, year_to, sort, limit, offset)
    result = _listing(page, offset, [report_record(hit) for hit in page.hits])
    result['note'] = '보고서 본문은 주지 않습니다. 제목·초록·목차 같은 메타정보이며 원문은 url 에서 확인합니다.'
    return result


def count(client: Client, service: str, group_by: str, query: str = '', search_in: str = '', year_from: int = 0,
          year_to: int = 0, top: int = 20, **conditions: str) -> dict[str, Any]:
    """검색 결과 전체를 기준 하나로 센다(NTIS 의 NAVIGATION). 목록을 넘기며 세지 않는다."""
    spec = SERVICES[service]
    if group_by not in spec.facets:
        raise NtisError('input', f'{spec.label} 집계 기준은 {", ".join(spec.facets)} 중 하나여야 합니다: {group_by}')
    top = min(max(1, int(top)), 100)
    wanted = 100 if group_by == 'year' else top
    page = client.search(service, query, search_field=search_in, conditions=conditions, year_from=year_from or None,
                         year_to=year_to or None, count=1, navi=wanted)
    rows = page.facets.get(spec.facets[group_by])
    if rows is None:
        raise NtisError('api', f'NTIS 응답에 {group_by} 집계가 없습니다(받은 기준: {", ".join(page.facets) or "없음"})')
    if group_by == 'year':
        rows = sorted(rows, key=lambda row: row['name'], reverse=True)
    else:
        rows = sorted(rows, key=lambda row: -row['count'])
    shown = rows[:top] if group_by != 'year' else rows
    listed = sum(row['count'] for row in shown)
    # 한 과제가 기준값 하나에만 속하는 기준(연도·부처 등)은 합이 total 과 같으면 빠진 값이 없는 것이다
    result = {'total': page.total, 'group_by': group_by, 'rows': [{'name': row['name'], 'count': row['count']} for row in shown],
              'listed_sum': listed, 'complete': len(rows) < wanted or (len(shown) == len(rows) and listed == page.total)}
    if not rows and page.total:
        # 실측(2026-11): 최근 연도만 고르면 NTIS 가 수행기관 집계를 비워서 줄 때가 있다
        result['complete'] = False
        result['caution'] = (f'NTIS 가 이 조건에서는 {group_by} 집계를 주지 않았습니다(전체 {page.total}건). 연도 범위를 넓히거나 '
                             '다른 기준으로 집계하고, 이 기준은 search 도구의 목록으로 확인하세요.')
    elif not result['complete']:
        result['caution'] = (f'값이 많아 건수 상위 {len(shown)}개만 나왔습니다. 나머지 값은 여기에 없으며, 그 건수는 '
                             f'total({page.total})에서 합계({listed})를 뺀 만큼입니다.')
    if service == 'project':
        result['note'] = '과제는 연도마다 한 건씩 등록되므로, 다년도 과제는 수행한 연도 수만큼 세어집니다.'
    return result


def lookup_terms(client: Client, query: str, limit: int = 10) -> dict[str, Any]:
    if not query.strip():
        raise NtisError('input', '찾을 용어를 주어야 합니다')
    page = client.search('term', query, count=min(max(1, int(limit)), MAX_LIMIT))
    return _listing(page, 0, [term_record(hit) for hit in page.hits])


def _code_record(row: dict[str, str]) -> dict[str, Any]:
    return _drop_empty({'code': row.get('classCd') or row.get('cd', ''), 'name': row.get('classCdNm') or row.get('cdNm', ''),
                        'name_en': row.get('classCdNmEng') or row.get('cdNmEng', ''),
                        'parent': row.get('upperClassCd') or row.get('upperCd', ''), 'area': row.get('kindCdNm', ''),
                        'description': _short(row.get('classCdExplan') or row.get('cdExplan', ''), 500)})


def lookup_codes(client: Client, kind: str = 'standard', code: str = '', parent: str = '', name: str = '', limit: int = 50,
                 offset: int = 0) -> dict[str, Any]:
    """분류코드. 조건이 없으면 맨 위 단계만, code 는 그 코드와 바로 아래 단계, parent 는 아래 단계, name 은 이름으로 찾기."""
    records = [_code_record(row) for row in client.codes(kind)]       # 전체 목록(표준분류 약 3천 건)을 받아 여기서 거른다
    codes = {record.get('code') for record in records}
    code, parent, name = code.strip(), parent.strip(), name.strip().lower()
    result: dict[str, Any] = {'kind': kind}
    if code:
        picked = [record for record in records if record.get('code') == code]
        result['children'] = [{'code': record['code'], 'name': record.get('name', '')} for record in records if record.get('parent') == code][:200]
    elif parent:
        picked = [record for record in records if record.get('parent') == parent]
    elif name:
        picked = [record for record in records if name in record.get('name', '').lower() or name in record.get('name_en', '').lower()]
    else:
        picked = [record for record in records if record.get('parent', '') not in codes]      # 위 단계가 목록에 없는 것 = 맨 위
    limit, offset = min(max(1, int(limit)), 200), max(0, int(offset))
    items = picked[offset:offset + limit]
    result.update(total=len(picked), offset=offset, count=len(items),
                  next_offset=offset + len(items) if offset + len(items) < len(picked) else None, items=items)
    return result


def status(client: Client, config: Any = None, probe: bool = False) -> dict[str, Any]:
    """키 등록 여부와 설정. probe 가 참이면 실제로 한 번 검색해 키가 받아들여지는지 확인한다."""
    settings = client.settings
    result: dict[str, Any] = {
        'key_registered': bool(settings.api_key),
        'key_source': settings.key_source or None,
        'config_file': str(appconfig.config_path(config)),
        'external_use': settings.external_use,
        'services': {name: spec.label for name, spec in SERVICES.items()} | {'code': '과학기술표준분류·국가중점기술 코드'},
        'not_available': '사업공고는 NTIS OpenAPI 로 받을 수 없습니다(공고는 기업마당 서버를 씁니다).',
    }
    warning = appconfig.permission_warning(config)
    if warning:
        result['warning'] = warning
    if not settings.api_key:
        result['how_to_register'] = (f'{result["config_file"]} 의 [ntis] api_key 에 승인키를 넣으면 다시 시작하지 않아도 '
                                     '바로 반영됩니다. 파일이 없으면 config.example.toml 을 복사해 만듭니다.')
    elif probe:
        try:
            page = client.search('project', '연구', count=1)
            result['probe'] = {'ok': True, 'project_total': page.total}
        except NtisError as exc:
            result['probe'] = {'ok': False, 'kind': exc.kind, 'error': str(exc)}
    if client.last_error:
        result['last_error'] = client.last_error
    if client.last_ok:
        result['last_ok'] = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(client.last_ok))
    return result
