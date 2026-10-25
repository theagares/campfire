"""One immutable Stage A prediction cache shared by all Stage B conditions."""
from __future__ import annotations
import collections
import importlib
import sys
import time
from pathlib import Path
from .common import *


def stage_a_provenance(c):
    a=c['stage_a']
    if not a.get('run_dir'):raise ValueError('Select stage_a.run_dir using KDPII validation before prediction')
    run=project(c,a['run_dir']); rc=read(run/'config.json'); checkpoint=run/a['checkpoint']
    exposure_paths=[project(c,p) for p in a['exposure_files']]
    if not exposure_paths:raise ValueError('Stage A train/valid exposure records are required')
    hashes={str(p):sha(p) for p in exposure_paths}
    if rc.get('rows_sha256') not in hashes.values():
        raise ValueError('Stage A run config rows_sha256 must match an exposure JSONL')
    files={str(checkpoint):sha(checkpoint),str(run/'config.json'):sha(run/'config.json')}
    for name in ('train_gazetteer.json',):
        if (run/name).exists():files[str(run/name)]=sha(run/name)
    code_files={str(p):sha(p) for p in sorted(project(c,a['code_dir']).glob('*.py'))}
    return {'run_dir':str(run),'checkpoint':str(checkpoint),'files':files,'code_files':code_files,
            'exposures':hashes,'adapter':a['adapter'],'window_stride':a['window_stride']}


def leakage_audit(docs, split, c, provenance):
    # Check the same retained rows used for Stage A training/model selection.
    texts=set();source_ids=set(); used=0
    for path in provenance['exposures']:
        for row in rows(path):
            if row.get('split') not in ('train','valid','validation','dev'):continue
            used+=1
            if row.get('source_id'):source_ids.add(row['source_id'])
            if 'text' in row and row.get('gold'):texts.add(norm(row['text']))
            for sentence in row.get('sentences',[]):
                if sentence.get('gold'):texts.add(norm(sentence['text']))
    if not used:raise ValueError('No Stage A training/validation exposure rows found')
    overlaps=[]
    for d in docs:
        if split[d['id']] not in ('valid','test'):continue
        hit=d['source_id'] in source_ids or any(norm(s['sentence']) in texts for s in d['graph']['sentences'])
        if hit:overlaps.append({'document':d['id'],'partition':split[d['id']]})
    return {'passed':not overlaps,'stage_a_used_rows':used,'overlaps':overlaps,
            'scope':'Original source IDs where available + normalized entity-bearing sentences; not a semantic near-duplicate guarantee.'}


def registry(spans):
    groups={}
    for s in spans:
        key=(s['label'],norm(s['form']))
        if key not in groups:groups[key]={'entity_id':f'P{len(groups)+1}','entity_type':s['label'],'canonical_form':key[1],'mentions':[]}
        m={k:s[k] for k in ('sent_idx','begin','end','form')}
        if m not in groups[key]['mentions']:groups[key]['mentions'].append(m)
    return list(groups.values())


class ExistingKDPIIDetector:
    """Adapter for existing PIITagger safetensors. New heads supply another factory."""
    def __init__(self,c):
        import torch
        from safetensors.torch import load_file
        from transformers import AutoTokenizer
        sys.path.insert(0,str(project(c,c['stage_a']['code_dir'])))
        from modeling import PIITagger,GazetteerTrie
        from kdpii import decode_bio
        self.torch=torch;self.decode_bio=decode_bio
        run=project(c,c['stage_a']['run_dir']);rc=read(run/'config.json');spec=rc['spec']
        self.tokenizer=AutoTokenizer.from_pretrained(rc['backbone'],local_files_only=True,use_fast=True)
        self.model=PIITagger(Path(rc['backbone']),spec['gazetteer'],spec['crf'],False)
        self.model.load_state_dict(load_file(str(run/c['stage_a']['checkpoint'])),strict=True)
        self.device=c['device'];self.model.to(self.device).eval()
        self.gaz=GazetteerTrie(read(run/'train_gazetteer.json')) if spec['gazetteer'] else None
        self.max_length=rc['max_length'];self.stride=c['stage_a']['window_stride']
        if not 0<=self.stride<self.max_length-2:raise ValueError('Invalid Stage A overlap stride')
        self.windows=0
    def predict(self,sentences):
        import torch
        from kdpii import TYPES
        result=[]
        with torch.inference_mode():
            for s in sentences:
                text=s['sentence']
                batch=self.tokenizer(text,truncation=True,max_length=self.max_length,stride=self.stride,
                      return_overflowing_tokens=True,return_offsets_mapping=True)
                for ids,attn,offsets in zip(batch['input_ids'],batch['attention_mask'],batch['offset_mapping']):
                    self.windows+=1
                    gaz=self.gaz.features(text,offsets) if self.gaz else [[0.]*len(TYPES) for _ in ids]
                    logits=self.model(torch.tensor([ids],device=self.device),torch.tensor([attn],device=self.device),
                                      torch.tensor([gaz],device=self.device,dtype=torch.float32))
                    valid=torch.tensor([b>a for a,b in offsets],device=self.device,dtype=torch.bool)
                    tags=[0]*len(ids)
                    if bool(valid.any()):
                        if self.model.use_crf:
                            em=logits[:,valid];p=self.model.crf.decode(em,torch.ones(em.shape[:2],device=self.device,dtype=torch.bool))[0].tolist()
                        else:p=logits[0,valid].argmax(-1).tolist()
                        for i,t in zip(valid.nonzero().flatten().tolist(),p):tags[i]=t
                    for m in self.decode_bio(offsets,tags):
                        m.update(sent_idx=s['sent_idx'],form=text[m['begin']:m['end']]);result.append(m)
        # Overlapping windows are deduplicated; no gold span is used in merging.
        return registry(result)


def predict(c, split_name):
    folder,manifest,docs,split=prepared(c); provenance=stage_a_provenance(c)
    audit=leakage_audit(docs,split,c,provenance)
    if not audit['passed']:
        write(project(c,c['candidates'])/'leakage_blocker.json',audit)
        raise ValueError('Stage A train/valid exposure overlaps held-out documents; see leakage_blocker.json')
    out=project(c,c['candidates'])/split_name
    if out.exists():raise FileExistsError('Candidate cache is immutable; choose a new candidates directory')
    module,name=c['stage_a']['adapter'].split(':')
    detector=getattr(importlib.import_module(module),name)(c)
    selected=[d for d in docs if split[d['id']]==split_name];output=[];started=time.monotonic()
    for d in selected:
        entities=detector.predict(public_document(d)['sentences']);validate_candidates(d,entities)
        output.append({'id':d['id'],'entities':entities})
    elapsed=time.monotonic()-started
    out.mkdir(parents=True);write_rows(out/'candidates.jsonl',output)
    write(out/'manifest.json',{'prepared_sha256':sha(folder/'manifest.json'),'stage_a':provenance,
          'leakage_audit_passed':True,'leakage_audit':audit,'split':split_name,'documents':len(output),
          'rows_sha256':sha(out/'candidates.jsonl'),'seconds':elapsed,'grouping':'type + normalized surface; not verified coreference'})
