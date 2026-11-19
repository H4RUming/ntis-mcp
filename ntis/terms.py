"""과제 키워드 색인: 검색어와 뜻이 가까운, 실제 과제에 쓰인 말을 찾아 준다.

NTIS 검색은 낱말 일치라 '드론'으로는 '무인기'라고 쓴 과제를 찾지 못한다. 서로 바꿔 쓰는 말은 한 과제에 같이 나오지도
않아서(실측: '드론' 상위 100건의 키워드에 무인기·UAV 가 없다) 통계만으로는 이을 수 없다. 그래서 과제에 연구자가 붙인
한글·영문 키워드를 모아 임베딩해 두고, 검색어와 가까운 키워드를 쓰인 횟수와 함께 내놓는다. 고르는 것은 쓰는 쪽이다
(가까운 말이 곧 같은 말은 아니다: '양자컴퓨팅' 옆에는 '양자센서'도 있다).

  python -m ntis.terms harvest --years 2026 2025 2024     # 키워드 모으기(이어서 하기 가능)
  python -m ntis.terms build                              # 임베딩해 색인 만들기
  python -m ntis.terms similar 드론 양자컴퓨팅              # 확인

NTIS 에서 받아 두는 것은 키워드와 쓰인 횟수뿐이다(과제 자료는 쌓지 않는다). 임베딩은 설정의 `[terms] embed_url`
(OpenAI 호환 /v1/embeddings)에 맡기므로 이 저장소에는 모델도 GPU 코드도 없다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
from urllib import request

import appconfig

from .client import Client, NtisError

DEFAULT_DIR = Path(__file__).resolve().parent.parent / 'data' / 'terms'
PAGE = 100                      # NTIS 가 한 번에 주는 최대 건수
MIN_LENGTH, MAX_LENGTH = 2, 40
_SPLIT = re.compile(r'[,;，；、\n]+')
_EDGE = ' \t\r\n.·•*"\'“”‘’()[]<>:-_/'


def split_keywords(text: str) -> list[str]:
    """키워드 칸의 글을 낱낱의 키워드로 나눈다. 너무 짧거나 길거나 숫자뿐인 것은 버린다."""
    out = []
    for part in _SPLIT.split(str(text or '')):
        word = ' '.join(part.split()).strip(_EDGE)
        if MIN_LENGTH <= len(word) <= MAX_LENGTH and not word.replace(' ', '').isdigit() and word not in out:
            out.append(word)
    return out


def _text(value: Any, *path: str) -> str:
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, dict):
        value = value.get('text') or ''
    return str(value or '')


def _save(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def harvest(client: Client, directory: Path, years: list[int], max_requests: int | None = None,
            log: Callable[[str], None] = print) -> dict[str, Any]:
    """연도마다 과제를 최근 것부터 넘기며 키워드를 센다. 멈췄다 다시 돌리면 이어서 한다.

    keywords.json: {접은 글자: [보이는 글자, 'ko'|'en', 쓰인 과제 수]}, harvest.json: 연도별 진행 위치.
    """
    directory.mkdir(parents=True, exist_ok=True)
    counts: dict[str, list] = _load(directory / 'keywords.json', {})
    state: dict[str, Any] = _load(directory / 'harvest.json', {'years': {}, 'records': 0, 'requests': 0})
    made, stopped = 0, ''

    def keep() -> None:
        _save(directory / 'keywords.json', counts)
        _save(directory / 'harvest.json', state)

    for year in years:
        progress = state['years'].setdefault(str(year), {'next': 1, 'total': None, 'done': False})
        while not progress['done']:
            if max_requests is not None and made >= max_requests:
                stopped = 'max_requests'
                break
            try:
                page = client.search('project', '', year_from=year, year_to=year, sort='recent', start=progress['next'], count=PAGE)
            except NtisError as exc:
                stopped = f'{exc.kind}: {exc}'
                break
            made += 1
            state['requests'] += 1
            progress['total'] = page.total
            for hit in page.hits:
                for language, field in (('ko', 'Korean'), ('en', 'English')):
                    for word in split_keywords(_text(hit, 'Keyword', field)):
                        entry = counts.setdefault(word.casefold(), [word, language, 0])
                        entry[2] += 1
            state['records'] += len(page.hits)
            progress['next'] += len(page.hits)
            progress['done'] = not page.hits or progress['next'] > page.total
            if made % 20 == 0:
                keep()
                log(f'{year}: {progress["next"] - 1}/{page.total}건, 키워드 {len(counts)}종')
        if stopped:
            break
    keep()
    return {'requests': made, 'records': state['records'], 'keywords': len(counts), 'stopped': stopped,
            'years': {year: {key: value[key] for key in ('next', 'total', 'done')} for year, value in state['years'].items()}}


# 키워드 칸에 내용 대신 적혀 있는 말(색인에 넣지 않는다)
_NOISE = {'secret project', '추후입력', '추후 입력', '해당없음', '해당 없음', '없음', '미정', 'none', 'n/a', 'na', 'tbd', 'null', '기타', 'etc'}


class Settings:
    """설정의 [terms]: embed_url(OpenAI 호환 /v1/embeddings 주소, 비우면 색인을 쓰지 않는다), model, dimensions, data."""

    def __init__(self, config: str | os.PathLike | None = None):
        values = appconfig.section('terms', config)
        self.embed_url = str(values.get('embed_url') or '').strip()
        self.model = str(values.get('model') or '').strip()
        self.dimensions = int(values.get('dimensions') or 256)
        self.floor = float(values.get('floor') or 0.0)          # 이보다 덜 가까운 말은 내놓지 않는다(값은 모델마다 다르다)
        data = str(values.get('data') or '').strip()
        self.directory = (appconfig.config_path(config).parent / data) if data else DEFAULT_DIR


def embed(settings: Settings, texts: list[str], timeout: float = 120.0):
    """글들을 벡터(행마다 길이 1)로. 임베딩 서버에 묻는다."""
    import base64
    import numpy
    body = {'input': texts, 'dimensions': settings.dimensions, 'encoding_format': 'base64', 'task': 'similarity'}
    if settings.model:
        body['model'] = settings.model
    call = request.Request(settings.embed_url, data=json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with request.urlopen(call, timeout=timeout) as response:
        rows = sorted(json.loads(response.read())['data'], key=lambda row: row['index'])
    vectors = numpy.stack([numpy.frombuffer(base64.b64decode(row['embedding']), dtype='<f4') if isinstance(row['embedding'], str)
                           else numpy.asarray(row['embedding'], dtype='<f4') for row in rows])
    vectors = vectors[:, :settings.dimensions]
    return vectors / numpy.maximum(numpy.linalg.norm(vectors, axis=1, keepdims=True), 1e-9)


def build(settings: Settings, min_count: int = 2, batch: int = 2048, log: Callable[[str], None] = print) -> dict[str, Any]:
    """모아 둔 키워드 가운데 min_count 번 이상 쓰인 것을 임베딩해 색인을 만든다(vectors.npy, terms.json, index.json)."""
    import numpy
    if not settings.embed_url:
        raise SystemExit('설정 [terms] embed_url 이 없습니다')
    directory = settings.directory
    counts = _load(directory / 'keywords.json', {})
    chosen = sorted((entry for key, entry in counts.items() if entry[2] >= min_count and key not in _NOISE), key=lambda entry: -entry[2])
    if not chosen:
        raise SystemExit('색인할 키워드가 없습니다. 먼저 harvest 를 돌리세요.')
    parts, started = [], time.time()
    for start in range(0, len(chosen), batch):
        for attempt in range(4):
            try:
                parts.append(embed(settings, [entry[0] for entry in chosen[start:start + batch]]).astype('float16'))
                break
            except OSError:                  # 임베딩 서버가 잠깐 못 받는 때(GPU 메모리 부족 등): 조금 쉬고 다시 묻는다
                if attempt == 3:
                    raise
                time.sleep(10 * (attempt + 1))
        log(f'{min(start + batch, len(chosen))}/{len(chosen)}')
    vectors = numpy.concatenate(parts)
    numpy.save(directory / 'vectors.tmp.npy', vectors)
    os.replace(directory / 'vectors.tmp.npy', directory / 'vectors.npy')
    _save(directory / 'terms.json', chosen)
    info = {'terms': len(chosen), 'dimensions': int(vectors.shape[1]), 'min_count': min_count, 'model': settings.model,
            'built_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'seconds': round(time.time() - started, 1)}
    _save(directory / 'index.json', info)
    return info


def _squash(text: str) -> str:
    return ''.join(text.casefold().split())


class Index:
    """키워드 색인에서 가까운 말을 찾는다. 색인이나 임베딩 서버가 없으면 조용히 빈 결과를 낸다(검색 자체는 막지 않는다)."""

    def __init__(self, config: str | os.PathLike | None = None, settings: Settings | None = None):
        self.settings = settings or Settings(config)
        self._loaded: tuple[float, Any, list] | None = None
        self._queries: dict[str, Any] = {}
        self.problem = ''

    def _load(self):
        path = self.settings.directory / 'vectors.npy'
        try:
            stamp = path.stat().st_mtime
        except OSError:
            self.problem = '키워드 색인이 아직 없습니다'
            return None
        if self._loaded is None or self._loaded[0] != stamp:              # 색인을 다시 만들면 재시작 없이 새로 읽는다
            import numpy
            self._loaded = (stamp, numpy.load(path).astype('float32'), _load(self.settings.directory / 'terms.json', []))
            self._queries.clear()
        return self._loaded

    def similar(self, words: list[str], limit: int = 8, floor: float | None = None) -> list[dict[str, Any]]:
        """words 와 가까운 키워드를 가까운 순으로. 이미 words 에 있는 말(띄어쓰기만 다른 것 포함)과 그 말을 낱말로 품은 더 좁은 말은 뺀다."""
        words = [word for word in dict.fromkeys(' '.join(str(word).replace('"', ' ').split()) for word in words) if word and not word.startswith('!')][:12]
        floor = self.settings.floor if floor is None else floor
        if not words or not self.settings.embed_url:
            return []
        loaded = self._load()
        if loaded is None:
            return []
        _, vectors, terms = loaded
        missing = [word for word in words if word not in self._queries]
        if missing:
            try:
                for word, vector in zip(missing, embed(self.settings, missing, timeout=20.0)):
                    self._queries[word] = vector
            except Exception as exc:                 # 임베딩 서버가 내려가 있어도 검색은 계속돼야 한다
                self.problem = f'임베딩 서버에 묻지 못했습니다: {type(exc).__name__}'
                return []
            while len(self._queries) > 2048:
                self._queries.pop(next(iter(self._queries)))
        self.problem = ''
        import numpy
        scores = (vectors @ numpy.stack([self._queries[word] for word in words]).T).max(axis=1)      # 낱말들 가운데 가장 가까운 것 기준
        # 묻는 말이나 이미 고른 말을 품은 더 긴 말은 뺀다('드론'에 '배송드론', '양자컴퓨터'에 '중성원자 양자컴퓨터'): 검색어를 넓혀 주지 못하고
        # 자리만 차지한다. 남은 것은 가까운 순으로 하되, 많이 쓰이는 말을 조금 앞에 둔다(세 번 쓰인 오타보다 수백 번 쓰인 표기가 쓸모 있다).
        taken = [_squash(word) for word in words]
        candidates = []
        for position in numpy.argsort(-scores)[:400]:
            display, _, count = terms[position]
            score = float(scores[position])
            if score < floor:
                break
            key = _squash(display)
            if any(known in key for known in taken):
                continue
            taken.append(key)
            candidates.append((score + 0.004 * math.log2(max(count, 1)), display, count, score))
            if len(candidates) >= limit * 6:
                break
        candidates.sort(key=lambda item: -item[0])
        return [{'term': display, 'uses': count, 'score': round(score, 3)} for _, display, count, score in candidates[:limit]]


HINT_NOTE = ('related_terms 는 과제 키워드 가운데 검색어와 가까운 말입니다(uses = 그 말을 키워드로 단 과제 수). 후보일 뿐이라 뜻이 다른 말도 '
             '섞여 있습니다. 검색어와 같은 것을 가리키는데 빠뜨린 말이 있으면 `|` 로 더해 다시 찾으세요.')


def hints(index: Index, query_text: str = '', and_text: str = '', limit: int = 10) -> dict[str, Any]:
    """검색 결과에 붙일 가까운 말. 검색어 칸마다 따로 찾는다. 색인을 못 쓰면 빈 dict."""
    found = {}
    for name, text in (('query_text', query_text), ('and_text', and_text)):
        words = [part for part in (' '.join(piece.split()) for piece in str(text or '').split('|')) if part]
        related = index.similar(words, limit) if words else []
        if related:
            found[name] = [{'term': hit['term'], 'uses': hit['uses']} for hit in related]
    return {'related_terms': found, 'related_terms_note': HINT_NOTE} if found else {}


def main() -> None:
    parser = argparse.ArgumentParser(description='NTIS 과제 키워드 색인')
    parser.add_argument('--config', type=Path, default=None)
    parser.add_argument('--data', type=Path, default=DEFAULT_DIR)
    commands = parser.add_subparsers(dest='command', required=True)
    gather = commands.add_parser('harvest', help='과제 키워드를 모은다(이어서 하기 가능)')
    gather.add_argument('--years', type=int, nargs='+', required=True)
    gather.add_argument('--max-requests', type=int, default=None)
    make = commands.add_parser('build', help='모은 키워드를 임베딩해 색인을 만든다')
    make.add_argument('--min-count', type=int, default=2)
    look = commands.add_parser('similar', help='가까운 키워드를 찾아본다')
    look.add_argument('words', nargs='+')
    look.add_argument('--limit', type=int, default=12)
    args = parser.parse_args()
    if args.command in ('build', 'similar'):
        settings = Settings(args.config)
        if args.data != DEFAULT_DIR:
            settings.directory = args.data
        if args.command == 'build':
            print(json.dumps(build(settings, args.min_count, log=lambda line: print(time.strftime('%H:%M:%S'), line, flush=True)), ensure_ascii=False))
        else:
            index = Index(settings=settings)
            for word in args.words:
                print(word, '→', ', '.join(f'{hit["term"]}({hit["uses"]}, {hit["score"]})' for hit in index.similar([word], args.limit)) or index.problem)
        return
    if args.command == 'harvest':
        started = time.time()
        summary = harvest(Client(args.config), args.data, args.years, args.max_requests,
                          log=lambda line: print(time.strftime('%H:%M:%S'), line, flush=True))
        summary['seconds'] = round(time.time() - started)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        sys.exit(1 if summary['stopped'] not in ('', 'max_requests') else 0)


if __name__ == '__main__':
    main()
