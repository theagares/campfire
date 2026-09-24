const MASKED_PROMPT = '연락처 [전화번호 마스킹]';
const MASKED_FILE_TEXT = 'MASKED_FILE_CONTENT [전화번호 마스킹]';

const multiSessions = new Map();

function toBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function maskedFile(payload = {}) {
  const original = String(payload.fileName || 'fixture.pdf');
  const dot = original.lastIndexOf('.');
  const fileName = dot > 0
    ? `${original.slice(0, dot)}_masked${original.slice(dot)}`
    : `${original}_masked`;
  return {
    action: 'upload',
    maskedBase64: toBase64(MASKED_FILE_TEXT),
    mimeType: payload.mimeType || 'application/pdf',
    fileName,
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'OPEN_PANEL') {
    sendResponse({ ok: true, qaHarness: true });
    return false;
  }

  if (message?.type === 'CLOSE_PANEL') {
    sendResponse({ ok: true });
    return false;
  }

  if (message?.type === 'START_MULTI_SCAN') {
    const session = {
      tabId: sender.tab?.id,
      leaseId: `qa_${message.sessionId}`,
      items: message.payload?.items || [],
    };
    multiSessions.set(message.sessionId, session);
    sendResponse({ ok: true, queued: true });
    setTimeout(() => {
      if (session.tabId == null) return;
      chrome.tabs.sendMessage(session.tabId, {
        type: 'SCAN_LEASE_GRANTED', sessionId: message.sessionId, leaseId: session.leaseId,
      }).catch(() => {});
    }, 0);
    return false;
  }

  if (message?.type === 'SCAN_MULTI_PROMPT') {
    sendResponse({ ok: true, prompt: { status: 'done', counts: { pii: 1, injection: 0 } } });
    return false;
  }

  if (message?.type === 'SCAN_MULTI_ITEM') {
    sendResponse({ ok: true, doc: { id: message.payload?.docId, status: 'done' } });
    return false;
  }

  if (message?.type === 'FINISH_MULTI_SCAN') {
    const session = multiSessions.get(message.sessionId);
    sendResponse({ ok: !!session });
    if (session?.tabId != null) {
      setTimeout(() => chrome.tabs.sendMessage(session.tabId, {
        type: 'CONTENT_BATCH_DECISION',
        sessionId: message.sessionId,
        decision: {
          action: 'send', promptText: MASKED_PROMPT,
          files: session.items.map((item) => ({
            id: item.id, action: 'masked', artifactId: `${message.sessionId}:${item.id}`,
          })),
        },
      }).catch(() => {}), 25);
    }
    return false;
  }

  if (message?.type === 'GET_SCAN_ARTIFACT') {
    const session = multiSessions.get(message.sessionId);
    const item = session?.items.find((candidate) => candidate.id === message.docId);
    const file = item && maskedFile({ fileName: item.fileName, mimeType: item.mimeType });
    sendResponse(file
      ? { ok: true, base64: file.maskedBase64, mimeType: file.mimeType, fileName: file.fileName }
      : { ok: false });
    return false;
  }

  if (message?.type === 'ACK_SCAN_ARTIFACT') {
    sendResponse({ ok: true });
    return false;
  }

  if (message?.type === 'FINALIZE_MULTI_SESSION') {
    multiSessions.delete(message.sessionId);
    sendResponse({ ok: true });
    return false;
  }

  if (message?.type !== 'START_SCAN') return false;

  const { sessionId, kind, payload = {} } = message;
  let decision;
  if (kind === 'combined') {
    decision = {
      action: 'send',
      maskedText: MASKED_PROMPT,
      file: maskedFile(payload),
    };
  } else if (kind === 'file') {
    decision = maskedFile(payload);
  } else {
    decision = { action: 'masked', maskedText: MASKED_PROMPT };
  }

  sendResponse({ ok: true, qaHarness: true });
  setTimeout(() => {
    if (sender.tab?.id == null) return;
    chrome.tabs.sendMessage(sender.tab.id, {
      type: 'PANEL_DECISION',
      sessionId,
      kind,
      decision,
    }).catch(() => {});
  }, 25);
  return false;
});
