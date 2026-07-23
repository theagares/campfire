'use strict';
/**
 * main/tray.js — 시스템 트레이 상주 + 팝오버 창 (PLAN §8 트레이 팝업).
 *
 * 트레이 아이콘 클릭 시 프레임 없는 팝오버 창(renderer/tray.html)을 트레이 근처에 띄운다.
 * 구성(§8): Security ON/OFF, PII/INJECTION 모델 pill, CPU/GPU/RAM/VRAM 바, 오늘 탐지 카운트,
 *          "대시보드에서 더 보기 →", "QUIT UpSecurity".
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
    const iconPath = path.join(__dirname, '..', 'assets', 'tray-icon.png');
    let image = nativeImage.createFromPath(iconPath); // 같은 폴더의 tray-icon@2x.png 를 레티나용으로 자동 인식
    if (image.isEmpty()) {
      image = nativeImage.createEmpty();
    }
    // tray-icon.png 는 검정 라인아트 + 투명 배경(진짜 알파 채널)으로 만든 macOS 템플릿 전용
    // 에셋이다 — setTemplateImage(true) 는 알파를 마스크로 쓰고 RGB 는 무시하기 때문에,
    // 이전처럼 배경까지 불투명(알파 없음)한 PNG 를 넘기면 아이콘 전체가 라이트/다크 모드에
    // 따라 검정 또는 흰색 사각형으로 통짜 채워져 "색이 반전된 것처럼" 보이는 버그가 있었다.
    if (process.platform === 'darwin') {
      const tmpl = image.resize({ width: 22, height: 22 });
      tmpl.setTemplateImage(true);
      this.tray = new Tray(tmpl);
    } else {
      this.tray = new Tray(image.resize({ width: 20, height: 20 }));
    }
    this.tray.setToolTip('UpSecurity — 로컬 보안 게이트웨이');

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
      { label: 'QUIT UpSecurity', click: () => this.onQuit && this.onQuit() },
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
