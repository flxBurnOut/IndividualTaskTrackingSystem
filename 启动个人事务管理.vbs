Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run Chr(34) & base & "\release\PersonalManagement-1.0.0\PersonalManagement.exe" & Chr(34) & " --data-dir " & Chr(34) & base & "\data" & Chr(34), 1, False
