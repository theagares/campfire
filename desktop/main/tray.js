'use strict';
/**
 * main/tray.js — 시스템 트레이 상주 + 팝오버 창 (PLAN §8 트레이 팝업).
 *
 * 트레이 아이콘 클릭 시 프레임 없는 팝오버 창(renderer/tray.html)을 트레이 근처에 띄운다.
 * 구성(§8): Security ON/OFF, PII/INJECTION 모델 pill, CPU/GPU/RAM/VRAM 바, 오늘 탐지 카운트,
 *          "대시보드에서 더 보기 →", "QUIT Campfire".
 * macOS 는 메뉴바, Windows 는 시스템 트레이. 우클릭 시 최소 네이티브 메뉴도 제공.
 */

const fs = require('fs');
const path = require('path');
const { Tray, BrowserWindow, Menu, nativeImage, screen, systemPreferences } = require('electron');

const POPOVER_W = 300;
const POPOVER_H = 460; // ACTIVE MODEL 섹션 + 2×2 리소스 그리드(서브로우 포함) 반영해 확장

// 트레이 애니메이션 프레임. assets/tray-frames/manifest.json 이 프레임 목록과 재생
// 간격을 들고 있어 이 파일에 하드코딩하지 않는다.
const FRAMES_DIR = path.join(__dirname, '..', 'assets', 'tray-frames');

class TrayController {
  constructor(app, { onShowDashboard, onQuit }) {
    this.app = app;
    this.onShowDashboard = onShowDashboard;
    this.onQuit = onQuit;
    this.tray = null;
    this.popover = null;
    // 불꽃 애니메이션 상태. frames 가 없으면(윈도우·에셋 없음) 정지 아이콘으로 남는다.
    this.frames = null;      // { idle: [nativeImage], busy: [nativeImage] }
    this.frameIntervalMs = 100;
    this.animState = 'idle';
    this.animIndex = 0;
    this.animTimer = null;
  }

  create() {
    // 플랫폼마다 트레이 아이콘 규칙이 정반대라 에셋을 나눠 쓴다.
    //
    // macOS: 템플릿 이미지 — setTemplateImage(true) 가 알파를 마스크로 쓰고 RGB 는
    //   무시한다. 그래서 tray-icon.png 는 검정 실루엣 + 투명 배경이어야 하고, OS 가
    //   메뉴바 테마에 맞춰 알아서 반전한다. 배경까지 불투명한 PNG 를 넘기면 아이콘이
    //   통짜 사각형으로 칠해지는 버그가 있었다.
    // Windows: 템플릿 반전을 해주지 않는다 — 같은 검정 실루엣을 쓰면 Windows 11 기본
    //   다크 작업표시줄에서 거의 안 보인다. 그래서 흰색으로 그린 별도 에셋을 쓴다.
    const base = process.platform === 'darwin' ? 'tray-icon' : 'tray-icon-win';
    const iconPath = path.join(__dirname, '..', 'assets', `${base}.png`);
    let image = nativeImage.createFromPath(iconPath); // 같은 폴더의 <base>@2x.png 를 레티나용으로 자동 인식
    if (image.isEmpty()) {
      image = nativeImage.createEmpty();
    }
    // 에셋이 이미 최종 크기(mac 22 / win 16)와 @2x 로 준비돼 있어 리사이즈하지 않는다 —
    // 예전엔 큰 원본을 런타임에 줄여 써서 가장자리가 뭉갰다.
    if (process.platform === 'darwin') image.setTemplateImage(true);
    this.tray = new Tray(image);
    this.tray.setToolTip('Campfire — 로컬 보안 게이트웨이');

    this.tray.on('click', () => this.togglePopover());
    this.tray.on('right-click', () => this._showContextMenu());
    this._startAnimation();
    this._buildPopover();
  }

  // ── 불꽃 애니메이션 ────────────────────────────────────────────────────────
  // 평상시엔 잔잔하게(idle), 문서를 검사하는 동안엔 세게(busy) 타오른다. 상태는
  // 엔진의 처리현황 SSE 에서 온다(main.js 가 setBusy 로 알려준다).
  //
  // macOS 전용이다. Windows 용 프레임은 만들지 않았고, 무엇보다 작업표시줄 아이콘은
  // 템플릿 반전이 없어 같은 검정 실루엣을 그대로 쓸 수 없다. 프레임이 없으면 이 함수는
  // 조용히 물러나고 기존 정지 아이콘이 그대로 남는다.

  /** manifest 를 읽어 프레임을 nativeImage 로 미리 만들어 둔다(재생 중 디스크 접근 0). */
  _loadFrames() {
    if (process.platform !== 'darwin') return null;
    let manifest;
    try {
      manifest = JSON.parse(fs.readFileSync(path.join(FRAMES_DIR, 'manifest.json'), 'utf-8'));
    } catch {
      return null; // 에셋이 없는 체크아웃 — 정지 아이콘으로 동작
    }
    const spec = manifest && manifest.mac;
    if (!spec || !Array.isArray(spec.idle) || !spec.idle.length) return null;

    const toImages = (names) => {
      const out = [];
      for (const name of names || []) {
        // 같은 폴더의 <name>@2x.png 를 레티나용으로 nativeImage 가 알아서 집어온다.
        const img = nativeImage.createFromPath(path.join(FRAMES_DIR, name));
        if (img.isEmpty()) return null; // 한 장이라도 깨졌으면 애니메이션을 켜지 않는다
        img.setTemplateImage(true);
        out.push(img);
      }
      return out.length ? out : null;
    };

    const idle = toImages(spec.idle);
    if (!idle) return null;
    const busy = toImages(spec.busy) || idle; // busy 세트가 없으면 idle 을 그대로 쓴다
    if (Number.isFinite(manifest.frameIntervalMs) && manifest.frameIntervalMs > 0) {
      this.frameIntervalMs = manifest.frameIntervalMs;
    }
    return { idle, busy };
  }

  _startAnimation() {
    this.frames = this._loadFrames();
    if (!this.frames) return;

    // macOS "동작 줄이기"를 켠 사용자에겐 첫 프레임만 정지 상태로 보여준다. 메뉴바에서
    // 계속 움직이는 아이콘은 이 설정을 켠 이유 그 자체다.
    if (this._prefersReducedMotion()) {
      this._drawFrame();
      return;
    }
    this.animTimer = setInterval(() => {
      this.animIndex += 1;
      this._drawFrame();
    }, this.frameIntervalMs);
  }

  _prefersReducedMotion() {
    try {
      return !!systemPreferences.getAnimationSettings().prefersReducedMotion;
    } catch {
      return false; // 이 API 가 없는 플랫폼/버전이면 애니메이션을 막지 않는다
    }
  }

  _drawFrame() {
    if (!this.frames || !this.tray || this.tray.isDestroyed()) return;
    const set = this.frames[this.animState] || this.frames.idle;
    this.tray.setImage(set[this.animIndex % set.length]);
  }

  /** 검사 중인가. main.js 가 처리현황 구독 결과를 넘겨준다. */
  setBusy(busy) {
    const next = busy ? 'busy' : 'idle';
    if (next === this.animState) return;
    this.animState = next;
    // 세기가 바뀌는 순간은 불꽃 모양이 튀지 않게 프레임 위상을 유지한 채 세트만 바꾼다.
    this._drawFrame();
  }

  _buildPopover() {
    this.popover = new BrowserWindow({
      width: POPOVER_W,
      height: POPOVER_H,
      show: false,
      frame: false,
      resizable: false,
      movable: false,
      fullscreenable: false,
      skipTaskbar: true,
      transparent: true,
      alwaysOnTop: true,
      // macOS 전용 네이티브 프로스티드 글래스(Figma 디자인의 "애플 글래스" 룩) — CSS
      // backdrop-filter 는 창 자신의 콘텐츠만 블러하지만, vibrancy 는 창 뒤 실제 데스크탑을
      // OS 컴포지터가 진짜로 블러/채도 처리해서 보여준다(Windows 에선 무시되는 옵션).
      ...(process.platform === 'darwin' ? { vibrancy: 'popover', visualEffectState: 'active' } : {}),
      webPreferences: {
        preload: path.join(__dirname, 'tray-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    this.popover.loadFile(path.join(__dirname, '..', 'renderer', 'tray.html'));
    // 포커스 잃으면 닫기 (네이티브 팝오버 UX)
    this.popover.on('blur', () => {
      if (this.popover && !this.popover.webContents.isDevToolsOpened()) {
        this.popover.hide();
      }
    });
  }

  togglePopover() {
    if (!this.popover) this._buildPopover();
    if (this.popover.isVisible()) {
      this.popover.hide();
      return;
    }
    this._positionPopover();
    this.popover.show();
    this.popover.focus();
  }

  _positionPopover() {
    try {
      const bounds = this.tray.getBounds();
      const display = screen.getDisplayNearestPoint({ x: bounds.x, y: bounds.y });
      const area = display.workArea;
      let x = Math.round(bounds.x + bounds.width / 2 - POPOVER_W / 2);
      let y;
      if (process.platform === 'darwin') {
        y = Math.round(bounds.y + bounds.height + 4); // 메뉴바 아래
      } else {
        y = Math.round(area.y + area.height - POPOVER_H - 8); // 작업표시줄 위
        x = Math.round(bounds.x + bounds.width / 2 - POPOVER_W / 2);
      }
      // 화면 밖으로 나가지 않게 보정
      x = Math.min(Math.max(x, area.x + 4), area.x + area.width - POPOVER_W - 4);
      y = Math.min(Math.max(y, area.y + 4), area.y + area.height - POPOVER_H - 4);
      this.popover.setPosition(x, y, false);
    } catch {
      /* 위치 계산 실패 시 기본 위치 */
    }
  }

  _showContextMenu() {
    const menu = Menu.buildFromTemplate([
      { label: '대시보드 열기', click: () => this.onShowDashboard && this.onShowDashboard() },
      { type: 'separator' },
      { label: 'QUIT Campfire', click: () => this.onQuit && this.onQuit() },
    ]);
    this.tray.popUpContextMenu(menu);
  }

  destroy() {
    if (this.animTimer) {
      clearInterval(this.animTimer);
      this.animTimer = null;
    }
    this.frames = null;
    if (this.popover && !this.popover.isDestroyed()) this.popover.destroy();
    if (this.tray && !this.tray.isDestroyed()) this.tray.destroy();
    this.popover = null;
    this.tray = null;
  }
}

module.exports = { TrayController };
