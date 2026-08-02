# PII Local Engine Handoff: SKT CRF+Gaz x3 Mix-all

This bundle is the deployable local-app PII detector.

It is **not** the previous single `seed44` handoff. This package uses the final `SKT CRF+Gaz x3` Mix-all ensemble:

- `seed42`
- `seed43`
- `seed44`

The runtime loads all three single checkpoints and applies entity-level majority voting. A PII span is accepted when at least 2 of 3 models detect the same label on an overlapping character span.

## Model Identity

- Task: Korean PII entity detection
- Backbone: `skt/A.X-Encoder-base`
- Architecture: encoder token classifier + gazetteer features + CRF decoding
- Ensemble: 3 independently saved seed checkpoints
- Default voting: `min_votes=2`
- Training recipe: `SKT CRF+Gaz x3`, `Mix-all`
- Training data: KDPII + clean synthetic Mix-all split used by the real pipeline

Verified evaluation metrics from the experiment artifacts:

| Evaluation split | Precision | Recall | F1 |
|---|---:|---:|---:|
| Combined full19 | 99.21 | 95.74 | 97.45 |
| KDPII full19 | 97.46 | 95.32 | 96.38 |
| Synthetic full19 | 99.58 | 95.83 | 97.67 |

## Bundle Layout

```text
pii_engine/
  manifest.json
  README.md
  models/
    seed42/
    seed43/
    seed44/
  runtime/
    local_pii_ensemble_inference.py
    local_pii_inference.py
    pii_model.py
    crf_bio.py
    gazetteer.py
    requirements.txt
```

Each seed folder contains its tokenizer, weights, PII config, label map, gazetteer, and bundled backbone config.

## Install

Use Python 3.10+ when possible.

```bash
cd pii_engine/runtime
python3 -m pip install -r requirements.txt
```

For a CPU-only app environment, install a CPU build of PyTorch appropriate for the target OS before installing the requirements.

## One-shot Inference

```bash
cd pii_engine/runtime
python3 local_pii_ensemble_inference.py \
  --ensemble-dir .. \
  --device cpu \
  --text "홍길동 고객님의 연락처는 010-1234-5678 입니다."
```

Output:

```json
{
  "entities": [
    {"form": "홍길동", "label": "PS_NAME", "begin": 0, "end": 3},
    {"form": "010-1234-5678", "label": "QT_MOBILE", "begin": 14, "end": 27}
  ]
}
```

## Long-running App Mode

Start the detector once:

```bash
cd pii_engine/runtime
python3 local_pii_ensemble_inference.py --ensemble-dir .. --device cpu --stdio
```

Send one JSON object per line:

```json
{"id":"req-1","text":"홍길동 고객님의 연락처는 010-1234-5678 입니다."}
{"id":"req-2","texts":["김민수입니다.","서울특별시 강남구 테헤란로 123"]}
```

Receive one JSON object per line:

```json
{"id":"req-1","entities":[{"form":"홍길동","label":"PS_NAME","begin":0,"end":3},{"form":"010-1234-5678","label":"QT_MOBILE","begin":14,"end":27}]}
{"id":"req-2","entities":[[{"form":"김민수","label":"PS_NAME","begin":0,"end":3}],[{"form":"서울특별시 강남구 테헤란로 123","label":"LC_ADDRESS","begin":0,"end":20}]]}
```

## Integration Contract

The engine should call the Python sidecar in `--stdio` mode and keep the process alive.

Input schema:

```json
{"id":"string-request-id","text":"single text"}
```

or:

```json
{"id":"string-request-id","texts":["text 1","text 2"]}
```

Output schema:

```json
{"id":"string-request-id","entities":[{"form":"string","label":"PII_LABEL","begin":0,"end":3}]}
```

For batched input, `entities` is a list of entity lists in the same order as `texts`.

Character offsets are Python string offsets. Treat `begin` as inclusive and `end` as exclusive.

## Recommended Local-app Defaults

- Use `--device cpu` for maximum portability.
- Use `--batch-size 4` or lower for low-memory machines.
- Use `--max-length 256` unless the product regularly sends longer text chunks.
- Chunk long documents before inference and map offsets back to the original document.
- Redact or mask only the returned spans; do not transform the whole text with regex alone.

## Notes for the Implementing LLM

Implement a small adapter around `local_pii_ensemble_inference.py --stdio`.

The adapter should:

1. Start the Python process when the app or service starts.
2. Wait for the first `{"ready": true, ...}` line.
3. Send requests as JSON Lines.
4. Parse `entities`.
5. Replace spans from right to left so offsets remain valid.
6. Restart the sidecar if it exits unexpectedly.

Do not retrain the model in the local app. The model files in `models/seed42`, `models/seed43`, and `models/seed44` are the deployable artifacts.
