"""One Stage A detector, with attached MLPs sharing its representations.

Discrete Viterbi/span selection is inference-only. Joint training uses annotated
mentions and detection + contextual losses. No gradient is claimed through argmax.
"""
from pathlib import Path
import sys,json,itertools
import torch
from torch import nn
from torch.nn import functional as F
from safetensors.torch import load_file

PARENT=Path(__file__).resolve().parent/'members'
sys.path[:0]=[str(PARENT/'downstream_masking/src'),str(PARENT/'stage_a_sota/src'),str(PARENT/'stage_a_char_boundary')]
from stagea.models import Detector,span_targets,span_loss,decode,flat_spans
from stagea.kdpii import TYPES,BIO,align_bio
from stagea.base_modeling import GazetteerTrie
from boundary import BoundaryTagger,targets as char_targets,boundary_loss,predict_spans,legacy
from masking.candidates import registry
from masking.common import mask_labels
from masking.heads import make_mlp
from transformers import AutoTokenizer

DOMAINS=['career_education','contract','financial','legal','medical','support']

class ContextHeads(nn.Module):
    def __init__(self,features,relations,direct=False):
        super().__init__();self.direct=direct;self.relation_count=relations
        width=(2 if direct else 4)*features+len(TYPES)+len(DOMAINS)
        if direct:self.mask=make_mlp(width,[512,256],1,.15)
        else:
            self.relation=make_mlp(width,[512,256],relations,.15)
            self.privacy=make_mlp(width+2*relations,[512,256],1,.15)
    def forward(self,x):
        if self.direct:return self.mask(x).squeeze(-1)
        relation=self.relation(x);n=len(x);r=self.relation_count
        identity=torch.eye(r,device=x.device).expand(n,-1,-1)
        values=torch.cat((x[:,None].expand(-1,r,-1),relation.sigmoid()[:,None].expand(-1,r,-1),identity),-1)
        return relation,self.privacy(values).squeeze(-1)

class Integrated(nn.Module):
    def __init__(self,config,relations,condition,device='cuda'):
        super().__init__()
        if config.get('model') != 'ax_char_boundary' or condition != 'direct_full':
            raise ValueError('This runtime supports only ax_char_boundary/direct_full')
        a=config['stage_a'];self.config=config;self.relations=relations
        run=Path(a['run_dir']);rc=json.loads((run/'config.json').read_text())
        rc['backbone']=str(Path(a['backbone_path']).resolve())
        self.is_char=config['model']=='ax_char_boundary';self.frozen=condition=='frozen';self.direct=condition=='direct_full'
        self.tokenizer=AutoTokenizer.from_pretrained(rc['backbone'],local_files_only=True,use_fast=True)
        if self.is_char:
            self.vocab=json.loads((run/'char_vocab.json').read_text());self.stage_a=BoundaryTagger(Path(rc['backbone']),len(self.vocab)+4);self.head_kind='bio'
        else:
            self.vocab=None;spec={'head':'bio',**rc['spec']};self.head_kind=spec['head']
            self.stage_a=Detector(rc['backbone'],spec,rc.get('span_dim',128),True)
        # The caller loads the complete final Integrated state_dict strictly.
        # No initial detector or pretrained backbone weights are needed.
        self.stage_a.requires_grad_(not self.frozen)
        self.gaz=GazetteerTrie(json.loads((run/'train_gazetteer.json').read_text()))
        self.max_length=rc['max_length'];self.stride=a['window_stride'];self.device_name=device
        hidden=self.stage_a.backbone.config.hidden_size
        self.feature_dim=hidden+(32 if self.stage_a.use_gazetteer else 0)+len(BIO)
        self.heads=ContextHeads(self.feature_dim,len(relations),self.direct)
        self.allowed={};self.to(device)
    def train(self,mode=True):
        super().train(mode)
        if self.frozen:self.stage_a.eval()
        return self

    def tokenize(self,public,gold=None):
        """The public argument contains sentences only; gold is training-only."""
        windows=[];keys=[];lookup={}
        gold_mentions={}
        if gold is not None:
            for e in gold['entities']:
                for m in e['mentions']:
                    gold_mentions.setdefault(m['sent_idx'],[]).append(dict(begin=m['begin'],end=m['end'],label=e['entity_type']))
        for s in public['sentences']:
            text=s['sentence'];sid=s['sent_idx']
            tok=self.tokenizer(text,truncation=True,max_length=self.max_length,stride=self.stride,return_overflowing_tokens=True,return_offsets_mapping=True)
            for ids,att,offsets in zip(tok['input_ids'],tok['attention_mask'],tok['offset_mapping']):
                offsets=[tuple(x) for x in offsets];real=[i for i,(a,b) in enumerate(offsets) if b>a]
                if not real:continue
                row=dict(text=text,sent_idx=sid,input_ids=ids,attention_mask=att,offsets=offsets,gazetteer=self.gaz.features(text,offsets),real=real)
                row['global']=[]
                for i in real:
                    key=(sid,*offsets[i])
                    if key not in lookup:lookup[key]=len(keys);keys.append(key)
                    row['global'].append(lookup[key])
                if gold is not None:
                    left,right=offsets[real[0]][0],offsets[real[-1]][1]
                    row['gold']=[e for e in gold_mentions.get(sid,[]) if left<=e['begin']<e['end']<=right]
                    row['ignored']=[(e['begin'],e['end'],e['label']) for e in gold_mentions.get(sid,[]) if e not in row['gold'] and left<e['end'] and e['begin']<right]
                    if self.is_char:row['labels'],row['boundary_targets'],_=char_targets(row,offsets)
                    else:row['labels']=align_bio(row,offsets)
                    if self.head_kind=='span':row['span_targets'],_=span_targets(row,len(ids))
                windows.append(row)
        if not keys:raise ValueError('Document has no tokens')
        return dict(windows=windows,keys=keys,public=public)

    def encode(self,data,supervised=False,predict=False):
        device=next(self.parameters()).device;n=len(data['keys']);total=None;coverage=None;all_spans={};loss_sum=[]
        windows=data['windows']
        for start in range(0,len(windows),8):
            batch=windows[start:start+8];width=max(len(x['input_ids']) for x in batch)
            ids=torch.full((len(batch),width),self.tokenizer.pad_token_id,device=device,dtype=torch.long)
            att=torch.zeros_like(ids);gaz=torch.zeros(len(batch),width,len(TYPES),device=device)
            labels=torch.full_like(ids,-100)
            for j,row in enumerate(batch):
                k=len(row['input_ids']);ids[j,:k]=torch.tensor(row['input_ids'],device=device);att[j,:k]=1;gaz[j,:k]=torch.tensor(row['gazetteer'],device=device)
                if supervised:labels[j,:k]=torch.tensor(row['labels'],device=device)
            with torch.set_grad_enabled(self.training and not self.frozen):
                h=self.stage_a.backbone(input_ids=ids,attention_mask=att).last_hidden_state
                features=h
                if self.stage_a.use_gazetteer:
                    g=self.stage_a.gazetteer_projection(gaz)
                    if self.head_kind=='gated':g=self.stage_a.context_gate(h,g)
                    features=torch.cat((h,g),-1)
                if self.head_kind=='span':
                    logits=self.stage_a.span_head(h)
                    # Soft start/end evidence keeps contextual-loss gradients to the span head.
                    valid=att.bool();order=torch.arange(width,device=device)
                    valid_pair=valid[:,:,None]&valid[:,None,:]&(order[:,None]<=order[None,:])[None]
                    prob=logits.float().softmax(-1)[...,1:]*valid_pair[...,None]
                    first=prob.amax(2);last=prob.amax(1)
                    evidence=torch.cat((1-first.amax(-1,keepdim=True),first,last),-1)
                else:
                    logits=self.stage_a.classifier(self.stage_a.dropout(features));evidence=logits.float().softmax(-1)
                joined=torch.cat((features,evidence),-1)
                if supervised:
                    if self.head_kind=='span':
                        targets=torch.full((len(batch),width,width),-100,device=device,dtype=torch.long)
                        for j,row in enumerate(batch):
                            k=len(row['input_ids']);targets[j,:k,:k]=row['span_targets'].to(device)
                        detection=span_loss(logits,targets)
                    elif self.stage_a.use_crf:detection=self.stage_a.crf.loss(logits,labels)/max(1,int((labels!=-100).sum()))
                    else:detection=F.cross_entropy(logits.float().reshape(-1,len(BIO)),labels.reshape(-1),ignore_index=-100)
                    if self.is_char:
                        boundary,_=boundary_loss(self.stage_a,features,batch,self.vocab);detection=detection+.1*boundary
                    loss_sum.append(detection)
            flat=[];indices=[]
            for j,row in enumerate(batch):flat.append(joined[j,row['real']]);indices.extend(row['global'])
            vals=torch.cat(flat);idx=torch.tensor(indices,device=device)
            if total is None:total=vals.new_zeros(n,vals.shape[-1]);coverage=vals.new_zeros(n,1)
            total=total.index_add(0,idx,vals);coverage=coverage.index_add(0,idx,vals.new_ones(len(indices),1))
            if predict:
                with torch.no_grad():
                    if self.is_char:
                        real=torch.zeros_like(att,dtype=torch.bool)
                        for j,row in enumerate(batch):real[j,row['real']]=True
                        tags=legacy.decode(self.stage_a,logits,real)
                        _,decoded=predict_spans(self.stage_a,features,batch,tags,self.vocab)
                    else:decoded=decode(self.stage_a,logits,batch)
                    for row,spans in zip(batch,decoded):
                        bucket=all_spans.setdefault(row['sent_idx'],{})
                        for s in spans:
                            covered=[i for i in row['real'] if row['offsets'][i][0]<s['end'] and s['begin']<row['offsets'][i][1]]
                            score=1+min(covered[0]-row['real'][0],row['real'][-1]-covered[-1]) if covered else 1
                            key=(s['begin'],s['end'],s['label'])
                            if key not in bucket or score>bucket[key]['score']:bucket[key]={**s,'score':score}
        if bool((coverage==0).any()):raise ValueError('Uncovered tokens')
        entities=None
        if predict:
            spans=[];texts={s['sent_idx']:s['sentence'] for s in data['public']['sentences']}
            for sid,bucket in all_spans.items():
                for s in flat_spans(list(bucket.values())):
                    spans.append(dict(begin=s['begin'],end=s['end'],label=s['label'],sent_idx=sid,form=texts[sid][s['begin']:s['end']]))
            entities=registry(spans)
        detection_loss=torch.stack(loss_sum).mean() if loss_sum else total.sum()*0
        return total/coverage,entities,detection_loss

    def contextual(self,h,data,entities,domain):
        device=h.device;vectors=[];ranges=[]
        for e in entities:
            selected=set()
            for m in e['mentions']:
                selected.update(i for i,(sid,a,b) in enumerate(data['keys']) if sid==m['sent_idx'] and a<m['end'] and m['begin']<b)
            if not selected:raise ValueError('No entity tokens')
            idx=sorted(selected);vectors.append(h[idx].mean(0));ranges.append((idx[0],idx[-1]+1))
        if not vectors:return None,[]
        v=torch.stack(vectors);types=F.one_hot(torch.tensor([TYPES.index(e['entity_type']) for e in entities],device=device),len(TYPES)).float()
        dom=F.one_hot(torch.tensor(DOMAINS.index(domain),device=device),len(DOMAINS)).float()
        if self.direct:
            x=torch.cat((v,h.mean(0).expand(len(v),-1),types,dom.expand(len(v),-1)),-1)
            return self.heads(x),[]
        pairs=list(itertools.combinations(range(len(entities)),2))
        if not pairs:return None,pairs
        a,b=torch.tensor(pairs,device=device).unbind(-1);context=[]
        for i,j in pairs:
            left,right=sorted((ranges[i],ranges[j]));s,t=left[1],max(left[1],right[0])
            context.append(h[s:t].mean(0) if t>s else h.mean(0))
        x=torch.cat((v[a]+v[b],(v[a]-v[b]).abs(),v[a]*v[b],torch.stack(context),types[a]+types[b],dom.expand(len(pairs),-1)),-1)
        return self.heads(x),pairs

    def training_loss(self,data,graph,domain,pos_weight):
        h,_,detection=self.encode(data,supervised=not self.frozen)
        output,pairs=self.contextual(h,data,graph['entities'],domain)
        if output is None:raise ValueError('Empty training targets')
        if self.direct:
            labels=mask_labels(graph);truth=torch.tensor([labels[e['entity_id']] for e in graph['entities']],device=h.device,dtype=torch.float32)
            context=F.binary_cross_entropy_with_logits(output.float(),truth)
        else:
            relation,privacy=output;truth=torch.zeros_like(relation,dtype=torch.float32);private=torch.zeros_like(truth)
            eid={e['entity_id']:i for i,e in enumerate(graph['entities'])};pairmap={p:i for i,p in enumerate(pairs)}
            for edge in graph['relations']:
                i=pairmap[tuple(sorted((eid[edge['entity']],eid[edge['target_entity']])))]
                j=self.relations.index(edge['relation']);truth[i,j]=1;private[i,j]=float(edge['privacy_label']=='PII')
            context=F.binary_cross_entropy_with_logits(relation.float(),truth,pos_weight=pos_weight)
            mask=truth.bool()
            if bool(mask.any()):context=context+F.binary_cross_entropy_with_logits(privacy[mask].float(),private[mask])
        return context+detection,dict(context=context,detection=detection)

    @torch.inference_mode()
    def infer(self,data,domain):
        self.eval();h,entities,_=self.encode(data,predict=True)
        output,pairs=self.contextual(h,data,entities,domain)
        if self.direct:
            return entities,[],({e['entity_id']:float(v) for e,v in zip(entities,output.sigmoid())} if output is not None else {})
        edges=[]
        if output is not None:
            rp,pp=[x.float().sigmoid().cpu().tolist() for x in output]
            for i,(a,b) in enumerate(pairs):
                for j,name in enumerate(self.relations):
                    if name in self.allowed[domain]:edges.append(dict(entity=entities[a]['entity_id'],target_entity=entities[b]['entity_id'],relation=name,relation_probability=rp[i][j],privacy_probability=pp[i][j]))
        return entities,edges,None
