"""BIO-constrained linear-chain CRF with FP32 dynamic programming."""
import torch
from torch import nn
from torch.nn.utils.rnn import pad_sequence

from .kdpii import BIO, BIO_ID


@torch.jit.script
def partition(emissions: torch.Tensor, mask: torch.Tensor, start: torch.Tensor,
              end: torch.Tensor, transitions: torch.Tensor):
    score = start[None, :] + emissions[:, 0]
    for timestep in range(1, emissions.shape[1]):
        nxt = torch.logsumexp(score[:, :, None] + transitions[None, :, :] + emissions[:, timestep, None, :], dim=1)
        score = torch.where(mask[:, timestep, None], nxt, score)
    return torch.logsumexp(score + end[None, :], dim=1)


@torch.jit.script
def viterbi(emissions: torch.Tensor, mask: torch.Tensor, start: torch.Tensor,
            end: torch.Tensor, transitions: torch.Tensor):
    score = start[None, :] + emissions[:, 0]
    history = torch.zeros((emissions.shape[1], emissions.shape[0], emissions.shape[2]),
                          dtype=torch.long, device=emissions.device)
    for timestep in range(1, emissions.shape[1]):
        nxt, index = (score[:, :, None] + transitions[None, :, :]).max(dim=1)
        nxt = nxt + emissions[:, timestep]
        score = torch.where(mask[:, timestep, None], nxt, score)
        history[timestep] = index
    current = (score + end[None, :]).argmax(dim=1)
    lengths = mask.long().sum(1)
    paths = torch.zeros(mask.shape, dtype=torch.long, device=emissions.device)
    for timestep in range(emissions.shape[1] - 1, -1, -1):
        valid = timestep < lengths
        paths[:, timestep] = torch.where(valid, current, torch.zeros_like(current))
        if timestep > 0:
            previous = history[timestep].gather(1, current[:, None]).squeeze(1)
            current = torch.where(valid, previous, current)
    return paths


class CRF(nn.Module):
    def __init__(self):
        super().__init__()
        size = len(BIO)
        self.start_transitions = nn.Parameter(torch.zeros(size))
        self.end_transitions = nn.Parameter(torch.zeros(size))
        self.transitions = nn.Parameter(torch.zeros(size, size))
        start, transitions = torch.zeros(size), torch.zeros(size, size)
        for destination, tag in enumerate(BIO):
            if tag.startswith("I-"):
                start[destination] = -10000.0
                for source, previous in enumerate(BIO):
                    if previous not in ("B-" + tag[2:], "I-" + tag[2:]):
                        transitions[source, destination] = -10000.0
        self.register_buffer("start_constraint", start, persistent=False)
        self.register_buffer("transition_constraint", transitions, persistent=False)

    def scores(self):
        return (self.start_transitions + self.start_constraint,
                self.end_transitions,
                self.transitions + self.transition_constraint)

    def loss(self, emissions, labels):
        sequences, gold = [], []
        for row, values in enumerate(labels.detach().cpu().tolist()):
            begin = None
            for timestep in range(len(values) + 1):
                valid = timestep < len(values) and values[timestep] != -100
                if valid and begin is None:
                    begin = timestep
                if not valid and begin is not None:
                    sequences.append(emissions[row, begin:timestep])
                    tags = values[begin:timestep]
                    if BIO[tags[0]].startswith("I-"):
                        tags[0] = BIO_ID["B-" + BIO[tags[0]][2:]]
                    gold.append(torch.tensor(tags, device=emissions.device))
                    begin = None
        if not sequences:
            return emissions.sum() * 0.0
        lengths = torch.tensor([len(sequence) for sequence in sequences], device=emissions.device)
        packed = pad_sequence(sequences, batch_first=True).float()
        tags = pad_sequence(gold, batch_first=True)
        mask = torch.arange(packed.shape[1], device=emissions.device)[None, :] < lengths[:, None]
        start, end, transitions = self.scores()
        normalizer = partition(packed, mask, start, end, transitions)
        value = start[tags[:, 0]] + (packed.gather(2, tags[:, :, None]).squeeze(2) * mask).sum(1)
        value = value + (transitions[tags[:, :-1], tags[:, 1:]] * mask[:, 1:]).sum(1)
        last = mask.long().sum(1) - 1
        final_tag = tags.gather(1, last[:, None]).squeeze(1)
        return (normalizer - value - end[final_tag]).sum()

    @torch.no_grad()
    def decode(self, emissions, mask):
        start, end, transitions = self.scores()
        return viterbi(emissions.float(), mask, start, end, transitions)
