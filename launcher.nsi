Unicode true
Name "Endora MultiSync Setup"
OutFile "dist\Endora MultiSync Setup.exe"
Icon "endora\endora.ico"
RequestExecutionLevel user
SilentInstall silent
Section
  StrCpy $0 "$EXEDIR\EndoraMultiSync-Setup.exe"
  IfFileExists "$EXEDIR\EndoraMultiSync-Setup.part0" 0 missing
  IfFileExists "$EXEDIR\EndoraMultiSync-Setup.part1" 0 missing
  IfFileExists "$EXEDIR\EndoraMultiSync-Setup.part2" 0 missing

  SetOutPath "$EXEDIR"
  nsExec::Exec '"$SYSDIR\cmd.exe" /c copy /b /y EndoraMultiSync-Setup.part0+EndoraMultiSync-Setup.part1+EndoraMultiSync-Setup.part2 EndoraMultiSync-Setup.exe'
  Pop $1

  StrCmp $1 "0" 0 failed
  ExecShell "open" "$0"
  Quit
  missing:
    MessageBox MB_ICONSTOP "Keep the three EndoraMultiSync-Setup.part files in the same folder as this Setup, then run it again."
    Quit
  failed:
    MessageBox MB_ICONSTOP "Could not prepare the installer (error $1). Make sure the folder is not read-only and there is 100 MB free."
SectionEnd
