Option Explicit
Dim files, shell, root, launcher
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
launcher = files.BuildPath(root, "mover-v2-launcher.exe")
If Not files.FileExists(launcher) Then
    WScript.Echo "Please fully extract the release ZIP, including mover-v2-launcher.exe."
    WScript.Quit 2
End If
shell.CurrentDirectory = root
shell.Run Chr(34) & launcher & Chr(34) & " --open", 0, False
