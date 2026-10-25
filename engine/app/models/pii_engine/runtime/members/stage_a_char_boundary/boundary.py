"""BIO+CRF with character boundaries inside its first/last predicted tokens."""
import math
from pathlib import Path
import sys
import torch
from torch import nn
from torch.nn import functional as F

LEGACY = Path(__file__).resolve().parents[2] / 'repo/members/taehwan/submit_experiments/KDPII_SOTA'
sys.path.insert(0, str(LEGACY / 'src'))
import train as legacy
from kdpii import BIO, BIO_ID, TYPES
from modeling import PIITagger


def targets(row, offsets):
    """Label intersecting tokens, including partial boundaries; audit ambiguous tokens."""
    owners = [[j for j,e in enumerate(row['gold']) if a < e['end'] and e['begin'] < b]
              if b > a else [] for a,b in offsets]
    labels = []; active = set()
    for (a,b), hits in zip(offsets, owners):
        if b <= a or len(hits) > 1:
            labels.append(-100)
        elif hits:
            j = hits[0]; e = row['gold'][j]
            labels.append(BIO_ID[('I-' if j in active else 'B-') + e['label']]); active.add(j)
        elif any(a < end and begin < b for begin,end,_ in row['ignored']):
            labels.append(-100)
        else:
            labels.append(0)
    edges = []; audit = dict(gold=len(row['gold']), interior_boundary=0, ambiguous_or_unrepresentable=0)
    for j,e in enumerate(row['gold']):
        positions = [i for i,h in enumerate(owners) if j in h]
        if not positions:
            audit['ambiguous_or_unrepresentable'] += 1; continue
        first, last = positions[0], positions[-1]
        a,b = offsets[first]; c,d = offsets[last]
        if any(len(owners[i]) != 1 for i in positions) or not (a <= e['begin'] < b and c < e['end'] <= d):
            audit['ambiguous_or_unrepresentable'] += 1; continue
        audit['interior_boundary'] += int(a != e['begin'] or d != e['end'])
        edges.extend([dict(token=first,role=0,label=TYPES.index(e['label']),target=e['begin']-a),
                      dict(token=last,role=1,label=TYPES.index(e['label']),target=e['end']-c)])
    return labels, edges, audit


class CharacterBoundary(nn.Module):
    def __init__(self, hidden, vocab_size):
        super().__init__()
        self.context = nn.Linear(hidden,128)
        self.chars = nn.Embedding(vocab_size,24,padding_idx=0)
        self.position = nn.Embedding(33,16)
        self.kind = nn.Embedding(len(TYPES),16)
        self.role = nn.Embedding(2,8)
        self.mlp = nn.Sequential(nn.Linear(218,128),nn.GELU(),nn.Linear(128,1))

    def forward(self, features, rows, items, vocab):
        device = features.device
        if not items:
            return features.new_zeros((0,1)), torch.zeros((0,1),device=device,dtype=torch.bool)
        lengths=[]; strings=[]
        for item in items:
            row=rows[item['batch']];a,b=row['offsets'][item['token']]
            text=row['text'][a:b]
            if not text: raise ValueError('Boundary on empty token')
            lengths.append(len(text));strings.append(text)
        width=max(lengths)+1;n=len(items)
        left=torch.zeros((n,width),dtype=torch.long,device=device);right=torch.zeros_like(left)
        positions=torch.arange(width,device=device)[None,:].expand(n,-1)
        for i,text in enumerate(strings):
            ids=[vocab.get(c,1) for c in text]
            left[i,:len(text)+1]=torch.tensor([2]+ids,device=device)
            right[i,:len(text)+1]=torch.tensor(ids+[3],device=device)
        lengths_t=torch.tensor(lengths,device=device)[:,None]
        roles=torch.tensor([x['role'] for x in items],device=device)
        valid=(positions<=lengths_t) & torch.where(roles[:,None]==0,positions<lengths_t,positions>0)
        h=torch.stack([features[x['batch'],x['token']] for x in items])
        context=self.context(h)[:,None,:].expand(-1,width,-1)
        labels=torch.tensor([x['label'] for x in items],device=device)
        numeric=torch.stack((positions/lengths_t,torch.log1p(lengths_t).expand(-1,width)/10),dim=-1)
        x=torch.cat((context,self.chars(left),self.chars(right),self.position(positions.clamp_max(32)),
                     self.kind(labels)[:,None,:].expand(-1,width,-1),self.role(roles)[:,None,:].expand(-1,width,-1),numeric),dim=-1)
        logits=self.mlp(x).squeeze(-1).float().masked_fill(~valid,-1e4)
        return logits,valid


class BoundaryTagger(PIITagger):
    def __init__(self, backbone, vocab_size):
        # Preserve the original detector initialization before adding parameters.
        super().__init__(backbone, True, True)
        self.boundary=CharacterBoundary(self.classifier.in_features,vocab_size)

    def forward(self,input_ids,attention_mask,gazetteer):
        hidden=self.backbone(input_ids=input_ids,attention_mask=attention_mask).last_hidden_state
        features=torch.cat((hidden,self.gazetteer_projection(gazetteer)),dim=-1)
        return self.classifier(self.dropout(features)),features


def boundary_loss(model,features,rows,vocab):
    items=[dict(batch=i,**e) for i,row in enumerate(rows) for e in row['boundary_targets']]
    if not items: return features.sum()*0,0
    scores,valid=model.boundary(features,rows,items,vocab)
    expected=torch.tensor([e['target'] for e in items],device=features.device)
    if not bool(valid[torch.arange(len(items),device=features.device),expected].all()):
        raise ValueError('Invalid training boundary target')
    return F.cross_entropy(scores,expected),len(items)


def candidates(offsets,tags):
    result=[];current=None
    for i,((a,b),label) in enumerate(zip(offsets,tags)):
        if b<=a: continue
        tag=BIO[label]
        if tag=='O':
            if current: result.append(current);current=None
        elif tag.startswith('B-') or current is None or current['label']!=tag[2:]:
            if current:result.append(current)
            current=dict(begin=a,end=b,label=tag[2:],first=i,last=i)
        else:
            current['end']=b;current['last']=i
    if current: result.append(current)
    return result


def choose_boundaries(start,end,same_token):
    if same_token:
        positions=torch.arange(start.shape[0],device=start.device)
        joint=(start[:,None]+end[None,:]).masked_fill(positions[:,None]>=positions[None,:],-1e9)
        index=int(joint.argmax());return index//len(end),index%len(end)
    return int(start.argmax()),int(end.argmax())


@torch.no_grad()
def predict_spans(model,features,rows,tags,vocab):
    coarse=[candidates(row['offsets'],ids[:len(row['offsets'])]) for row,ids in zip(rows,tags)]
    items=[]
    for i,spans in enumerate(coarse):
        for span in spans:
            items.extend([dict(batch=i,token=span['first'],role=0,label=TYPES.index(span['label'])),
                          dict(batch=i,token=span['last'],role=1,label=TYPES.index(span['label']))])
    scores,_=model.boundary(features,rows,items,vocab)
    refined=[];cursor=0
    for row,spans in zip(rows,coarse):
        output=[]
        for span in spans:
            first,last=span['first'],span['last'];a,b=row['offsets'][first];c,d=row['offsets'][last]
            start,end=choose_boundaries(scores[cursor,:b-a+1],scores[cursor+1,:d-c+1],first==last);cursor+=2
            if not 0<=a+start<c+end<=len(row['text']):raise ValueError('Invalid refined span')
            output.append(dict(begin=a+start,end=c+end,label=span['label']))
        refined.append(output)
    clean=[[{k:s[k] for k in ['begin','end','label']} for s in spans] for spans in coarse]
    return clean,refined
