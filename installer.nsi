; MomentShift (瞬变工坊) Windows 安装包脚本
; 用法（CI / 本地）：先 `pyinstaller build.spec` 生成 dist\MomentShift，
; 再 `makensis installer.nsi` 生成 MomentShift-Windows-Setup.exe。
;
; v0.8.16 变更：
;   1. `Unicode true` + 源文件带 UTF-8 BOM —— 修复安装界面中文显示成
;      "çž¬å˜å·¥åŠ" 的乱码。根因是 NSIS 3 在没有 BOM 时按构建机的 ANSI
;      代码页解析源码，GitHub Actions 的英文 Windows 用 CP1252，UTF-8
;      中文字节被逐字节误读。两者缺一不可：BOM 决定源码怎么读，
;      Unicode true 决定安装器运行时怎么显示。
;   2. 默认装到 `C:\Program Files (x86)\MomentShift`（需管理员），
;      并在 .onInit 里检测旧版安装位置沿用之，避免一台机器出现两份。

Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define APPNAME "MomentShift"
!define APPNAME_ZH "瞬变工坊"
!define VERSION "0.8.17"
!define PUBLISHER "GreenChennai"
!define ICON "src\momentshift\resources\icons\app_logo.ico"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"

Name "${APPNAME} (${APPNAME_ZH})"
OutFile "MomentShift-Windows-Setup.exe"
InstallDir "$PROGRAMFILES32\${APPNAME}"
RequestExecutionLevel admin
ShowInstDetails show
ShowUnInstDetails show

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APPNAME}"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchAsUser

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

; ---------------------------------------------------------------------------
; 完成页「立即启动」
; ---------------------------------------------------------------------------
Function LaunchAsUser
  ; 安装器以管理员身份运行，直接 Exec 会让主程序继承管理员令牌。
  ; 提权后的窗口收不到资源管理器的拖放（Windows UIPI 完整性级别限制），
  ; 而拖放是本软件的主要入口，所以借 explorer.exe 转发，
  ; 让主程序退回登录用户的普通权限运行。
  Exec '"$WINDIR\explorer.exe" "$INSTDIR\momentshift.exe"'
FunctionEnd

; ---------------------------------------------------------------------------
; 旧版本安装位置探测：装过就装回原地，防止一台电脑出现两份
; ---------------------------------------------------------------------------
Function .onInit
  ; 1) 机器级安装（v0.8.16 起的默认位置），64 位与 32 位注册表视图都看一遍
  SetRegView 64
  ReadRegStr $0 HKLM "${UNINST_KEY}" "InstallLocation"
  SetRegView 32
  ${If} $0 == ""
    ReadRegStr $0 HKLM "${UNINST_KEY}" "InstallLocation"
  ${EndIf}

  ; 2) 旧的每用户安装（v0.8.15 及更早，装在 %LOCALAPPDATA%\MomentShift）
  ${If} $0 == ""
    ReadRegStr $0 HKCU "${UNINST_KEY}" "InstallLocation"
  ${EndIf}

  ; 3) 更早的版本没写过 InstallLocation，从卸载器路径反推安装目录
  ${If} $0 == ""
    ReadRegStr $1 HKLM "${UNINST_KEY}" "UninstallString"
    ${If} $1 == ""
      ReadRegStr $1 HKCU "${UNINST_KEY}" "UninstallString"
    ${EndIf}
    ${If} $1 != ""
      ${GetParent} $1 $0
    ${EndIf}
  ${EndIf}

  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
FunctionEnd

Section "Install"
  ; 机器级安装：快捷方式写到「所有用户」，与 Program Files 的定位一致
  SetShellVarContext all

  SetOutPath "$INSTDIR"
  File /r "dist\MomentShift\*"

  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\momentshift.exe"
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\momentshift.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "${APPNAME} (${APPNAME_ZH})"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\momentshift.exe,0"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "${PUBLISHER}"
  ; InstallLocation 是下次升级时定位旧版本的唯一依据，必须写
  WriteRegStr HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKLM "${UNINST_KEY}" "EstimatedSize" "$0"

  ; 从 v0.8.15 及更早的每用户安装升级上来时，清掉旧的卸载登记项，
  ; 否则「应用和功能」里会同时出现两条 MomentShift 记录
  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd

Section "Uninstall"
  ; 应用运行时把右键菜单写在 HKCU，卸载时一并清掉，避免留下失效菜单项
  DeleteRegKey HKCU "Software\Classes\*\shell\MomentShift.Convert"
  DeleteRegKey HKCU "Software\Classes\*\shell\MomentShift.Compress"
  DeleteRegKey HKCU "Software\Classes\*\shell\MomentShift.Upscale"

  ; 快捷方式在两种上下文都清一遍：机器级安装写在 all，
  ; 从旧的每用户安装升级上来的残留写在 current
  SetShellVarContext all
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\${APPNAME}.lnk"
  SetShellVarContext current
  Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
  RMDir "$SMPROGRAMS\${APPNAME}"
  Delete "$DESKTOP\${APPNAME}.lnk"

  RMDir /r "$INSTDIR"

  DeleteRegKey HKLM "${UNINST_KEY}"
  DeleteRegKey HKCU "${UNINST_KEY}"
SectionEnd
