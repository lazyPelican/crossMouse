@echo off
echo ==========================================
echo   Building Mouse Share Server (.exe)
echo ==========================================
echo.
echo [1/2] Installing dependencies ...
pip install pyinstaller pynput pystray Pillow
echo.
echo [2/2] Building executable ...
pyinstaller --onefile --windowed --name "MouseShareServer" ^
    --hidden-import=pynput.keyboard._win32 ^
    --hidden-import=pynput.mouse._win32 ^
    --hidden-import=pystray._win32 ^
    --hidden-import=PIL ^
    server_gui.py
echo.
echo ==========================================
echo   BUILD COMPLETE
echo   .exe location:  dist\MouseShareServer.exe
echo ==========================================
echo.
pause
