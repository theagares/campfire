"""Real character coverage, including detector misses and partial masking."""
from __future__ import annotations
import collections
import random
from .common import mask_labels


def coverage(entities,decisions):
    output=collections.defaultdict(set)
    for e in entities:
        if decisions[e['entity_id']]:
            for m in e['mentions']:output[m['sent_idx']].update(range(m['begin'],m['end']))
    return output


def document_counts(doc,entities,decisions):
    gold=mask_labels(doc['graph']);actual=coverage(entities,decisions)
    baseline=coverage(entities,{e['entity_id']:True for e in entities})
    bytype=collections.defaultdict(collections.Counter); counts=collections.Counter()
    exact_gold=set();exact_pred=set()
    for e in entities:
        if decisions[e['entity_id']]:
            exact_pred.update((m['sent_idx'],m['begin'],m['end'],e['entity_type']) for m in e['mentions'])
    for e in doc['graph']['entities']:
        for m in e['mentions']:
            chars=set(range(m['begin'],m['end'])); sid=m['sent_idx'];a=actual[sid];b=baseline[sid]
            row=collections.Counter()
            if gold[e['entity_id']]:
                row['mask_support']=1;row['leaked']=int(not chars<=a)
                row['introduced_leakage']=int(chars<=b and not chars<=a)
                row['baseline_leaked']=int(not chars<=b)
                exact_gold.add((sid,m['begin'],m['end'],e['entity_type']))
            else:
                row['keep_support']=1;row['overmasked']=int(bool(chars&a))
                row['correctly_released']=int(bool(chars&b) and not chars&a)
                row['baseline_overmasked']=int(bool(chars&b))
            counts.update(row);bytype[e['entity_type']].update(row)
    counts.update(exact_tp=len(exact_gold&exact_pred),exact_fp=len(exact_pred-exact_gold),exact_fn=len(exact_gold-exact_pred))
    # Detect accidental additions: Stage B must only remove Stage A masking.
    if any(not chars<=baseline[sid] for sid,chars in actual.items()):raise ValueError('Stage B added masking')
    return {'id':doc['id'],'domain':doc['domain'],'counts':dict(counts),'by_type':dict(bytype)}


def rates(c):
    def ratio(a,b):return c.get(a,0)/c.get(b,0) if c.get(b,0) else None
    tp,fp,fn=(c.get(k,0) for k in ('exact_tp','exact_fp','exact_fn'))
    values={k:c.get(k,0) for k in ('mask_support','keep_support','leaked','overmasked','introduced_leakage','baseline_leaked','baseline_overmasked','correctly_released')}
    return {**dict(c),**values,'overmask_rate':ratio('overmasked','keep_support'),'leakage_rate':ratio('leaked','mask_support'),
            'preservation_rate':1-ratio('overmasked','keep_support') if c.get('keep_support',0) else None,
            'protection_rate':1-ratio('leaked','mask_support') if c.get('mask_support',0) else None,
            'release_rate_among_baseline_overmasked':ratio('correctly_released','baseline_overmasked'),
            'exact_mask_span_type_f1':2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None}


def aggregate(values):
    total=collections.Counter();domains=collections.defaultdict(collections.Counter);types=collections.defaultdict(collections.Counter)
    for v in values:
        total.update(v['counts']);domains[v['domain']].update(v['counts'])
        for k,c in v['by_type'].items():types[k].update(c)
    # Exact span F1 is only computed for the overall table, not type/domain rows.
    return {'overall':rates(total),'by_domain':{k:rates(v) for k,v in domains.items()},
            'by_type':{k:rates(v) for k,v in types.items()},'documents':len(values)}


def paired_bootstrap(left,right,iterations=2000,seed=2026):
    """right-minus-left, resampling independent source groups, same sample for both."""
    if set(left)!=set(right) or not left:raise ValueError('Paired cohorts must be identical and nonempty')
    keys=sorted(left);rng=random.Random(seed);draws={k:[] for k in ('overmask_rate','leakage_rate')}
    # Values are already grouped by source, so augmented variants never act as independent units.
    for _ in range(iterations):
        sampled=rng.choices(keys,k=len(keys));a=collections.Counter();b=collections.Counter()
        for k in sampled:a.update(left[k]);b.update(right[k])
        ar,br=rates(a),rates(b)
        for metric in draws:
            if ar[metric] is not None and br[metric] is not None:draws[metric].append(br[metric]-ar[metric])
    totals=[]
    for source in (left,right):
        total=collections.Counter()
        for value in source.values():total.update(value)
        totals.append(rates(total))
    differences={k:totals[1][k]-totals[0][k] if totals[0][k] is not None and totals[1][k] is not None else None for k in draws}
    result={}
    for metric,values in draws.items():
        values.sort()
        result[metric]=[values[int((len(values)-1)*q)] for q in (.025,.975)] if values else None
    return {'difference':'right minus left','scale':'fraction','unit':'source document group','iterations':iterations,'mean_difference':differences,'ci95':result}
