; Campfire 설치기 손질 — electron-builder 가 build/installer.nsh 를 자동으로 넣는다.
;
; 업데이트는 소리 없이(/S) 돌리지 않고 진행 창을 보인다(main/updater.js). 업데이트(--updated)일 때는
; 진행 막대만 보이도록 마법사 화면을 건너뛴다. 설치 폴더 화면은 원래 템플릿이 건너뛴다.
;   - "누구를 위해 설치할까요": 기존 설치(내 계정용 / 모든 사용자용)를 그대로 따른다
;   - "마침": 건너뛴다. 앱의 "다시 시작해 업데이트" 로 설치했으면(--force-run) 앱을 다시 연다
; 새로 설치할 때의 화면은 바꾸지 않는다 — 아래 customFinishPage 는 템플릿(assistedInstaller.nsh)의
; 기본 마침 화면을 그대로 옮기고, 업데이트일 때 건너뛰는 PRE 함수만 더했다.

!macro customInstallMode
  ${if} ${isUpdated}
    ${if} $hasPerUserInstallation == "1"
    ${andIf} $hasPerMachineInstallation == "0"
      StrCpy $isForceCurrentInstall "1"
    ${elseIf} $hasPerMachineInstallation == "1"
    ${andIf} $hasPerUserInstallation == "0"
      StrCpy $isForceMachineInstall "1"
    ${endIf}
  ${endIf}
!macroend

!macro customFinishPage
  !ifndef HIDE_RUN_AFTER_FINISH
    Function StartApp
      ${if} ${isUpdated}
        StrCpy $1 "--updated"
      ${else}
        StrCpy $1 ""
      ${endif}
      ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
    FunctionEnd

    !define MUI_FINISHPAGE_RUN
    !define MUI_FINISHPAGE_RUN_FUNCTION "StartApp"
  !endif

  Function campfireSkipFinishIfUpdated
    ${if} ${isUpdated}
      ${GetParameters} $R0
      ClearErrors
      ${GetOptions} $R0 "--force-run" $R1
      ${ifNot} ${Errors}
        ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "--updated"
      ${endIf}
      Abort
    ${endIf}
  FunctionEnd

  !define MUI_PAGE_CUSTOMFUNCTION_PRE campfireSkipFinishIfUpdated
  !insertmacro MUI_PAGE_FINISH
!macroend
