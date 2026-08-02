'use strict';
/**
 * main/tray.js — 시스템 트레이 상주 + 팝오버 창 (PLAN §8 트레이 팝업).
 *
 * 트레이 아이콘 클릭 시 프레임 없는 팝오버 창(renderer/tray.html)을 트레이 근처에 띄운다.
 * 구성(§8): Security ON/OFF, PII/INJECTION 모델 pill, CPU/GPU/RAM/VRAM 바, 오늘 탐지 카운트,
 *          "대시보드에서 더 보기 →", "QUIT Campfire".
 * macOS 는 메뉴바, Windows 는 시스템 트레이. 우클릭 시 최소 네이티브 메뉴도 제공.
 */

const path = require('path');
const { Tray, BrowserWindow, Menu, nativeImage, screen } = require('electron');

const POPOVER_W = 300;
const POPOVER_H = 460; // ACTIVE MODEL 섹션 + 2×2 리소스 그리드(서브로우 포함) 반영해 확장

class TrayController {
  constructor(app, { onShowDashboard, onQuit }) {
    this.app = app;
    this.onShowDashboard = onShowDashboard;
    this.onQuit = onQuit;
    this.tray = null;
    this.popover = null;
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
    this._buildPopover();
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
    if (this.popover && !this.popover.isDestroyed()) this.popover.destroy();
    if (this.tray && !this.tray.isDestroyed()) this.tray.destroy();
    this.popover = null;
    this.tray = null;
  }
}

module.exports = { TrayController };
