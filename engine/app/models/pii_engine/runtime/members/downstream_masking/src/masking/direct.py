"""Direct entity MASK probability; no relation prediction or relation inputs."""
import math
import torch
from contextlib import nullcontext
from .metrics import document_counts,aggregate


def direct_decisions(candidates,probabilities,threshold):
    ids={e['entity_id'] for e in candidates}
    if set(probabilities)-ids:raise ValueError('Probability for an unknown candidate')
    if not 0<=threshold<=1:raise ValueError('Invalid MASK threshold')
    for p in probabilities.values():
        if not isinstance(p,(int,float)) or not math.isfinite(p) or not 0<=p<=1:raise ValueError('Invalid MASK probability')
    # Unsupported entity types remain masked, as in the relation-based model.
    return {key:key not in probabilities or probabilities[key]>=threshold for key in ids}


@torch.inference_mode()
def predict_direct(model,document):
    model.eval();device=next(model.parameters()).device
    amp=torch.autocast('cuda',dtype=torch.bfloat16) if device.type=='cuda' and getattr(model,'precision','fp32')=='bf16' else nullcontext()
    with amp:logits=model(document)
    return dict(zip(document.entity_ids,logits.float().sigmoid().cpu().tolist()))


def choose_direct_threshold(docs,candidates,scores,c):
    values=[]
    # Thirteen prespecified options, matching the relation model's 12 + all-mask.
    for threshold in c['selection']['direct_mask_thresholds']:
        metric=aggregate([document_counts(d,candidates[d['id']],direct_decisions(candidates[d['id']],scores[d['id']],threshold)) for d in docs])['overall']
        added=metric.get('introduced_leakage',0)/max(1,metric.get('mask_support',0))
        values.append({'mask_threshold':threshold,'allowed':added<=c['selection']['max_added_validation_leakage_rate'],
                       'metrics':metric,'added_leakage_rate':added})
    feasible=[r for r in values if r['allowed']]
    if not feasible:raise ValueError('No direct threshold satisfies leakage budget; include all-mask threshold 0')
    best=min(feasible,key=lambda r:(r['metrics']['overmask_rate'],r['metrics']['leakage_rate'],r['mask_threshold']))
    return best,values
