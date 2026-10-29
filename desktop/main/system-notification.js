'use strict';

const fs = require('fs');
const path = require('path');

// Electron Notification 은 EventEmitter 다. 네이티브 표시 결과를 관측할 동안
// 객체를 붙잡아 두고, 닫히거나 일정 시간이 지나면 놓는다.
const activeNotifications = new Set();
const RETAIN_MS = 10 * 60 * 1000;

function createFileLogger(filePath) {
  function write(level, message) {
    const line = `${new Date().toISOString()} ${level.toUpperCase()} ${message}\n`;
    try {
      fs.mkdirSync(path.dirname(filePath), { recursive: true });
      fs.appendFileSync(filePath, line, 'utf8');
    } catch (err) {
      console.error('[notification] 로그 저장 실패:', err.message);
    }
  }
  return {
    info: (message) => write('info', message),
    warn: (message) => write('warn', message),
    error: (message) => write('error', message),
  };
}

function showSystemNotification({
  Notification, title, body, platform = process.platform,
  logger = console, fallback = null,
}) {
  const useFallback = (reason) => {
    try { fallback?.(reason); } catch (err) { logger.error(`[notification] 대체 알림 실패: ${err.message}`); }
  };

  let supported = false;
  try { supported = Boolean(Notification?.isSupported?.()); } catch (err) {
    logger.error(`[notification] 지원 여부 확인 실패: ${err.message}`);
  }
  if (!supported) {
    logger.warn('[notification] 운영체제 알림을 지원하지 않음');
    useFallback('not-supported');
    return false;
  }

  const options = { title, body };
  if (platform === 'win32') {
    // 차단 이유는 전송 실패 직후 반드시 보여야 한다. Windows 기본 토스트는 빨리
    // 사라질 수 있으므로 중요 알림으로 정렬하고 사용자가 닫을 때까지 유지한다.
    options.urgency = 'critical';
    options.timeoutType = 'never';
  }

  try {
    const notification = new Notification(options);
    activeNotifications.add(notification);
    const release = () => activeNotifications.delete(notification);
    notification.once('show', () => logger.info(`[notification] 표시됨: ${title}`));
    notification.once('failed', (_event, error) => {
      release();
      const reason = String(error || 'unknown');
      logger.error(`[notification] 표시 실패: ${reason}`);
      useFallback(reason);
    });
    notification.once('close', release);
    const timer = setTimeout(release, RETAIN_MS);
    timer.unref?.();
    notification.show();
    return true;
  } catch (err) {
    logger.error(`[notification] 생성 실패: ${err.message}`);
    useFallback(err.message);
    return false;
  }
}

module.exports = { createFileLogger, showSystemNotification };
