"""
onnxruntime 가 실제로 GPU 로 추론하는지 확인합니다.

★ 왜 전용 스크립트가 필요한가

`ort.get_available_providers()` 에 'CUDAExecutionProvider' 가 보이는 것과
실제로 GPU 에서 돌아가는 것은 **완전히 다릅니다.** 이 프로젝트에서 실제로
세 단계로 나뉘어 실패했습니다.

  1단계  제공자 목록에도 없음        -> onnxruntime(CPU판)이 설치돼 있었음
  2단계  목록엔 있는데 세션 생성 실패 -> CUDA DLL 없음 (cudart64_13.dll 등)
  3단계  세션은 생기는데 실행 실패    -> cuDNN 버전 불일치, CPU 로 조용히 되돌아감

특히 3단계가 위험합니다. onnxruntime 은 실패하면 **경고만 찍고 CPU 로
되돌아가서 그냥 계속 돌아갑니다.** 결과는 나오므로 "GPU 를 쓰고 있다"고
착각하기 쉽습니다. 그래서 실제 추론까지 돌려 속도를 재야 합니다.

사용:
    python tools/ort_gpu_check.py [--model <onnx경로>] [--iters 30]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

#: rtmlib 이 내려받는 기본 위치. Pose2Sim 이 쓰는 모델과 같습니다.
DEFAULT_MODEL = (Path.home() / ".cache" / "rtmlib" / "hub" / "checkpoints"
                 / "rtmpose-x_simcc-body7_pt-body7-halpe26_700e-384x288-7fb6e239_20230606.onnx")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(DEFAULT_MODEL))
    ap.add_argument("--iters", type=int, default=30)
    a = ap.parse_args()

    import numpy as np
    import onnxruntime as ort

    print(f"onnxruntime {ort.__version__}")
    print(f"빌드된 제공자 목록: {ort.get_available_providers()}")

    # 1.21+ 에서 nvidia-* pip 패키지의 DLL 경로를 등록해 줍니다.
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
            print("preload_dlls() 호출됨")
        except Exception as e:  # noqa: BLE001
            print(f"preload_dlls 경고: {e}")

    model = Path(a.model)
    if not model.exists():
        print(f"★ 모델이 없습니다: {model}")
        print("  Pose2Sim 을 한 번 돌리면 자동으로 내려받습니다.")
        return 1

    results = {}
    for name in ("CUDAExecutionProvider", "CPUExecutionProvider"):
        if name not in ort.get_available_providers():
            print(f"\n[{name}] 빌드에 없음 — 건너뜁니다")
            continue
        print(f"\n[{name}]")
        try:
            # ★ 다른 제공자로 조용히 되돌아가지 못하게 **하나만** 지정합니다.
            #   ['CUDA','CPU'] 로 주면 실패해도 CPU 로 돌아가 성공처럼 보입니다.
            s = ort.InferenceSession(str(model), providers=[name])
        except Exception as e:  # noqa: BLE001
            print(f"  세션 생성 실패: {type(e).__name__}: {str(e)[:200]}")
            continue

        actual = s.get_providers()
        if name not in actual:
            print(f"  ★ 요청은 {name} 인데 실제는 {actual}")
            continue

        inp = s.get_inputs()[0]
        shape = [1 if (isinstance(d, str) or d is None) else d for d in inp.shape]
        x = np.random.rand(*shape).astype(np.float32)

        try:
            for _ in range(3):
                s.run(None, {inp.name: x})     # 워밍업 (커널 컴파일 포함)
        except Exception as e:  # noqa: BLE001
            print(f"  ★ 추론 실패: {type(e).__name__}")
            msg = str(e)
            print(f"    {msg[:300]}")
            if "CUDNN" in msg:
                print("    -> cuDNN 버전 불일치입니다. nvidia-cudnn-cu12 버전을 바꿔 보세요.")
            continue

        t = time.perf_counter()
        for _ in range(a.iters):
            s.run(None, {inp.name: x})
        dt = (time.perf_counter() - t) / a.iters
        results[name] = dt
        print(f"  1회 {dt * 1000:7.1f} ms   = {1 / dt:6.1f} fps")

    print()
    cu = results.get("CUDAExecutionProvider")
    cp = results.get("CPUExecutionProvider")
    if cu and cp:
        print(f"판정: GPU 가 CPU 보다 {cp / cu:.1f}배 빠릅니다.")
        if cp / cu < 1.5:
            print("      ★ 배율이 낮습니다. GPU 가 제대로 쓰이지 않을 수 있습니다.")
    elif cu:
        print("판정: GPU 는 동작합니다 (CPU 비교값 없음).")
    elif cp:
        print("판정: ★ GPU 를 쓸 수 없습니다. CPU 만 동작합니다.")
        return 2
    else:
        print("판정: ★ 어느 제공자도 동작하지 않았습니다.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
