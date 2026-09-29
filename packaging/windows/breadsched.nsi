; BreadSched Windows installer (NSIS).
;
; Built by packaging/windows/build-installer.sh, which passes VERSION, STAGE (the
; staged MSYS2 UCRT64 runtime with BreadSched installed), and OUTFILE. The
; installer is per-user: it needs no administrator rights, installs under
; %LOCALAPPDATA%\Programs\BreadSched, and registers an uninstaller for the
; current user only. Books are never inside the installation directory, so
; upgrading or uninstalling never touches them.
;
; Adding the command line to the user's PATH is optional and off by default:
; choose it on the Components page, or pass /ADDTOPATH to a silent install. A
; later install keeps that choice unless it is changed, and the uninstaller always
; removes the entry. user_path.py makes the change with the bundled Python.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"

!ifndef VERSION
  !error "VERSION is required"
!endif
!ifndef STAGE
  !error "STAGE is required"
!endif
!ifndef OUTFILE
  !define OUTFILE "BreadSched-${VERSION}-setup.exe"
!endif

!define APP "BreadSched"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\BreadSched"

Name "${APP} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_LICENSE "${STAGE}\LICENSE.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\runtime\bin\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS "-m breadsched.gui"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; Runs stop-helpers.ps1, which stops gdbus.exe helpers started from this runtime.
!define STOP_HELPERS '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File'

Section "BreadSched" SecMain
  SectionIn RO
  ; An upgrade replaces the whole runtime so no stale module survives. A helper
  ; still running from the old runtime would keep its files open, so stop it first.
  InitPluginsDir
  File "/oname=$PLUGINSDIR\stop-helpers.ps1" "${STAGE}\stop-helpers.ps1"
  nsExec::ExecToLog '${STOP_HELPERS} "$PLUGINSDIR\stop-helpers.ps1" "$INSTDIR"'
  Pop $0
  RMDir /r "$INSTDIR\runtime"
  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*"

  CreateDirectory "$SMPROGRAMS\${APP}"
  CreateShortcut "$SMPROGRAMS\${APP}\${APP}.lnk" "$INSTDIR\runtime\bin\pythonw.exe" "-m breadsched.gui" \
    "$INSTDIR\runtime\bin\pythonw.exe" 0
  CreateShortcut "$SMPROGRAMS\${APP}\Uninstall ${APP}.lnk" "$INSTDIR\Uninstall.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APP}" "Version" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "BreadSched contributors"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

!define USER_PATH '"$INSTDIR\runtime\bin\python.exe" "$INSTDIR\user_path.py"'

Section /o "Add the breadsched command to PATH" SecPath
  nsExec::ExecToLog '${USER_PATH} add "$INSTDIR"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONEXCLAMATION "Could not add $INSTDIR to PATH ($0)." /SD IDOK
    SetErrorLevel 3
  ${EndIf}
  WriteRegDWORD HKCU "Software\${APP}" "AddToPath" 1
SectionEnd

; Deselecting the option on a later install takes the entry off PATH again.
Section "-Keep PATH as chosen"
  ${IfNot} ${SectionIsSelected} ${SecPath}
    nsExec::ExecToLog '${USER_PATH} remove "$INSTDIR"'
    Pop $0
    WriteRegDWORD HKCU "Software\${APP}" "AddToPath" 0
  ${EndIf}
SectionEnd

Function .onInit
  ReadRegDWORD $0 HKCU "Software\${APP}" "AddToPath"
  ${GetParameters} $1
  ClearErrors
  ${GetOptions} $1 "/ADDTOPATH" $2
  ${IfNot} ${Errors}
    StrCpy $0 1
  ${EndIf}
  ${If} $0 == 1
    !insertmacro SelectSection ${SecPath}
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\${APP}\${APP}.lnk"
  Delete "$SMPROGRAMS\${APP}\Uninstall ${APP}.lnk"
  RMDir "$SMPROGRAMS\${APP}"
  nsExec::ExecToLog '${USER_PATH} remove "$INSTDIR"'
  Pop $0
  nsExec::ExecToLog '${STOP_HELPERS} "$INSTDIR\stop-helpers.ps1" "$INSTDIR"'
  Pop $0
  RMDir /r "$INSTDIR\runtime"
  Delete "$INSTDIR\breadsched.cmd"
  Delete "$INSTDIR\breadsched-gtk.cmd"
  Delete "$INSTDIR\LICENSE.txt"
  Delete "$INSTDIR\user_path.py"
  Delete "$INSTDIR\stop-helpers.ps1"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\${APP}"
SectionEnd
