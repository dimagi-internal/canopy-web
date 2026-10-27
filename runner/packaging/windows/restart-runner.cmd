@echo off
rem Canopy Runner - Restart (the Start-menu entry). The task is IgnoreNew, so a /Run
rem that lands while /End is still settling is dropped: end it, wait until it is no
rem longer Running (up to 15 s), then run it -- what platform_jobs.restart_windows_task does.
set TASK=\Canopy\canopy-runner
schtasks /Change /TN "%TASK%" /ENABLE >nul 2>&1
schtasks /End /TN "%TASK%" >nul 2>&1
for /l %%i in (1,1,15) do (
  rem The State enum: the status column schtasks prints is translated.
  powershell -NoProfile -NonInteractive -Command "if ((Get-ScheduledTask -TaskPath '\Canopy\' -TaskName 'canopy-runner').State -eq 'Running') { exit 0 } else { exit 1 }" || goto run
  ping -n 2 127.0.0.1 >nul
)
:run
schtasks /Run /TN "%TASK%" >nul
