"""Shared model family for controlled KDPII-19 comparisons."""
from __future__ import annotations

from pathlib import Path

import torch
from torch import nn
from transformers import AutoConfig, AutoModel

from crf import CRF
from kdpii import BIO, TYPES


MODEL_SPECS = {
    "xlmr_large": {"backbone": "xlm-roberta-large", "gazetteer": False, "crf": False, "lr": 1e-5},
    "klue_roberta": {"backbone": "klue-roberta-base", "gazetteer": False, "crf": False, "lr": 2e-5},
    "ax_encoder": {"backbone": "A.X-Encoder-base_original", "gazetteer": False, "crf": False, "lr": 2e-5},
    "ax_crf": {"backbone": "A.X-Encoder-base_original", "gazetteer": False, "crf": True, "lr": 2e-5},
    "ax_gazetteer": {"backbone": "A.X-Encoder-base_original", "gazetteer": True, "crf": False, "lr": 2e-5},
    "ax_gazetteer_crf": {"backbone": "A.X-Encoder-base_original", "gazetteer": True, "crf": True, "lr": 2e-5},
}


class GazetteerTrie:
    class Node:
        __slots__ = ("children", "labels")

        def __init__(self):
            self.children = {}
            self.labels = set()

    def __init__(self, entries: dict[str, list[str]]):
        self.root = self.Node()
        self.label_to_id = {label: index for index, label in enumerate(TYPES)}
        for label, forms in entries.items():
            for form in forms:
                if not form:
                    continue
                node = self.root
                for character in form:
                    node = node.children.setdefault(character, self.Node())
                node.labels.add(label)

    def features(self, text: str, offsets: list[tuple[int, int]]) -> list[list[float]]:
        char_labels = [set() for _ in text]
        for start in range(len(text)):
            node, cursor, best = self.root, start, None
            while cursor < len(text) and text[cursor] in node.children:
                node = node.children[text[cursor]]
                cursor += 1
                if node.labels:
                    best = (cursor, set(node.labels))
            if best:
                end, labels = best
                for position in range(start, end):
                    char_labels[position].update(labels)
        result = []
        for begin, end in offsets:
            vector = [0.0] * len(TYPES)
            for position in range(max(0, begin), min(len(text), end)):
                for label in char_labels[position]:
                    vector[self.label_to_id[label]] = 1.0
            result.append(vector)
        return result


class PIITagger(nn.Module):
    def __init__(self, backbone_path: Path, use_gazetteer: bool, use_crf: bool,
                 gradient_checkpointing: bool = True):
        super().__init__()
        architecture = AutoConfig.from_pretrained(backbone_path, local_files_only=True)
        if architecture.model_type == "modernbert":
            architecture.reference_compile = False
        load_options = {"add_pooling_layer": False} if architecture.model_type in {"roberta", "xlm-roberta"} else {}
        self.backbone = AutoModel.from_config(architecture, dtype=torch.float32, **load_options)
        if gradient_checkpointing and self.backbone.supports_gradient_checkpointing:
            self.backbone.gradient_checkpointing_enable()
        hidden = self.backbone.config.hidden_size
        dropout = getattr(self.backbone.config, "hidden_dropout_prob", 0.1)
        self.dropout = nn.Dropout(dropout)
        self.use_gazetteer = use_gazetteer
        self.use_crf = use_crf
        if use_gazetteer:
            self.gazetteer_projection = nn.Sequential(nn.Linear(len(TYPES), 32), nn.GELU())
            hidden += 32
        self.classifier = nn.Linear(hidden, len(BIO))
        self.crf = CRF() if use_crf else None

    def forward(self, input_ids, attention_mask, gazetteer):
        hidden = self.backbone(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        if self.use_gazetteer:
            hidden = torch.cat((hidden, self.gazetteer_projection(gazetteer)), dim=-1)
        return self.classifier(self.dropout(hidden))


def parameter_counts(model: nn.Module) -> dict[str, int]:
    return {
        "total": sum(parameter.numel() for parameter in model.parameters()),
        "trainable": sum(parameter.numel() for parameter in model.parameters() if parameter.requires_grad),
    }
