!macro customInit
  SetShellVarContext current
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\Key Shift Piano"
!macroend

!macro customCheckAppRunning
  ${nsProcess::FindProcess} "${APP_EXECUTABLE_FILENAME}" $R0
  ${If} $R0 == 0
    MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION "$(appRunning)" /SD IDOK IDOK keyShiftCloseApp
    Quit

    keyShiftCloseApp:
      ${nsProcess::CloseProcess} "${APP_EXECUTABLE_FILENAME}" $R0
      Sleep 1000
      ${nsProcess::FindProcess} "${APP_EXECUTABLE_FILENAME}" $R0
      ${If} $R0 == 0
        ${nsProcess::KillProcess} "${APP_EXECUTABLE_FILENAME}" $R0
        Sleep 500
      ${EndIf}
  ${EndIf}
!macroend
