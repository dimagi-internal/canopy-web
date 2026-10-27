@echo off
rem Canopy Runner - Stop (the Start-menu entry). Disabled as well as ended: the task's
rem 5-minute repeat would otherwise start it again. Restart (or the next install or
rem update, which re-registers the task) turns it back on.
set TASK=\Canopy\canopy-runner
schtasks /Change /TN "%TASK%" /DISABLE >nul 2>&1
schtasks /End /TN "%TASK%" >nul
