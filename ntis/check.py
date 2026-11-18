"""승인키를 등록한 뒤 실제 NTIS 에 물어 동작을 확인한다:  python -m ntis.check

이 서버의 검색 문법은 NTIS 매뉴얼을 따랐지만, 매뉴얼의 파라미터 이름이 표와 예시에서 서로 다르고 조건 여러 개를
잇는 방법은 예시 한 줄뿐이다. 그래서 필터를 건 건수를 NTIS 가 직접 센 집계값과 맞춰 보는 식으로 검증한다.
요청은 10여 번이며 읽기만 한다. 하나라도 어긋나면 종료 코드 1.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import query
from .client import Client, NtisError


def run(client: Client, word: str = '인공지능', out=print) -> bool:
    failed = 0

    def step(name: str, function) -> object:
        nonlocal failed
        try:
            ok, detail, value = function()
        except NtisError as exc:
            ok, detail, value = False, f'{exc.kind}: {exc}', None
        except Exception as exc:        # 응답 모양이 예상과 다를 때
            ok, detail, value = False, f'{type(exc).__name__}: {exc}', None
        failed += not ok
        out(f'{"OK  " if ok else "FAIL"} {name}: {detail}')
        return value

    settings = client.settings
    if not settings.api_key:
        out('FAIL 승인키가 없습니다. 설정 파일의 [ntis] api_key 를 채우세요.')
        return False
    out(f'승인키: {settings.key_source} (길이 {len(settings.api_key)}) / external_use={settings.external_use}')

    def basic():
        result = query.search_projects(client, word, limit=3)
        first = result['items'][0] if result['items'] else {}
        fields = [name for name in ('title', 'year', 'ministry', 'organization', 'manager', 'program', 'total_funds', 'summary') if name not in first]
        return bool(result['items']) and not fields, f'전체 {result["total"]}건, 첫 과제 "{first.get("title", "")[:30]}" ({first.get("year")}), 빈 항목 {fields or "없음"}, 연구비 예 {first.get("total_funds")}', result
    base = step('과제 검색', basic)

    def paging():
        first, second = query.search_projects(client, word, limit=3), query.search_projects(client, word, limit=3, offset=3)
        a, b = [x['project_number'] for x in first['items']], [x['project_number'] for x in second['items']]
        return bool(b) and not set(a) & set(b), f'1쪽 {a} / 2쪽 {b}', None
    step('쪽 넘기기', paging)

    def years():
        result = query.count(client, 'project', 'year', word)
        return len(result['rows']) > 3, f'{len(result["rows"])}개 연도, 최근 {result["rows"][:3]}', result
    by_year = step('연도별 집계', years)

    if by_year and by_year['rows']:
        year, expected = int(by_year['rows'][1 if len(by_year['rows']) > 1 else 0]['name']), None
        expected = next(row['count'] for row in by_year['rows'] if int(row['name']) == year)

        def one_year():
            total = query.search_projects(client, word, year_from=year, year_to=year, limit=1)['total']
            return total == expected, f'{year}년 조건 {total}건 vs 집계 {expected}건', None
        step('연도 조건(addQuery PY)', one_year)

        def year_range():
            rows = [row for row in by_year['rows'] if year - 2 <= int(row['name']) <= year]
            total = query.search_projects(client, word, year_from=year - 2, year_to=year, limit=1)['total']
            return total == sum(row['count'] for row in rows), f'{year - 2}~{year}년 조건 {total}건 vs 집계 합 {sum(row["count"] for row in rows)}건', None
        step('연도 범위', year_range)

        def ministry():
            rows = query.count(client, 'project', 'ministry', word, year_from=year, year_to=year)['rows']
            if not rows:
                return False, '부처 집계가 비었습니다', None
            total = query.search_projects(client, word, year_from=year, year_to=year, ministry=rows[0]['name'], limit=1)['total']
            return total == rows[0]['count'], f'{year}년 + 부처 "{rows[0]["name"]}" 조건 {total}건 vs 집계 {rows[0]["count"]}건', None
        step('조건 두 개 잇기(연도 & 부처)', ministry)

    def sort():
        recent, oldest = query.search_projects(client, word, sort='recent', limit=3), query.search_projects(client, word, sort='oldest', limit=3)
        a, b = [x.get('year') for x in recent['items']], [x.get('year') for x in oldest['items']]
        return bool(a and b) and min(a) >= max(b), f'recent {a} / oldest {b}', None
    step('정렬', sort)

    def title_only():
        everywhere, title = query.search_projects(client, word, limit=1)['total'], query.search_projects(client, word, search_in='title', limit=1)['total']
        return 0 < title <= everywhere, f'전체 범위 {everywhere}건 ≥ 과제명만 {title}건', None
    step('검색 범위(searchField)', title_only)

    def no_query():
        total = query.search_projects(client, year_from=2024, year_to=2024, limit=1)['total']
        return total > 0, f'검색어 없이 2024년 과제 {total}건', None
    step('검색어 없이 조건만', no_query)

    if base and base['items']:
        number = base['items'][0]['project_number']
        step('과제 한 건', lambda: (query.get_project(client, number).get('project_number') == number, f'과제고유번호 {number}', None))
    for kind in ('paper', 'patent'):
        step(f'성과 검색({kind})', lambda kind=kind: (lambda r: (bool(r['items']), f'전체 {r["total"]}건, 첫 항목 "{(r["items"] or [{}])[0].get("title", "")[:30]}"', None))(query.search_results(client, kind, word, limit=2)))
    step('연구보고서 검색', lambda: (lambda r: (bool(r['items']), f'전체 {r["total"]}건, 첫 항목 "{(r["items"] or [{}])[0].get("title", "")[:30]}"', None))(query.search_reports(client, word, limit=2)))
    step('용어사전', lambda: (lambda r: (bool(r['items']), f'전체 {r["total"]}건, 첫 항목 {(r["items"] or [{}])[0]}', None))(query.lookup_terms(client, '나노', limit=2)))
    step('분류코드', lambda: (lambda r: (r['count'] > 0, f'맨 위 단계 {r["total"]}건, 첫 항목 {(r["items"] or [{}])[0]}', None))(query.lookup_codes(client, 'standard')))
    out('모두 통과' if not failed else f'{failed}개 항목이 어긋났습니다. 위 FAIL 줄의 조건 문법을 ntis/client.py 에서 고쳐야 합니다.')
    return not failed


def main() -> None:
    parser = argparse.ArgumentParser(description='NTIS 승인키와 검색 문법을 실제 요청으로 확인')
    parser.add_argument('--config', type=Path, default=None)
    parser.add_argument('--word', default='인공지능', help='시험에 쓸 검색어')
    args = parser.parse_args()
    sys.exit(0 if run(Client(args.config), args.word) else 1)


if __name__ == '__main__':
    main()
