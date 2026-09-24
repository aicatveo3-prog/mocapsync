"""
pytest 설정.

`server` 디렉터리를 import 경로에 넣습니다.

왜 필요한가: 지금까지는 테스트 전체를 한 번에 돌릴 때만 통과했습니다.
어떤 테스트 파일이 먼저 실행되면서 경로를 잡아줬기 때문인데,
파일 하나만 따로 돌리면 `ModuleNotFoundError: No module named 'mocapsync'` 가 났습니다.
디버깅 중에 한 파일만 돌리는 일이 잦으므로 여기서 명시적으로 잡습니다.
"""
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
