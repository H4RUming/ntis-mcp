"""NTIS: 설정 파일의 승인키, 검색 조건 조립, 응답 해석, 대외 제공 항목 제한, 집계, 오류 처리, MCP 도구, 실키 점검 절차."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from urllib import parse
from xml.sax.saxutils import escape

import appconfig
from ntis import check, client as ntis_client, query
from ntis.client import Client, NtisError, Settings

try:
    from ntis import mcp_server
except ImportError:  # MCP SDK 가 없는 환경
    mcp_server = None

KEY = 'TESTKEY0123456789'
MARK = '<span class="search_word">{}</span>'
# 가짜 NTIS 가 가진 과제: (번호, 과제명, 연도, 부처, 수행기관)
PROJECTS = [(f'17110{n:05d}', title, year, ministry, agency) for n, (title, year, ministry, agency) in enumerate([
    ('인공지능 반도체 설계 기술 개발', 2025, '과학기술정보통신부', '한국과학기술원'),
    ('인공지능 기반 신약 후보 탐색', 2025, '보건복지부', '서울대학교'),
    ('제조 현장 인공지능 품질 검사', 2025, '산업통상부', '한국생산기술연구원'),
    ('인공지능 반도체 설계 기술 개발', 2024, '과학기술정보통신부', '한국과학기술원'),
    ('농업용 드론 방제 인공지능', 2024, '농림축산식품부', '전북대학교'),
    ('차세대 이차전지 소재 (AI 활용)', 2024, '과학기술정보통신부', '한국화학연구원'),
    ('인공지능 반도체 설계 기술 개발', 2023, '과학기술정보통신부', '한국과학기술원'),
    ('양자 인공지능 알고리즘', 2022, '과학기술정보통신부', '서울대학교'),
])]


def project_xml(number, title, year, ministry, agency, word):
    marked = escape(title.replace(word, MARK.format(word)))
    return f'''<HIT NO="1"><ProjectNumber>{number}</ProjectNumber>
<ProjectTitle><Korean>{marked}</Korean><English>Project {number}</English></ProjectTitle>
<Manager><Name>홍길동</Name></Manager><Researchers><Name>김연구;이연구</Name><ManCount>1</ManCount><WomanCount>1</WomanCount></Researchers>
<Goal><Full>세계 수준의 {escape(MARK.format('인공지능'))} 기술 확보</Full><Teaser /></Goal>
<Abstract><Full>{'연구 내용 ' * 80}</Full><Teaser /></Abstract>
<Effect><Full>산업 경쟁력 강화</Full><Teaser /></Effect>
<Keyword><Korean>인공지능,반도체</Korean><English>AI,semiconductor</English></Keyword>
<OrderAgency><Name>한국연구재단</Name></OrderAgency><ResearchAgency><Name>{agency}</Name></ResearchAgency>
<BudgetProject><Name>차세대지능형반도체기술개발</Name></BudgetProject><BigprojectTitle>설계</BigprojectTitle>
<Ministry><Name>{ministry}</Name></Ministry><ProjectYear>{year}</ProjectYear>
<ProjectPeriod><Start>{year}0101</Start><End>{year}1231</End><TotalStart>20230401</TotalStart><TotalEnd>20271231</TotalEnd></ProjectPeriod>
<ScienceClass type="new" sequence="1"><Large code="EE">전기/전자</Large><Medium code="EE01">반도체소자</Medium><Small code="EE0101">설계</Small></ScienceClass>
<PerformAgent code="1">대학</PerformAgent><GovernmentFunds>500000000</GovernmentFunds><TotalFunds>650000000</TotalFunds>
<SeriesProject><PrePjtId>1711000000</PrePjtId></SeriesProject></HIT>'''


class FakeNtis:
    """요청 주소를 풀어 PROJECTS 에서 답을 만드는 가짜 서버. 조건 문법이 client 와 어긋나면 건수가 달라진다."""

    def __init__(self):
        self.calls = []
        self.fail = None

    def __call__(self, url, data, timeout):
        split = parse.urlsplit(url)
        params = dict(parse.parse_qsl(data.decode() if data else split.query, keep_blank_values=True))
        self.calls.append((split.path.rsplit('/', 1)[-1], params))
        if self.fail:
            raise self.fail
        endpoint = split.path.rsplit('/', 1)[-1]
        if params.get('apprvKey') != KEY and endpoint != 'targetSearch':
            return f"<?xml version='1.0' encoding='UTF-8' ?><error>유효한 인증키가 아닙니다. 인증키 : {params.get('apprvKey')}</error>".encode()
        if endpoint == 'targetSearch' and params.get('apprvKey') != KEY:
            return f"<ntis><status><recode>3</recode><remsg>유효한 인증키가 아닙니다. 인증키 : {params.get('apprvKey')}</remsg></status></ntis>".encode()
        if endpoint == 'targetSearch':
            if params.get('rqstSearchCd') == '999999':
                return b'<ntis><status><recode>101</recode><remsg>no data</remsg></status></ntis>'
            if params.get('rqstSlctCd') == 'NTIS002':
                rows = [('060000', '첨단소재', 'null', ''), ('060200', '금속', '금속소재의 구조', '060000'), ('090000', '인공지능', 'null', '')]
                sets = ''.join(f'<dataset num="{n}"><cd>{cd}</cd><cdNm><![CDATA[{nm}]]></cdNm><cdExplan><![CDATA[{ex}]]></cdExplan>'
                               f'<cdNmEng>null</cdNmEng><upperCd>{up}</upperCd></dataset>' for n, (cd, nm, ex, up) in enumerate(rows, 1))
            else:
                rows = [('NA', '수학', 'Mathematics', '01'), ('NA01', '대수학', 'null', 'NA'), ('NA0101', '선형대수', 'Linear algebra', 'NA01'),
                        ('EE', '전기/전자', 'null', '03')]
                sets = ''.join(f'<dataset num="{n}"><classCd>{cd}</classCd><kindCd>01</kindCd><kindCdNm>자연</kindCdNm><classCdNm>{nm}</classCdNm>'
                               f'<classCdNmEng>{en}</classCdNmEng><classCdExplan>null</classCdExplan><upperClassCd>{up}</upperClassCd></dataset>'
                               for n, (cd, nm, en, up) in enumerate(rows, 1))
            return f'<ntis><status><recode>100</recode><remsg>정상</remsg></status><contents>{sets}</contents></ntis>'.encode()
        if endpoint == 'public_project':
            return self.projects(params)
        return self.canned(endpoint, params)

    def projects(self, params):
        word = params.get('query', '')
        rows = [row for row in PROJECTS if not word or word in row[1]] if params.get('searchField') == 'TI' else list(PROJECTS)
        for condition in filter(None, params.get('addQuery', '').split('&')):
            name, _, value = condition.partition('=')
            if name == 'PY':
                for part in value.split(','):
                    year, _, how = part.partition('/')
                    rows = [r for r in rows if {'SAME': r[2] == int(year), 'MORE': r[2] >= int(year), 'UNDER': r[2] <= int(year)}[how]]
            elif name == 'PB02':
                rows = [r for r in rows if r[3] == value]
            elif name == 'PB01':
                rows = [r for r in rows if r[4] == value]
            elif name == 'CN':
                rows = [r for r in rows if r[0] == value]
            else:
                rows = []
        if params.get('sortby') == 'DATE/ASC':
            rows.sort(key=lambda r: r[2])
        elif params.get('sortby') == 'DATE/DESC':
            rows.sort(key=lambda r: -r[2])
        navigation = ''
        if int(params.get('naviCount', 0)):
            for name, column, code in (('projectyear', 2, ''), ('ministryname', 3, '▦1721000'), ('leadagency', 4, '▦B350021')):
                counts = {}
                for row in ([] if name == 'leadagency' and 'PY=2022/SAME' in params.get('addQuery', '') else rows):
                    counts[row[column]] = counts.get(row[column], 0) + 1
                top = sorted(counts.items(), key=lambda item: -item[1])[:int(params['naviCount'])]
                navigation += (f'<NAVIGATIONENTRY NAME="{name}"><NAVIGATIONELEMENTS COUNT="{len(top)}">'
                               + ''.join(f'<NAVIGATIONELEMENT NAME="{value}{code}" COUNT="{count}" />' for value, count in top)
                               + '</NAVIGATIONELEMENTS></NAVIGATIONENTRY>')
        start, size = int(params.get('startPosition', 1)), int(params.get('displayCount', 10))
        page = rows[start - 1:start - 1 + size]
        hits = ''.join(project_xml(*row, word or '인공지능') for row in page)
        return (f'<?xml version="1.0" encoding="UTF-8"?><RESULT><QUERYTRANSFORMS><ORIGINALQUERY>{escape(word)}</ORIGINALQUERY>'
                f'</QUERYTRANSFORMS><COUNTLIST><COLCOUNT NAME="project">{len(rows)}</COLCOUNT></COUNTLIST>'
                f'<NAVIGATION>{navigation}</NAVIGATION><TOTALHITS>{len(rows)}</TOTALHITS><STARTPOSITION>{start}</STARTPOSITION>'
                f'<HITS>{len(page)}</HITS><SEARCHTIME>0.01</SEARCHTIME><RESULTSET>{hits}</RESULTSET></RESULT>').encode()

    def canned(self, endpoint, params):
        collection = params.get('collection', '')
        if collection == 'rpaper':
            hit = ('<HIT NO="1" SITID="PAP"><ResultID>CNL-1</ResultID><ResultTitle>&lt;span class="search_word"&gt;인공지능&lt;/span&gt; 논문</ResultTitle>'
                   '<JournalName>한국학회지</JournalName><Author>박용민</Author><Abstract><Full>초록 전체</Full><Teaser>초록 일부</Teaser></Abstract>'
                   '<PubYear>2024</PubYear><Year>2024</Year><ProjectID>1711000000</ProjectID><PaperType code="104">국내학술대회발표논문집</PaperType>'
                   '<SciType>02</SciType><NationType>국내</NationType><ProjectYear>2024</ProjectYear><BudgetProject>기초연구</BudgetProject>'
                   '<MinistryName code="1721000">과학기술정보통신부</MinistryName><ProjectTitle>모과제</ProjectTitle>'
                   '<PerformAgency code="B350021">한국과학기술원</PerformAgency></HIT>')
        elif collection == 'rpatent':
            hit = ('<HIT NO="1" SITID="PAT"><Year>2023</Year><ResultID>PAT-1</ResultID><ResultTitle>인공지능 장치</ResultTitle>'
                   '<RegistCountry code="KR">대한민국</RegistCountry><RegistNumber>10-2023-0000001</RegistNumber><Registrant>한국과학기술원</Registrant>'
                   '<RegistType code="1">출원</RegistType><IprType code="1">특허</IprType><ProjectID>1711000000</ProjectID></HIT>')
        elif endpoint == 'rresearchpdf':
            hit = ('<HIT NO="1"><TermSn>9</TermSn><PublicationYear>2022</PublicationYear><ResearchPublicNo>TRKO202200001</ResearchPublicNo>'
                   '<PublicationAgency>한국연구재단</PublicationAgency><ResultTitle><Korean><span class="search_word">인공지능</span> 보고서</Korean><English>AI report</English></ResultTitle>'
                   '<Abstract><Korean>보고서 초록</Korean><English>abstract</English></Abstract><Keyword><Korean>인공지능</Korean><English>AI</English></Keyword>'
                   '<Contents>1. 서론 2. 본론</Contents><DocUrl>https://scienceon.kisti.re.kr/commons/util/originalView.do?dbt=TRKO&cn=TRKO202200001</DocUrl><ProjectNumber>1711000000</ProjectNumber>'
                   '<LeadAgency>서울대학교</LeadAgency><ManagerName>홍길동</ManagerName></HIT>')
        else:
            hit = ('<HIT NO="1"><TermSn>2020063972</TermSn><KorWord>간접비</KorWord><EngWord>indirect cost</EngWord><MainAbrv>IC</MainAbrv>'
                   '<SntStdCls>과학기술정책 > 연구관리</SntStdCls><RelWord>직접비</RelWord><TermDctn>연구에 간접적으로 드는 비용</TermDctn><TermCls>용어</TermCls><Extra>x</Extra></HIT>')
        navigation = ('<NAVIGATIONENTRY NAME="year"><NAVIGATIONELEMENTS COUNT="2"><NAVIGATIONELEMENT NAME="2023" COUNT="4" />'
                      '<NAVIGATIONELEMENT NAME="2024" COUNT="7" /></NAVIGATIONELEMENTS></NAVIGATIONENTRY>') if int(params.get('naviCount', 0)) else ''
        return (f'<RESULT><COUNTLIST><COLCOUNT NAME="{collection}">11</COLCOUNT></COUNTLIST><NAVIGATION>{navigation}</NAVIGATION>'
                f'<TOTALHITS>11</TOTALHITS><STARTPOSITION>1</STARTPOSITION><HITS>1</HITS><RESULTSET>{hit}</RESULTSET></RESULT>').encode()


def make(external_use=True, key=KEY, **extra):
    fake = FakeNtis()
    return Client(fetch=fake, settings=Settings(api_key=key, external_use=external_use, delay=0.0, **extra)), fake


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'config.toml'

    def write(self, text):
        self.path.write_text(text, encoding='utf-8')
        self.stamp = getattr(self, 'stamp', self.path.stat().st_mtime) + 5
        os.utime(self.path, (self.stamp, self.stamp))      # 연달아 고쳐도 수정 시각이 달라지게

    def test_key_comes_from_file_and_environment_wins(self):
        self.assertEqual(Settings.load(self.path, env={}).api_key, '')
        self.write('[ntis]\napi_key = "  from-file  "\nexternal_use = false\ndelay = 0\n')
        settings = Settings.load(self.path, env={})
        self.assertEqual((settings.api_key, settings.external_use, settings.delay), ('from-file', False, 0.0))
        self.assertEqual(settings.key_source, str(self.path))
        settings = Settings.load(self.path, env={'NTIS_API_KEY': 'from-env'})
        self.assertEqual((settings.api_key, settings.key_source), ('from-env', '환경변수 NTIS_API_KEY'))

    def test_bad_config_is_explained(self):
        self.write('[ntis]\nexternal_use = "yes"\n')
        with self.assertRaisesRegex(ValueError, 'external_use'):
            Settings.load(self.path, env={})
        self.write('[ntis\napi_key = 1')
        with self.assertRaisesRegex(ValueError, '문법 오류'):
            Settings.load(self.path, env={})
        self.write('ntis = "x"\n')
        with self.assertRaisesRegex(ValueError, r'\[ntis\]'):
            appconfig.section('ntis', self.path)

    def test_registering_the_key_takes_effect_without_restart(self):
        fake = FakeNtis()
        env, os.environ['NTIS_API_KEY'] = os.environ.get('NTIS_API_KEY'), ''
        self.addCleanup(lambda: os.environ.pop('NTIS_API_KEY') if env is None else os.environ.__setitem__('NTIS_API_KEY', env))
        self.write('[ntis]\napi_key = ""\ndelay = 0\n')
        client = Client(self.path, fetch=fake)
        with self.assertRaises(NtisError) as raised:
            query.search_projects(client, '인공지능')
        self.assertEqual(raised.exception.kind, 'no_key')
        self.assertIn(str(self.path), str(raised.exception))
        self.assertEqual(fake.calls, [])                      # 키 없이 요청을 보내지 않는다
        state = query.status(client, self.path)
        self.assertFalse(state['key_registered'])
        self.assertIn('api_key', state['how_to_register'])
        self.write(f'[ntis]\napi_key = "{KEY}"\ndelay = 0\n')
        self.assertEqual(query.search_projects(client, '인공지능')['total'], len(PROJECTS))
        state = query.status(client, self.path, probe=True)
        self.assertEqual((state['key_registered'], state['probe']['ok']), (True, True))
        self.assertNotIn(KEY, repr(state))

    def test_world_readable_config_is_flagged(self):
        self.write(f'[ntis]\napi_key = "{KEY}"\n')
        self.path.chmod(0o644)
        self.assertIn('chmod 600', appconfig.permission_warning(self.path))
        self.path.chmod(0o600)
        self.assertEqual(appconfig.permission_warning(self.path), '')


class SearchTests(unittest.TestCase):
    def test_request_carries_the_documented_parameters(self):
        client, fake = make()
        query.search_projects(client, '인공지능 반도체', search_in='title', year_from=2023, year_to=2025,
                              ministry='과학기술정보통신부', sort='recent', limit=5, offset=10)
        endpoint, params = fake.calls[-1]
        self.assertEqual(endpoint, 'public_project')
        self.assertEqual(params, {'apprvKey': KEY, 'collection': 'project', 'query': '인공지능 반도체', 'searchField': 'TI',
                                  'addQuery': 'PY=2023/MORE,2025/UNDER&PB02=과학기술정보통신부', 'sortby': 'DATE/DESC',
                                  'startPosition': '11', 'displayCount': '5'})
        self.assertEqual(ntis_client.year_condition(2024, 2024), 'PY=2024/SAME')
        self.assertEqual(ntis_client.year_condition(2021, None), 'PY=2021/MORE')
        self.assertEqual(ntis_client.year_condition(None, 2020), 'PY=2020/UNDER')
        query.search_results(client, 'paper', '인공지능', year_from=2024, year_to=2024)
        self.assertEqual(fake.calls[-1][1]['addQuery'], 'DBT=PAP&PY=2024/SAME')   # 성과 검색은 DBT 가 필수

    def test_external_use_keeps_only_permitted_fields(self):
        client, _ = make(external_use=True)
        result = query.search_projects(client, '인공지능', limit=2)
        self.assertEqual((result['total'], result['count'], result['next_offset']), (len(PROJECTS), 2, 2))
        item = result['items'][0]
        self.assertEqual(set(item), {'project_number', 'title', 'title_en', 'year', 'ministry', 'program', 'organization',
                                     'manager', 'total_period', 'year_period', 'government_funds', 'total_funds', 'summary',
                                     'keywords', 'keywords_en', 'url'})
        self.assertEqual(item['title'], '인공지능 반도체 설계 기술 개발')          # 강조 태그가 걷힌다
        self.assertEqual((item['year'], item['total_funds'], item['total_period']), (2025, 650000000, '2023-04-01 ~ 2027-12-31'))
        self.assertLessEqual(len(item['summary']), query.SUMMARY_CHARS + 1)      # 연구내용은 250자 내외만
        self.assertEqual(item['url'], 'https://www.ntis.go.kr/project/pjtInfo.do?pjtId=' + item['project_number'])
        self.assertIn('허용한 항목', result['note'])

        internal, _ = make(external_use=False)
        full = query.get_project(internal, item['project_number'])
        self.assertEqual((full['goal'], full['funding_agency'], full['researchers']), ('세계 수준의 인공지능 기술 확보', '한국연구재단', '김연구;이연구'))
        self.assertEqual(full['science_class'], ['전기/전자 > 반도체소자 > 설계'])
        self.assertNotIn('goal', query.get_project(client, item['project_number']))

    def test_last_page_has_no_next_offset(self):
        client, _ = make()
        result = query.search_projects(client, '인공지능', limit=50, offset=6)
        self.assertEqual((result['count'], result['next_offset']), (2, None))
        self.assertEqual(query.search_projects(client, '인공지능', limit=500)['count'], len(PROJECTS))

    def test_counts_use_navigation(self):
        client, fake = make()
        by_year = query.count(client, 'project', 'year', '인공지능')
        self.assertEqual(by_year['rows'], [{'name': '2025', 'count': 3}, {'name': '2024', 'count': 3}, {'name': '2023', 'count': 1}, {'name': '2022', 'count': 1}])
        self.assertEqual((by_year['total'], by_year['listed_sum'], by_year['complete']), (8, 8, True))
        self.assertEqual(fake.calls[-1][1]['displayCount'], '1')                 # 목록은 받지 않는다
        by_ministry = query.count(client, 'project', 'ministry', '인공지능', top=2)
        self.assertEqual(by_ministry['rows'][0], {'name': '과학기술정보통신부', 'count': 5})   # 이름▦코드에서 이름만
        self.assertFalse(by_ministry['complete'])
        self.assertIn('상위 2개', by_ministry['caution'])
        filtered = query.count(client, 'project', 'organization', '', year_from=2024, year_to=2024, ministry='과학기술정보통신부')
        self.assertEqual(filtered['total'], 2)
        with self.assertRaisesRegex(NtisError, '집계 기준'):
            query.count(client, 'project', 'journal', '인공지능')
        empty = query.count(client, 'project', 'organization', '인공지능', year_from=2022, year_to=2022)   # NTIS 가 집계를 비워 보낸 경우
        self.assertEqual((empty['total'], empty['rows'], empty['complete']), (1, [], False))
        self.assertIn('집계를 주지 않았습니다', empty['caution'])
        self.assertEqual(query.count(client, 'paper', 'year', '인공지능')['rows'][0], {'name': '2024', 'count': 7})

    def test_results_reports_terms_and_codes(self):
        client, fake = make()
        paper = query.search_results(client, 'paper', '인공지능')['items'][0]
        self.assertEqual((paper['title'], paper['sci'], paper['paper_type'], paper['ministry'], paper['organization']),
                         ('인공지능 논문', '비SCI', '국내학술대회발표논문집', '과학기술정보통신부', '한국과학기술원'))
        self.assertTrue(paper['project_url'].endswith('pjtId=1711000000'))
        patent = query.search_results(client, 'patent', '인공지능', project_number='1711000000')['items'][0]
        self.assertEqual((patent['country'], patent['regist_type'], patent['number']), ('대한민국', '출원', '10-2023-0000001'))
        self.assertEqual(fake.calls[-1][1]['addQuery'], 'DBT=PAT&TI03=1711000000')
        report = query.search_reports(client, '인공지능')
        self.assertEqual((report['items'][0]['title'], report['items'][0]['url'], report['items'][0]['report_id']),
                         ('인공지능 보고서', 'https://scienceon.kisti.re.kr/commons/util/originalView.do?dbt=TRKO&cn=TRKO202200001', 'TRKO202200001'))
        term = query.lookup_terms(client, '간접비')['items'][0]
        self.assertEqual((term['term'], term['abbreviation'], term['definition'], term['type'], term['field'], term['Extra']),
                         ('간접비', 'IC', '연구에 간접적으로 드는 비용', '용어', '과학기술정책 > 연구관리', 'x'))
        self.assertNotIn('collection', fake.calls[-1][1])
        codes = query.lookup_codes(client, 'strategic', '060200')
        self.assertEqual(codes['items'], [{'code': '060200', 'name': '금속', 'parent': '060000', 'description': '금속소재의 구조'}])
        endpoint, params = fake.calls[-1]
        self.assertEqual((endpoint, params['rqstSlctCd']), ('targetSearch', 'NTIS002'))
        top = query.lookup_codes(client, 'strategic')                        # 조건이 없으면 맨 위 단계만, 'null' 은 빈 값
        self.assertEqual(top['items'], [{'code': '060000', 'name': '첨단소재'}, {'code': '090000', 'name': '인공지능'}])
        self.assertEqual(query.lookup_codes(client, 'strategic', '060000')['children'], [{'code': '060200', 'name': '금속'}])
        standard = query.lookup_codes(client, 'standard')
        self.assertEqual([item['code'] for item in standard['items']], ['NA', 'EE'])
        self.assertEqual(query.lookup_codes(client, 'standard', parent='NA')['items'][0],
                         {'code': 'NA01', 'name': '대수학', 'parent': 'NA', 'area': '자연'})
        self.assertEqual([item['code'] for item in query.lookup_codes(client, 'standard', name='linear')['items']], ['NA0101'])
        paged = query.lookup_codes(client, 'standard', name='수', limit=1)
        self.assertEqual((paged['total'], paged['count'], paged['next_offset']), (3, 1, 1))
        self.assertEqual(query.lookup_codes(client, 'standard', 'ZZ')['total'], 0)
        self.assertEqual(client.codes('standard', '999999'), [])
        with self.assertRaises(NtisError):
            query.search_results(client, 'equipment', '인공지능')

    def test_bad_input_is_rejected_before_any_request(self):
        client, fake = make()
        for call in (lambda: query.search_projects(client, ''),
                     lambda: query.search_projects(client, 'x', search_in='journal'),
                     lambda: query.search_projects(client, 'x', sort='newest'),
                     lambda: query.search_projects(client, 'x', ministry='부처&PY=2020/SAME'),
                     lambda: query.search_projects(client, 'x', year_from=2025, year_to=2020),
                     lambda: query.search_projects(client, 'x', year_from=20),
                     lambda: query.get_project(client, '17110/../x'),
                     lambda: query.lookup_codes(client, 'unknown'),
                     lambda: query.lookup_terms(client, ' ')):
            with self.assertRaises(NtisError) as raised:
                call()
            self.assertEqual(raised.exception.kind, 'input')
        self.assertEqual(fake.calls, [])

    def test_errors_never_expose_the_key(self):
        client, fake = make(key='WRONG-KEY-000')
        with self.assertRaises(NtisError) as raised:
            query.search_projects(client, '인공지능')
        self.assertEqual(raised.exception.kind, 'auth')
        self.assertNotIn('WRONG-KEY-000', str(raised.exception) + client.last_error)   # NTIS 가 문구에 실어 보낸 키도 가린다
        self.assertIn('***', str(raised.exception))
        with self.assertRaises(NtisError) as raised:
            query.lookup_codes(client, 'standard')
        self.assertNotIn('WRONG-KEY-000', str(raised.exception) + client.last_error)
        client, fake = make()
        fake.fail = RuntimeError(f'https://www.ntis.go.kr/x?apprvKey={KEY} failed')
        with self.assertRaises(NtisError) as raised:
            query.search_projects(client, '인공지능')
        self.assertEqual(raised.exception.kind, 'http')
        self.assertNotIn(KEY, str(raised.exception))
        self.assertNotIn(KEY, client.last_error)
        self.assertNotIn(KEY, repr(query.status(client)))
        for body, kind in ((b'<html>gateway timeout</html', 'parse'), (b'<other />', 'parse'),
                           ("<RESULT><verifyFlag>FALSE</verifyFlag><resCd>3</resCd><resMsg>API URL이 올바르지 않습니다.</resMsg></RESULT>".encode(), 'api'),
                           ('<error>예기치 않은 오류가 발생하였습니다.</error>'.encode(), 'api')):
            with self.assertRaises(NtisError) as raised:
                ntis_client.parse_search(body)
            self.assertEqual(raised.exception.kind, kind)

    def test_cache_reuses_answers_but_not_errors(self):
        client, fake = make(cache_ttl=600.0)
        query.search_projects(client, '인공지능')
        query.search_projects(client, '인공지능')
        self.assertEqual(len(fake.calls), 1)
        query.search_projects(client, '인공지능', offset=3)
        self.assertEqual(len(fake.calls), 2)
        client, fake = make(key='WRONG', cache_ttl=600.0)
        for _ in range(2):
            with self.assertRaises(NtisError):
                query.search_projects(client, '인공지능')
        self.assertEqual(len(fake.calls), 2)

    def test_live_check_passes_only_when_filters_agree_with_counts(self):
        lines = []
        client, _ = make()
        self.assertTrue(check.run(client, out=lines.append), '\n'.join(lines))
        self.assertIn('모두 통과', lines[-1])
        self.assertFalse(any(KEY in line for line in lines))
        original, ntis_client.CONDITION_JOIN = ntis_client.CONDITION_JOIN, ','
        self.addCleanup(setattr, ntis_client, 'CONDITION_JOIN', original)
        lines.clear()
        client, _ = make()
        self.assertFalse(check.run(client, out=lines.append))      # 조건을 잇는 문법이 틀리면 건수가 어긋나 잡힌다
        self.assertTrue(any(line.startswith('FAIL 조건 두 개 잇기') for line in lines), '\n'.join(lines))
        lines.clear()
        self.assertFalse(check.run(make(key='')[0], out=lines.append))
        self.assertIn('승인키가 없습니다', lines[0])


class McpTests(unittest.TestCase):
    @unittest.skipIf(mcp_server is None, 'MCP SDK is not installed in this interpreter')
    def test_tools(self):
        async def scenario():
            client, _ = make()
            server = mcp_server.create_server(client=client)
            tools = {tool.name: tool for tool in await server.list_tools()}
            self.assertEqual(set(tools), {'search_projects', 'count_projects', 'get_project', 'search_results', 'count_results',
                                          'search_reports', 'lookup_term', 'lookup_class_code', 'ntis_status'})
            self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools.values()))
            _, found = await server.call_tool('search_projects', {'query_text': '인공지능', 'year_from': 2025, 'year_to': 2025})
            self.assertEqual((found['total'], found['items'][0]['year']), (3, 2025))
            _, counted = await server.call_tool('count_projects', {'group_by': 'ministry', 'query_text': '인공지능', 'year_from': 2024})
            self.assertEqual((counted['total'], counted['rows'][0]['name']), (6, '과학기술정보통신부'))
            _, one = await server.call_tool('get_project', {'project_number': found['items'][0]['project_number']})
            self.assertEqual(one['title'], found['items'][0]['title'])
            _, papers = await server.call_tool('count_results', {'kind': 'paper', 'query_text': '인공지능'})
            self.assertEqual(papers['total'], 11)
            _, state = await server.call_tool('ntis_status', {})
            self.assertTrue(state['key_registered'])
            with self.assertRaisesRegex(Exception, '집계 기준'):
                await server.call_tool('count_projects', {'group_by': 'nothing', 'query_text': 'x'})
            keyless = mcp_server.create_server(client=make(key='')[0])
            with self.assertRaisesRegex(Exception, '승인키가 등록되지 않았습니다'):
                await keyless.call_tool('search_projects', {'query_text': '인공지능'})
            _, state = await keyless.call_tool('ntis_status', {})
            self.assertFalse(state['key_registered'])
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
