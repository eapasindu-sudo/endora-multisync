Unicode true
!define APPNAME "Endora MultiSync"
!ifndef VERSION
  !define VERSION "1.1.0"
!endif
!define EXE "EndoraMultiSync.exe"
Name "${APPNAME}"
OutFile "dist\EndoraMultiSync-Setup-${VERSION}.exe"
InstallDir "$PROGRAMFILES64\Endora MultiSync"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
Icon "endora\endora.ico"
UninstallIcon "endora\endora.ico"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "Endora Wedding Films"
VIAddVersionKey "FileDescription" "${APPNAME} installer"
!include "MUI2.nsh"
!define MUI_ICON "endora\endora.ico"
!define MUI_UNICON "endora\endora.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${EXE}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"
Function .onInit
  ; auto update runs us with /S: wait for the app to close (force it after ~10 s)
  IfSilent 0 done
  StrCpy $0 0
  loop:
    nsExec::Exec 'cmd /c tasklist /FI "IMAGENAME eq ${EXE}" | find /I "${EXE}"'
    Pop $1
    StrCmp $1 "0" 0 done
    IntOp $0 $0 + 1
    IntCmp $0 20 0 0 kill
    Sleep 500
    Goto loop
  kill:
    nsExec::Exec 'taskkill /F /IM ${EXE}'
    Sleep 1000
  done:
FunctionEnd
Section "Install"
  SetShellVarContext all
  SetOutPath "$INSTDIR"
  File /r "dist\EndoraMultiSync\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\${EXE}"
  CreateShortCut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\${EXE}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\EndoraMultiSync" "DisplayName" "${APPNAME}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\EndoraMultiSync" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\EndoraMultiSync" "Publisher" "Endora Wedding Films"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\EndoraMultiSync" "DisplayIcon" "$INSTDIR\${EXE}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\EndoraMultiSync" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  ; after a silent update, start the new version as the normal (non-admin) user
  IfSilent 0 +2
  Exec '"$WINDIR\explorer.exe" "$INSTDIR\${EXE}"'
SectionEnd
Section "Uninstall"
  SetShellVarContext all
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\EndoraMultiSync"
SectionEnd
