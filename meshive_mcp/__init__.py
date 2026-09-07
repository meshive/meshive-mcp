"""meshive-mcp — Meshive GPU Cloud 용 원격 MCP 서버.

`meshive` SDK(PyPI) 위의 얇은 도구 계층이다. 비즈니스 로직은 갖지 않고
(1) 요청의 Bearer API 키를 SDK 클라이언트에 실어 보내고
(2) 응답을 모델이 읽기 좋은 크기로 자르고
(3) 예외를 모델용 지시문(code/message/next_step)으로 번역한다.
"""
from ._version import __version__

__all__ = ["__version__"]
