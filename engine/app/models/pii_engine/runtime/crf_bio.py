"""
Linear-chain CRF (외부 의존성 없음).

BIO 전이 제약(init_bio_constraints) 포함.
"""

from typing import List, Optional

import torch
import torch.nn as nn

IMPOSSIBLE = -10000.0


class CRF(nn.Module):
    def __init__(self, num_tags: int, batch_first: bool = True):
        if num_tags <= 0:
            raise ValueError(f"num_tags must be positive, got {num_tags}")
        super().__init__()
        self.num_tags = num_tags
        self.batch_first = batch_first
        self.start_transitions = nn.Parameter(torch.empty(num_tags))
        self.end_transitions = nn.Parameter(torch.empty(num_tags))
        self.transitions = nn.Parameter(torch.empty(num_tags, num_tags))
        self.reset_parameters()

    def reset_parameters(self):
        nn.init.uniform_(self.start_transitions, -0.1, 0.1)
        nn.init.uniform_(self.end_transitions, -0.1, 0.1)
        nn.init.uniform_(self.transitions, -0.1, 0.1)

    def init_bio_constraints(self, id2label: dict):
        with torch.no_grad():
            n = self.num_tags
            for i in range(n):
                tag_i = id2label[i]
                if tag_i.startswith("I-"):
                    self.start_transitions[i] = IMPOSSIBLE
                for j in range(n):
                    tag_j = id2label[j]
                    if tag_j.startswith("I-"):
                        ok = (tag_i == tag_j) or (
                            tag_i.startswith("B-") and tag_i[2:] == tag_j[2:]
                        )
                        if not ok:
                            self.transitions[i, j] = IMPOSSIBLE

    def forward(
        self,
        emissions: torch.Tensor,
        tags: torch.LongTensor,
        mask: Optional[torch.ByteTensor] = None,
        reduction: str = "mean",
    ) -> torch.Tensor:
        if mask is None:
            mask = torch.ones_like(tags, dtype=torch.uint8)
        if self.batch_first:
            emissions = emissions.transpose(0, 1)
            tags = tags.transpose(0, 1)
            mask = mask.transpose(0, 1)
        mask = mask.to(torch.uint8)
        numerator = self._score(emissions, tags, mask)
        denominator = self._partition(emissions, mask)
        llh = numerator - denominator
        if reduction == "none":
            return llh
        if reduction == "sum":
            return llh.sum()
        if reduction == "token_mean":
            return llh.sum() / mask.float().sum()
        return llh.mean()

    def decode(
        self, emissions: torch.Tensor, mask: Optional[torch.ByteTensor] = None
    ) -> List[List[int]]:
        if mask is None:
            mask = emissions.new_ones(emissions.shape[:2], dtype=torch.uint8)
        if self.batch_first:
            emissions = emissions.transpose(0, 1)
            mask = mask.transpose(0, 1)
        mask = mask.to(torch.uint8)
        return self._viterbi(emissions, mask)

    def _score(self, emissions, tags, mask):
        seq_len, batch = tags.shape
        score = self.start_transitions[tags[0]]
        score += emissions[0].gather(1, tags[0].unsqueeze(1)).squeeze(1)
        for i in range(1, seq_len):
            score += self.transitions[tags[i - 1], tags[i]] * mask[i].float()
            emit = emissions[i].gather(1, tags[i].unsqueeze(1)).squeeze(1)
            score += emit * mask[i].float()
        seq_ends = mask.long().sum(0) - 1
        last_tags = tags[seq_ends, torch.arange(batch, device=tags.device)]
        score += self.end_transitions[last_tags]
        return score

    def _partition(self, emissions, mask):
        seq_len = emissions.size(0)
        score = self.start_transitions + emissions[0]
        for i in range(1, seq_len):
            broadcast_score = score.unsqueeze(2)
            broadcast_emit = emissions[i].unsqueeze(1)
            next_score = broadcast_score + self.transitions + broadcast_emit
            next_score = torch.logsumexp(next_score, dim=1)
            score = torch.where(mask[i].bool().unsqueeze(1), next_score, score)
        score += self.end_transitions
        return torch.logsumexp(score, dim=1)

    def _viterbi(self, emissions, mask):
        seq_len, batch = emissions.shape[:2]
        score = self.start_transitions + emissions[0]
        history = []
        for i in range(1, seq_len):
            broadcast_score = score.unsqueeze(2)
            broadcast_emit = emissions[i].unsqueeze(1)
            next_score = broadcast_score + self.transitions + broadcast_emit
            next_score, indices = next_score.max(dim=1)
            score = torch.where(mask[i].bool().unsqueeze(1), next_score, score)
            history.append(indices)
        score += self.end_transitions
        seq_ends = mask.long().sum(0) - 1
        best_paths = []
        for b in range(batch):
            best_last_tag = score[b].argmax().item()
            best_tags = [best_last_tag]
            end = seq_ends[b].item()
            for hist in reversed(history[:end]):
                best_last_tag = hist[b][best_tags[-1]].item()
                best_tags.append(best_last_tag)
            best_tags.reverse()
            best_paths.append(best_tags)
        return best_paths
