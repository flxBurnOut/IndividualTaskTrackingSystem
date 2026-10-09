Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
python = base & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(python) Then
    MsgBox "The Beta test environment is missing. Follow docs/Beta development setup first.", 48, "Personal Management - Beta Test"
    WScript.Quit 1
End If
shell.CurrentDirectory = base
shell.Run Chr(34) & python & Chr(34) & " -m management --data-dir " & Chr(34) & base & "\.beta-data\default" & Chr(34) & " --ui-style classic", 0, False
