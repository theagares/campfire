"""관계 multi-label 분류와 관계별 PII 분류를 분리합니다."""

import torch
from torch import nn


def make_mlp(input_dim: int, hidden_dims: list[int], output_dim: int, dropout: float):
    layers = []
    for width in hidden_dims:
        layers.extend([nn.Linear(input_dim, width), nn.GELU(), nn.LayerNorm(width), nn.Dropout(dropout)])
        input_dim = width
    return nn.Sequential(*layers, nn.Linear(input_dim, output_dim))


class RelationPrivacyHeads(nn.Module):
    def __init__(self, pair_dim, relation_count, settings):
        super().__init__()
        self.relation_embeddings = nn.Embedding(relation_count, settings.relation_dim)
        self.relation = make_mlp(pair_dim, settings.hidden_dims, relation_count, settings.dropout)
        self.privacy = make_mlp(pair_dim + 2 * settings.relation_dim,
                                settings.hidden_dims, 1, settings.dropout)

    def forward(self, pair_features):
        logits = self.relation(pair_features)
        probabilities = logits.sigmoid()
        # 정답 relation은 입력하지 않습니다. 예측 확률을 통해 relation head로도 역전파됩니다.
        summary = probabilities @ self.relation_embeddings.weight
        summary = summary / probabilities.sum(-1, keepdim=True).clamp_min(1e-6)
        count = self.relation_embeddings.num_embeddings
        pairs = pair_features[:, None, :].expand(-1, count, -1)
        context = summary[:, None, :].expand(-1, count, -1)
        names = self.relation_embeddings.weight[None, :, :].expand(len(pairs), -1, -1)
        privacy = self.privacy(torch.cat((pairs, context, names), dim=-1)).squeeze(-1)
        return logits, privacy
