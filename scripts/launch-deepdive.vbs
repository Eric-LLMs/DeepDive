' Double-click launcher for the DeepDive desktop workbench (no console window).
' Runs scripts/start_desktop.sh in a hidden shell; the Electron window is a
' normal GUI app. Used by the desktop shortcut (DeepDive.lnk).
Option Explicit

Dim fso, sh, root, bash, drive, probe, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

' Repo root = parent of this script's folder (scripts\..).
root = fso.GetParentFolderName(WScript.ScriptFullName)
root = fso.GetParentFolderName(root)

' Locate Git Bash (same probing as start-deepdive.bat).
bash = ""
For Each drive In Array("C", "D", "E", "F", "G")
    probe = drive & ":\Program Files\Git\bin\bash.exe"
    If fso.FileExists(probe) Then bash = probe
    If bash = "" Then
        probe = drive & ":\Program Files (x86)\Git\bin\bash.exe"
        If fso.FileExists(probe) Then bash = probe
    End If
    If bash <> "" Then Exit For
Next
If bash = "" Then
    MsgBox "Git Bash not found. Install Git for Windows first.", 48, "DeepDive"
    WScript.Quit 1
End If

' Start the one-click script in a hidden console (window style 0). The script
' is idempotent: starts backend :8300 + web UI :5173 if needed, then Electron.
sh.CurrentDirectory = root
cmd = """" & bash & """ scripts/start_desktop.sh"
sh.Run cmd, 0, False
