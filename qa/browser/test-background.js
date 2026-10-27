const MASKED_PROMPT = '연락처 [전화번호 마스킹]';
const MASKED_FILE_TEXT = 'MASKED_FILE_CONTENT [전화번호 마스킹]';

const multiSessions = new Map();

// 진짜 서비스 워커(scanFile·scanCombined)처럼 받은 파일을 FormData 에 넣어 fetch 해 본다(QA_ECHO_URL 은
// run-extension-e2e.mjs 가 앞에 붙인다). 사용자가 고른 디스크 파일을 그대로 넘기면 이 fetch 가 "Failed to fetch"
// 로 끝나 엔진에 /jobs 가 가지 않았다(v0.2.53 맥 실측). 못 보내면 그 검사를 실패로 세운다.
async function readsOriginalFile(file) {
  if (!(file instanceof Blob)) return false;
  try {
    const form = new FormData();
    form.append('file', file, file.name || 'fixture');
    const res = await fetch(QA_ECHO_URL, { method: 'POST', body: form });
    return (await res.text()).includes('ORIGINAL_PRIVATE_FILE');
  } catch (_) {
    return false;
  }
}

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
    const session = multiSessions.get(message.sessionId);
    readsOriginalFile(message.payload?.file).then((read) => {
      if (!read && session) session.unreadable = true;
      sendResponse(read
        ? { ok: true, doc: { id: message.payload?.docId, status: 'done' } }
        : { ok: true, doc: { id: message.payload?.docId, status: 'error', error: 'QA: 서비스 워커가 파일 본문을 읽지 못함' } });
    });
    return true;
  }

  if (message?.type === 'FINISH_MULTI_SCAN') {
    const session = multiSessions.get(message.sessionId);
    sendResponse({ ok: !!session });
    if (session?.unreadable) return false; // 못 읽은 파일은 가린 파일로 바꿔 넣지 않는다 — 시험이 실패한다
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
  const decide = () => {
    const decision = kind === 'combined'
      ? { action: 'send', maskedText: MASKED_PROMPT, file: maskedFile(payload) }
      : kind === 'file' ? maskedFile(payload) : { action: 'masked', maskedText: MASKED_PROMPT };
    setTimeout(() => {
      if (sender.tab?.id == null) return;
      chrome.tabs.sendMessage(sender.tab.id, { type: 'PANEL_DECISION', sessionId, kind, decision }).catch(() => {});
    }, 25);
  };
  if (kind === 'file' || kind === 'combined') {
    readsOriginalFile(payload.file).then((read) => {
      sendResponse({ ok: true, qaHarness: true });
      if (read) decide(); // 못 읽으면 결정을 보내지 않는다 — 가린 파일이 안 들어가 시험이 실패한다
    });
    return true;
  }
  sendResponse({ ok: true, qaHarness: true });
  decide();
  return false;
});
