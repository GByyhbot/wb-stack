' WorkBuddy 轮换代理隐藏启动器（无窗口后台运行，日志写入 rotator.log）
' 工作目录取脚本自身所在目录，不再写死某台机器的临时路径
Set ws = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.Run "cmd /c cd /d """ & dir & """ && C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe rotator.py serve --port 8310 >> rotator.log 2>&1", 0, False
