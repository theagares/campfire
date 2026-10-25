"""Fresh task heads on original pretrained backbones; no detector checkpoints."""
from pathlib import Path
import torch
from torch import nn
from torch.nn import functional as F
from .base_modeling import MODEL_SPECS as BASE_SPECS, PIITagger, GazetteerTrie, parameter_counts
from .kdpii import BIO, TYPES, decode_bio

SPECS={k:{**v,'head':'bio'} for k,v in BASE_SPECS.items()}
SPECS.update({
 'ax_gated_gazetteer_crf':{**SPECS['ax_gazetteer_crf'],'head':'gated'},
 'ax_span':{**SPECS['ax_encoder'],'head':'span'},
 'xlmr_crf':{**SPECS['xlmr_large'],'crf':True},
 'klue_roberta_crf':{**SPECS['klue_roberta'],'crf':True},
 'klue_bert':{'backbone':'klue-bert-base','gazetteer':False,'crf':False,'head':'bio','lr':2e-5},
 'klue_bert_crf':{'backbone':'klue-bert-base','gazetteer':False,'crf':True,'head':'bio','lr':2e-5},
})

class ContextGate(nn.Module):
    def __init__(self,hidden,gaz_dim=32):
        super().__init__(); self.gate=nn.Linear(hidden+gaz_dim,gaz_dim)
    def forward(self,hidden,gaz):
        return gaz*torch.sigmoid(self.gate(torch.cat((hidden,gaz),-1)))

class BiaffineHead(nn.Module):
    def __init__(self,hidden,dim=128,dropout=.1):
        super().__init__()
        self.start=nn.Sequential(nn.Linear(hidden,dim),nn.GELU(),nn.Dropout(dropout))
        self.end=nn.Sequential(nn.Linear(hidden,dim),nn.GELU(),nn.Dropout(dropout))
        self.weight=nn.Parameter(torch.empty(len(TYPES)+1,dim+1,dim+1))
        nn.init.xavier_uniform_(self.weight)
    def forward(self,hidden):
        s,e=self.start(hidden),self.end(hidden)
        s=torch.cat((s,torch.ones_like(s[...,:1])),-1)
        e=torch.cat((e,torch.ones_like(e[...,:1])),-1)
        return torch.einsum('bix,cxy,bjy->bijc',s,self.weight,e)

class Detector(PIITagger):
    def __init__(self,path,spec,span_dim=128,gradient_checkpointing=True):
        super().__init__(Path(path),spec['gazetteer'],spec['crf'],gradient_checkpointing)
        self.head_kind=spec['head']; hidden=self.backbone.config.hidden_size
        if self.head_kind=='gated':self.context_gate=ContextGate(hidden)
        if self.head_kind=='span':
            del self.classifier
            self.span_head=BiaffineHead(hidden,span_dim,self.dropout.p)
    def forward(self,input_ids,attention_mask,gazetteer):
        hidden=self.backbone(input_ids=input_ids,attention_mask=attention_mask).last_hidden_state
        if self.head_kind=='span':return self.span_head(hidden)
        if self.use_gazetteer:
            gaz=self.gazetteer_projection(gazetteer)
            if self.head_kind=='gated':gaz=self.context_gate(hidden,gaz)
            hidden=torch.cat((hidden,gaz),-1)
        return self.classifier(self.dropout(hidden))


def span_targets(row,width):
    """Ignore spans crossing ignored/alignment-invalid tokens; NONE=0, type=1..19."""
    offsets=row['offsets'];n=len(offsets)
    target=torch.full((width,width),-100,dtype=torch.long)
    valid=[label!=-100 and b>a for label,(a,b) in zip(row['labels'],offsets)]
    for i in range(n):
        if not valid[i]:continue
        for j in range(i,n):
            if not valid[j]:break
            target[i,j]=0
    unaligned=[]
    for entity in row['gold']:
        starts=[i for i,(a,b) in enumerate(offsets) if b>a and a==entity['begin']]
        ends=[j for j,(a,b) in enumerate(offsets) if b>a and b==entity['end']]
        pairs=[(i,j) for i in starts for j in ends if i<=j and target[i,j]!=-100]
        if len(pairs)==1:
            target[pairs[0]]=TYPES.index(entity['label'])+1
        else:
            unaligned.append(entity)
    # Unrepresentable gold must not supply contradictory NONE targets.
    for entity in unaligned:
        for i,(a,b) in enumerate(offsets):
            if b<=a:continue
            for j in range(i,n):
                if target[i,j]==0 and a<entity['end'] and offsets[j][1]>entity['begin']:
                    target[i,j]=-100
    return target,len(unaligned)


def span_loss(logits,targets):
    """Equal weight for present positive/negative groups; fixed, not test-tuned."""
    valid=targets!=-100
    if not valid.any():return logits.sum()*0
    losses=F.cross_entropy(logits.float()[valid],targets[valid],reduction='none')
    pos=targets[valid]>0
    groups=[losses[mask].mean() for mask in (pos,~pos) if bool(mask.any())]
    return torch.stack(groups).mean()


def flat_spans(candidates):
    """Maximum total positive score for non-overlapping character intervals."""
    import bisect
    candidates=sorted(candidates,key=lambda x:(x['end'],x['begin'],x['label']))
    ends=[x['end'] for x in candidates];scores=[0.];solutions=[[]]
    for k,c in enumerate(candidates):
        p=bisect.bisect_right(ends,c['begin'],0,k)
        include=scores[p]+c['score']
        if include>scores[-1]:scores.append(include);solutions.append(solutions[p]+[c])
        else:scores.append(scores[-1]);solutions.append(solutions[-1])
    return sorted(solutions[-1],key=lambda x:(x['begin'],x['end']))


def decode_span(logits,offsets):
    # No gold or ignored-label mask enters inference.
    margins=logits.float()[...,1:]-logits.float()[...,:1]
    score,kind=margins.max(-1);score=score.detach().cpu();kind=kind.detach().cpu()
    candidates=[]
    for i,(begin,end) in enumerate(offsets):
        if end<=begin:continue
        for j in range(i,len(offsets)):
            if offsets[j][1]<=offsets[j][0]:break
            if score[i,j]>0:
                candidates.append({'begin':begin,'end':offsets[j][1],'label':TYPES[int(kind[i,j])],'score':float(score[i,j])})
    return [{k:v for k,v in x.items() if k!='score'} for x in flat_spans(candidates)]


def decode(model,logits,rows):
    result=[]
    for k,row in enumerate(rows):
        offsets=row['offsets']
        if model.head_kind=='span':result.append(decode_span(logits[k],offsets));continue
        positions=[i for i,(a,b) in enumerate(offsets) if b>a]
        tags=[0]*len(offsets)
        if positions:
            em=logits[k,positions]
            if model.use_crf:
                path=model.crf.decode(em[None],torch.ones((1,len(positions)),dtype=torch.bool,device=em.device))[0].tolist()
            else:path=em.argmax(-1).tolist()
            for i,tag in zip(positions,path):tags[i]=tag
        result.append(decode_bio(offsets,tags))
    return result
