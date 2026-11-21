' WorkBuddy 轮换代理隐藏启动器（无窗口后台运行，日志写入 rotator.log）
Set ws = CreateObject("Wscript.Shell")
ws.Run "cmd /c cd /d D:\workbuddy-tmp\2026-11-06-12-15-29\wb-rotator && C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe rotator.py serve --port 8310 >> rotator.log 2>&1", 0, False
