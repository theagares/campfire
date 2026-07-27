"""
조합 가능한 PII 토큰분류: 백본 + [선택] gazetteer + [선택] CRF.
"""

from typing import Optional
from pathlib import Path

import torch
import torch.nn as nn
from transformers import AutoConfig, AutoModel, PretrainedConfig, PreTrainedModel
from transformers.modeling_outputs import TokenClassifierOutput

from crf_bio import CRF


class PIIModelConfig(PretrainedConfig):
    model_type = "pii_hwan"

    def __init__(
        self,
        backbone_model_id: str = "skt/A.X-Encoder-base",
        num_labels: int = 39,
        use_crf: bool = False,
        use_gazetteer: bool = False,
        num_gaz_labels: int = 19,
        gaz_emb_dim: int = 32,
        hidden_dropout_prob: float = 0.1,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.backbone_model_id = backbone_model_id
        self.num_labels = num_labels
        self.use_crf = use_crf
        self.use_gazetteer = use_gazetteer
        self.num_gaz_labels = num_gaz_labels
        self.gaz_emb_dim = gaz_emb_dim
        self.hidden_dropout_prob = hidden_dropout_prob


def compact_sequence(tensor: torch.Tensor, mask: torch.Tensor):
    B = mask.shape[0]
    lengths = mask.sum(dim=1)
    Tc = max(int(lengths.max().item()), 1) if lengths.numel() else 1
    out_shape = (B, Tc) + tuple(tensor.shape[2:])
    out = tensor.new_zeros(out_shape)
    compact_mask = torch.zeros((B, Tc), dtype=torch.bool, device=tensor.device)
    for b in range(B):
        idx = mask[b].nonzero(as_tuple=True)[0]
        n = idx.numel()
        if n == 0:
            continue
        out[b, :n] = tensor[b, idx]
        compact_mask[b, :n] = True
    return out, compact_mask


class TokenClassifierForPII(PreTrainedModel):
    config_class = PIIModelConfig
    base_model_prefix = "backbone"

    def __init__(self, config: PIIModelConfig):
        super().__init__(config)
        config_origin_raw = getattr(config, "_name_or_path", "") or ""
        bundled_backbone_config = Path(config_origin_raw) / "backbone_config" if config_origin_raw else None
        backbone_source = config.backbone_model_id
        if bundled_backbone_config and (bundled_backbone_config / "config.json").exists():
            backbone_source = str(bundled_backbone_config)
        backbone_cfg = AutoConfig.from_pretrained(backbone_source, trust_remote_code=True)
        self.backbone = AutoModel.from_config(backbone_cfg, trust_remote_code=True)
        hidden_size = backbone_cfg.hidden_size
        self.use_gazetteer = config.use_gazetteer
        self.use_crf = config.use_crf
        classifier_in = hidden_size
        if self.use_gazetteer:
            self.gaz_proj = nn.Sequential(
                nn.Linear(config.num_gaz_labels, config.gaz_emb_dim),
                nn.Tanh(),
            )
            classifier_in += config.gaz_emb_dim
        self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.classifier = nn.Linear(classifier_in, config.num_labels)
        if self.use_crf:
            self.crf = CRF(config.num_labels, batch_first=True)
            if config.id2label:
                id2label = {int(k): v for k, v in config.id2label.items()}
                self.crf.init_bio_constraints(id2label)
        self.post_init()

    def _init_weights(self, module):
        pass

    def get_input_embeddings(self):
        return self.backbone.get_input_embeddings()

    def set_input_embeddings(self, value):
        self.backbone.set_input_embeddings(value)

    def _encode(self, input_ids, attention_mask):
        outputs = self.backbone(input_ids=input_ids, attention_mask=attention_mask)
        hidden = getattr(outputs, "last_hidden_state", None)
        if hidden is None:
            hidden = outputs[0]
        hidden = hidden.to(self.classifier.weight.dtype)
        return hidden

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        labels: Optional[torch.Tensor] = None,
        gaz_features: Optional[torch.Tensor] = None,
        label_mask: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        hidden = self._encode(input_ids, attention_mask)
        hidden = self.dropout(hidden)
        if self.use_gazetteer:
            if gaz_features is None:
                gaz_features = hidden.new_zeros(
                    (hidden.shape[0], hidden.shape[1], self.config.num_gaz_labels)
                )
            gaz_emb = self.gaz_proj(gaz_features.to(hidden.dtype))
            hidden = torch.cat([hidden, gaz_emb], dim=-1)
        logits = self.classifier(hidden)
        loss = None
        if labels is not None:
            if self.use_crf:
                lm = label_mask if label_mask is not None else (labels != -100)
                compact_logits, compact_mask = compact_sequence(logits, lm)
                compact_labels, _ = compact_sequence(labels.unsqueeze(-1), lm)
                compact_labels = compact_labels.squeeze(-1)
                loss = -self.crf(
                    compact_logits, compact_labels, mask=compact_mask, reduction="mean"
                )
            else:
                loss = nn.functional.cross_entropy(
                    logits.view(-1, logits.size(-1)), labels.view(-1), ignore_index=-100
                )
        return TokenClassifierOutput(loss=loss, logits=logits)

    @torch.inference_mode()
    def predict_tags(
        self,
        input_ids,
        attention_mask,
        label_mask: torch.Tensor,
        gaz_features: Optional[torch.Tensor] = None,
    ):
        out = self.forward(
            input_ids=input_ids, attention_mask=attention_mask, gaz_features=gaz_features
        )
        logits = out["logits"]
        if self.use_crf:
            compact_logits, compact_mask = compact_sequence(logits, label_mask)
            return self.crf.decode(compact_logits, mask=compact_mask)
        preds = logits.argmax(-1)
        result = []
        for b in range(preds.shape[0]):
            idx = label_mask[b].nonzero(as_tuple=True)[0]
            result.append(preds[b, idx].tolist())
        return result


def build_model(
    backbone_model_id: str,
    num_labels: int,
    id2label: dict,
    label2id: dict,
    use_crf: bool = False,
    use_gazetteer: bool = False,
    num_gaz_labels: int = 19,
    gaz_emb_dim: int = 32,
) -> TokenClassifierForPII:
    config = PIIModelConfig(
        backbone_model_id=backbone_model_id,
        num_labels=num_labels,
        use_crf=use_crf,
        use_gazetteer=use_gazetteer,
        num_gaz_labels=num_gaz_labels,
        gaz_emb_dim=gaz_emb_dim,
        id2label=id2label,
        label2id=label2id,
    )
    model = TokenClassifierForPII(config)
    pretrained_backbone = AutoModel.from_pretrained(
        backbone_model_id, trust_remote_code=True, torch_dtype=torch.float32
    )
    model.backbone.load_state_dict(pretrained_backbone.state_dict(), strict=False)
    del pretrained_backbone
    return model.to(torch.float32)
