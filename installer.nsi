; MomentShift (瞬变工坊) Windows 安装包脚本
; 用法（CI / 本地）：先 `pyinstaller build.spec` 生成 dist\MomentShift，
; 再 `makensis installer.nsi` 生成 MomentShift-Windows-Setup.exe。
; 采用每用户安装（LocalAppData），无需管理员权限。

!include "MUI2.nsh"

!define APPNAME "MomentShift"
!define APPNAME_ZH "瞬变工坊"
!define VERSION "0.8.15"
!define PUBLISHER "GreenChennai"
!define ICON "src\momentshift\resources\icons\app_logo.ico"

Name "${APPNAME} (${APPNAME_ZH})"
OutFile "MomentShift-Windows-Setup.exe"
InstallDir "$LOCALAPPDATA\${APPNAME}"
RequestExecutionLevel user

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\momentshift.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APPNAME}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME} (${APPNAME_ZH})"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Copyright © 2026 ${PUBLISHER}"
VIAddVersionKey "FileDescription" "${APPNAME} Installer"

Section "Install"
  SetOutPath "$INSTDIR"
  File /r "dist\MomentShift\*"

  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\momentshift.exe"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\momentshift.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName" "${APPNAME} (${APPNAME_ZH})"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayIcon" "$INSTDIR\momentshift.exe,0"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher" "${PUBLISHER}"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\${APPNAME}.lnk"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
SectionEnd
