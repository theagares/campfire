import sys
from pathlib import Path

# engine/ 를 import 경로에 추가 (app 패키지)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
