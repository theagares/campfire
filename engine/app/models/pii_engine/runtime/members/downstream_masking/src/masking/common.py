"""Shared, CPU-only schema, provenance and masking policy."""
from __future__ import annotations
import collections
import hashlib
import json
import math
import re
import unicodedata
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temp.replace(path)

def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]

def write_rows(path, values):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as f:
        for row in values: f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    temp.replace(path)

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def config(path):
    c = read(path); c['_path'] = str(Path(path).resolve())
    c['_root'] = str(ROOT)  # Configs may reside in nested per-detector suite folders.
    return c

def project(c, value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else Path(c['_root']) / p

def norm(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()

def mask_labels(graph):
    ids = [e['entity_id'] for e in graph['entities']]
    if len(ids) != len(set(ids)): raise ValueError('Duplicate entity IDs')
    incident = {i: [] for i in ids}; seen = set()
    for edge in graph['relations']:
        a, b = edge['entity'], edge['target_entity']
        if a not in incident or b not in incident or a == b: raise ValueError('Invalid relation endpoints')
        if edge['privacy_label'] not in ('PII', 'NON_PII'): raise ValueError('Invalid privacy label')
        key = (*sorted((a,b)), edge['relation'])
        if key in seen: raise ValueError('Duplicate undirected relation')
        seen.add(key)
        for i in (a,b): incident[i].append(edge['privacy_label'] == 'PII')
    return {i: not es or any(es) for i, es in incident.items()}

def validate_graph(g):
    labels = mask_labels(g)
    sentences = {s['sent_idx']: s for s in g['sentences']}
    if len(sentences) != len(g['sentences']): raise ValueError('Duplicate sentence IDs')
    expected = set(); seen = set()
    registry = {e['entity_id']: e for e in g['entities']}
    for edge in g['relations']:
        for field, typefield in [('entity','entity_type'), ('target_entity','target_entity_type')]:
            if typefield in edge and edge[typefield] != registry[edge[field]]['entity_type']:
                raise ValueError('Edge endpoint type mismatch')
    for e in g['entities']:
        if not e['mentions']: raise ValueError('Entity has no mentions')
        for m in e['mentions']:
            text = sentences[m['sent_idx']]['sentence']; b,t = m['begin'],m['end']
            if not (0 <= b < t <= len(text)) or text[b:t] != m['form']: raise ValueError('Invalid mention')
            key = (m['sent_idx'],b,t)
            if key in seen: raise ValueError('Duplicate registry mention')
            seen.add(key)
            if labels[e['entity_id']]: expected.add((*key,e['entity_type']))
    actual = set()
    for s in g['sentences']:
        bio = ['O'] * len(s['sentence'])
        for m in s['PII_set']:
            b,t = m['begin'],m['end']; kind=m['label']
            if not (0 <= b < t <= len(bio)) or s['sentence'][b:t] != m['form']: raise ValueError('Invalid PII_set')
            if any(v!='O' for v in bio[b:t]): raise ValueError('Overlapping masking spans')
            bio[b:t] = ['B-'+kind] + ['I-'+kind]*(t-b-1)
            actual.add((s['sent_idx'],b,t,kind))
        if bio != s['labelling_seq']: raise ValueError('Masking BIO disagrees with PII_set')
    if actual != expected: raise ValueError('Masking spans disagree with relation privacy')

def load_archive(path):
    docs=[]
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if not name.endswith('.json') or '/._' in name or name.startswith('__MACOSX/'): continue
            g=json.loads(z.read(name)); validate_graph(g)
            roots={re.sub(r'_\d+$','',s['sent_idx']) for s in g['sentences']}
            # Original sentence IDs persist in augmented variants. New sentence IDs
            # may add suffixes, so source identity uses the first original sentence.
            source=re.sub(r'_\d+$','',g['sentences'][0]['sent_idx'])
            docs.append({'id':'/'.join(name.split('/')[-2:]), 'domain':name.split('/')[-2],
                         'source_id':source, 'graph':g, 'content_sha256':digest(g),
                         'text_hash':digest(norm('\n'.join(s['sentence'] for s in g['sentences'])))})
    if len({d['id'] for d in docs}) != len(docs): raise ValueError('Duplicate document keys')
    return docs

def public_document(doc):
    return {'id':doc['id'], 'domain':doc['domain'],
            'sentences':[{'sent_idx':s['sent_idx'],'sentence':s['sentence']} for s in doc['graph']['sentences']]}

def validate_candidates(doc, candidates):
    texts={s['sent_idx']:s['sentence'] for s in doc['graph']['sentences']}; seen=set(); ids=set()
    for e in candidates:
        if set(e)-{'entity_id','entity_type','canonical_form','mentions'}:
            raise ValueError('Unexpected field in predicted candidate')
        if e['entity_id'] in ids: raise ValueError('Duplicate predicted entity ID')
        ids.add(e['entity_id'])
        if not e['mentions'] or not isinstance(e['entity_type'],str): raise ValueError('Invalid candidate')
        for m in e['mentions']:
            if set(m)!={'sent_idx','begin','end','form'}:raise ValueError('Unexpected candidate mention fields')
            b,t=m['begin'],m['end']; text=texts[m['sent_idx']]
            if not (0<=b<t<=len(text)) or text[b:t]!=m['form']: raise ValueError('Invalid candidate offsets')
            k=(m['sent_idx'],b,t,e['entity_type'])
            if k in seen: raise ValueError('Duplicate candidate mention')
            seen.add(k)
        if any(k in e for k in ('privacy','privacy_label','mask','relations')):
            raise ValueError('Stage A candidates must not contain privacy targets')

def decisions(candidates, edges, relation_threshold=.5, privacy_threshold=.5):
    inc={e['entity_id']:[] for e in candidates}; seen=set()
    for e in edges:
        a,b=e['entity'],e['target_entity']; k=(*sorted((a,b)),e['relation'])
        if a not in inc or b not in inc or a==b or k in seen: raise ValueError('Invalid predicted edge')
        seen.add(k)
        rp,pp=e['relation_probability'],e['privacy_probability']
        if any(not isinstance(v,(float,int)) or not math.isfinite(v) or not 0<=v<=1 for v in (rp,pp)):
            raise ValueError('Invalid probability')
        if rp>=relation_threshold:
            for i in (a,b):inc[i].append(pp>=privacy_threshold)
    return {i:not es or any(es) for i,es in inc.items()}

def prepared(c):
    folder=project(c,c['prepared']); manifest=read(folder/'manifest.json')
    if sha(project(c,c['dataset']))!=manifest['dataset_sha256']:raise ValueError('Dataset changed; rebuild prepared data')
    if sha(folder/'documents.jsonl')!=manifest['documents_sha256']:raise ValueError('Prepared documents changed')
    if sha(folder/'split.json')!=manifest['split_sha256']:raise ValueError('Split changed')
    if sha(folder/'vocab.json')!=manifest['vocab_sha256']:raise ValueError('Vocabulary changed')
    if sha(project(c,c['llm']['prompt_file']))!=manifest['relation_schema_sha256']:raise ValueError('Relation schema changed')
    docs=rows(folder/'documents.jsonl'); split=read(folder/'split.json')
    return folder,manifest,docs,split

def candidate_cache(c, split_name):
    folder,manifest,docs,split=prepared(c)
    path=project(c,c['candidates'])/split_name
    meta=read(path/'manifest.json'); values=rows(path/'candidates.jsonl')
    if meta['prepared_sha256']!=sha(folder/'manifest.json'): raise ValueError('Candidate split/data mismatch')
    if meta['rows_sha256']!=sha(path/'candidates.jsonl'): raise ValueError('Candidates changed')
    if not meta.get('leakage_audit_passed'): raise ValueError('Stage A exposure audit is required')
    from .candidates import stage_a_provenance
    if meta['stage_a']!=stage_a_provenance(c):raise ValueError('Stage A checkpoint/code/exposure settings changed after caching')
    expected={d['id']:d for d in docs if split[d['id']]==split_name}
    if len(values)!=len(expected) or {v['id'] for v in values}!=set(expected): raise ValueError('Incomplete candidate cohort')
    for v in values:
        if set(v)!={'id','entities'}:raise ValueError('Unexpected candidate cache fields')
        validate_candidates(expected[v['id']],v['entities'])
    return meta,{v['id']:v['entities'] for v in values}


def allowed_relations(c):
    schema={};domain=None
    for line in project(c,c['llm']['prompt_file']).read_text(encoding='utf-8').splitlines():
        if re.fullmatch(r'\[[a-z_]+\]',line):
            domain=line[1:-1];schema[domain]=set()
        elif domain and line.startswith('- ') and ':' in line:
            schema[domain].add(line[2:].split(':',1)[0])
    if set(schema)!=set(c['stage_b']['domains']):raise ValueError('Relation schema domains mismatch')
    all_names=set().union(*schema.values())
    # Domain-specific examples are descriptive, not a current authoritative mask.
    # Some corrected archive annotations use a relation outside the legacy domain list.
    return {domain:set(all_names) for domain in schema}
