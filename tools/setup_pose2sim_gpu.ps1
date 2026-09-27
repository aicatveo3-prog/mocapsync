# Pose2Sim GPU 환경을 처음부터 다시 만듭니다.
#
# ★ 왜 스크립트로 남기는가
#   이 조합은 시행착오 끝에 찾은 것이라 버전 하나만 틀려도 조용히 CPU 로 돌아갑니다.
#     onnxruntime-gpu 1.30  -> CUDA 13 요구. 드라이버 566.14 는 CUDA 12 까지라 불가
#     nvidia-cudnn-cu12 9.26 -> ORT 1.22 와 안 맞음 (CUDNN_BACKEND_API_FAILED 후 CPU 로 후퇴)
#   재설치할 때 기억에 의존하지 않도록 정확한 버전을 여기 고정합니다.
#
# ★ 기존 ~/.venv/pose2sim (CPU, OpenVINO) 는 건드리지 않습니다. 별도 환경입니다.
#
# 사용:  powershell -ExecutionPolicy Bypass -File tools\setup_pose2sim_gpu.ps1
#
# 설치 후 Config.toml 에 반드시 둘 다 지정하세요 (device 만 주면 무시되고 CPU 로 갑니다):
#     backend = 'onnxruntime'
#     device  = 'CUDA'
# 그리고 Pose2Sim 을 부르기 전에 onnxruntime.preload_dlls() 를 호출해야
# pip 로 받은 CUDA DLL 을 찾습니다.

$ErrorActionPreference = "Stop"
$venv = "$env:USERPROFILE\.venv\pose2sim_gpu"
$py = "$venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    uv venv $venv --python 3.13
}

uv pip install --python $py "pose2sim==0.10.49"

# CPU 판을 지우고 GPU 판으로 교체 (둘이 같이 있으면 import 가 꼬입니다)
uv pip uninstall --python $py onnxruntime
uv pip install --python $py `
    "onnxruntime-gpu==1.22.0" `
    "nvidia-cuda-runtime-cu12==12.9.79" `
    "nvidia-cublas-cu12==12.9.2.10" `
    "nvidia-cufft-cu12==11.4.1.4" `
    "nvidia-curand-cu12==10.3.10.19" `
    "nvidia-cudnn-cu12==9.8.0.87"

# 실제로 GPU 에서 추론되는지 확인 (목록에 보이는 것만으로는 부족합니다)
& $py "$PSScriptRoot\ort_gpu_check.py"
