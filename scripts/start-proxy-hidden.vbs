' Start the web-search proxy with NO console window.
'
' The script resolves its own folder, so the repo can live anywhere.
' Requires pythonw.exe on PATH (comes with any normal Python 3.11+ install).
'
' Double-click it, or put a shortcut in  shell:startup  to run at logon.
' Stop the proxy with:  Get-Process pythonw | Stop-Process

Option Explicit

Dim fso, shell, here, proxy, cmd
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here  = fso.GetParentFolderName(WScript.ScriptFullName)              ' ...\scripts
proxy = fso.BuildPath(fso.GetParentFolderName(here), "proxy\ollama-proxy.py")
If Not fso.FileExists(proxy) Then
    proxy = fso.BuildPath(here, "ollama-proxy.py")                   ' flat layout
End If
If Not fso.FileExists(proxy) Then
    MsgBox "ollama-proxy.py not found near " & WScript.ScriptFullName, 16, "web-search proxy"
    WScript.Quit 1
End If

' pythonw.exe is python.exe without the console window.
cmd = """pythonw.exe"" """ & proxy & """ --port 11435"

' 0 = hidden window, False = do not wait for the process to exit.
shell.Run cmd, 0, False
