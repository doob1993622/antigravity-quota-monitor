Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pluginDir = fso.GetParentFolderName(scriptDir)
launcherPath = pluginDir & "\sidecars\quota_floating\launch.mjs"
widgetPath = fso.BuildPath(scriptDir, "quota_floating_widget.pyw")

nodeRc = shell.Run("cmd /c where node >nul 2>nul", 0, True)
If nodeRc = 0 Then
  shell.Run "node """ & launcherPath & """ --detached", 0, False
Else
  shell.Run "pythonw """ & widgetPath & """", 0, False
End If
