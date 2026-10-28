@echo off
echo Kill common dev/test ports...

for %%p in (3000 3300 3301 8000 8800 8090 18080) do (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%%p ^| findstr LISTENING') do (
        echo Kill PID %%a on port %%p
        taskkill /F /PID %%a 2>nul
    )
)

echo Done.
pause
