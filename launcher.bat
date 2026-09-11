@echo off
rem ============================================================
rem  双击即用：先归档一次，再启动本地服务。
rem
rem  编码说明（.bat 唯一的坑）：
rem    cmd 按系统 ANSI 码页读 .bat（中文 Windows 是 936/GBK），
rem    所以这个文件必须存成 GBK，存 UTF-8 注释会变乱码。
rem
rem    但下面的 echo 刻意写成英文：echo 出来的字怎么显示，
rem    取决于控制台"当前"码页；用英文就任何码页都不会乱。
rem    中文提示交给 Python 打印——它走 Unicode 接口，码页无关。
rem
rem  最后那个窗口别关，它就是服务本身。关窗口 = 停服务。
rem ============================================================

rem 切到本文件所在目录。双击时本来就是这里，但从快捷方式
rem 或计划任务启动时起始位置可能不对，这一行能兜住。
cd /d "%~dp0"

echo.
echo === [1/2] Archiving browsing history ===
python history_archive.py sync

rem errorlevel 不为 0 就是出错。不判断的话，Python 没装或者
rem 脚本报错时窗口会一闪而过，你什么都看不到。
if errorlevel 1 goto failed

echo.
echo === [2/2] Starting local server ===
echo The browser will open automatically.
echo To stop: close this window, or press Ctrl+C.
echo.
python history_archive.py view

echo.
echo Server stopped.
pause
exit /b 0

:failed
echo.
echo [x] Archiving failed. See the error above.
echo     Common cause: Python is not installed, or not on PATH.
pause
exit /b 1
