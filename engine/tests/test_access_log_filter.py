"""상태 확인 폴링의 성공 응답은 접근 로그에 남기지 않는다(app/main.py _QuietPollingFilter).

앱의 판단 폴링(0.5초)·연결 화면(1.5초)·확장 /health 가 로그를 분당 수백 줄씩 키웠다. 엔진
로그가 파이프로 가던 시절에는 이게 파이프를 채워 엔진을 멈추게 한 원인이기도 했다(맥 실측).
"""

import logging
import logging.config

from app.main import _QuietPollingFilter, _uvicorn_log_config


def _access(path: str, status: int, method: str = "GET") -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", method, path, "1.1", status), None,
    )


def test_quiets_successful_polling_but_keeps_everything_else():
    f = _QuietPollingFilter()
    assert not f.filter(_access("/health", 200))
    assert not f.filter(_access("/decisions", 200))
    assert not f.filter(_access("/proxy/extension-route", 200, "POST"))
    assert not f.filter(_access("/health?ts=1", 200))
    assert f.filter(_access("/health", 500)), "실패 응답은 원인 추적에 필요하다"
    assert f.filter(_access("/jobs/prompt", 200))
    other = logging.LogRecord("x", logging.INFO, __file__, 1, "plain message", None, None)
    assert f.filter(other)


def test_uvicorn_log_config_applies_the_filter():
    cfg = _uvicorn_log_config()
    assert cfg["loggers"]["uvicorn.access"]["filters"] == ["quiet_polling"]
    logging.config.dictConfig(cfg)  # 실제로 적용되는 모양인지(잘못되면 예외)
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(flt, _QuietPollingFilter) for flt in access.filters)


def test_quiets_decision_watch_but_keeps_decisions_and_failures():
    # 판단 창이 떠 있는 동안 앱이 1초마다 보는 GET /decisions/<id> — 한 건에 약 120줄이 쌓였다.
    f = _QuietPollingFilter()
    assert not f.filter(_access("/decisions/abc123", 200))
    assert f.filter(_access("/decisions/abc123", 200, "POST")), "사람의 결정은 남긴다"
    assert f.filter(_access("/decisions/abc123", 404)), "사라진 건(404)은 남긴다"
